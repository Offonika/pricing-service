from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Literal, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.customer_settlement import (
    CustomerAccount,
    CustomerAccountSiteBinding,
    CustomerAccountSourceBinding,
    CustomerSettlementAlertOutbox,
    CustomerSettlementAssertionJti,
    CustomerSettlementBalance,
    CustomerSettlementMappingEntry,
    CustomerSettlementMappingRevision,
    CustomerSettlementPilotAccess,
    CustomerSettlementReconciliationRun,
    CustomerSettlementRevision,
)

MAPPING_LINKED = "linked"
MAPPING_NOT_LINKED = "not_linked"
MAPPING_AMBIGUOUS = "ambiguous"
REVISION_ACTIVE = "active"
REVISION_SUPERSEDED = "superseded"

_MONEY_QUANTUM = Decimal("0.01")
_COUNTERPARTY_REF_RE = re.compile(r"^0x[0-9a-fA-F]{32}$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-" r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SITE_USER_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
DEFAULT_SITE_CODE = "master-mobile.ru"
DEFAULT_SOURCE_SYSTEM = "ut103"
DEFAULT_ORGANIZATION_REF = "0xb34a0025901e48ef11e211128227ea80"
DEFAULT_ORGANIZATION_GUID = "8227ea80-1112-11e2-b34a-0025901e48ef"
MANUAL_MAPPING_SOURCE_NAME = "manual_confirmed_pilot"
MAX_PILOT_USERS = 10


@dataclass(frozen=True)
class SettlementBalanceInput:
    counterparty_ref: str
    signed_balance: Decimal
    counterparty_guid: str | None = None
    currency: str = "RUB"
    exists: bool = True
    marked_deleted: bool = False


@dataclass(frozen=True)
class SettlementMappingInput:
    site_user_id: str
    cluster_id: str | None
    counterparty_ref: str | None
    status: Literal["linked", "not_linked", "ambiguous"]
    counterparty_guid: str | None = None
    counterparty_code: str | None = None
    identity_control_hash: str | None = None
    source_updated_at: datetime | None = None


@dataclass(frozen=True)
class SettlementSummary:
    status: Literal[
        "available",
        "stale",
        "temporarily_unavailable",
        "not_linked",
        "ambiguous_link",
        "pilot_disabled",
    ]
    state: Literal["debt", "advance", "zero"] | None = None
    amount: Decimal | None = None
    currency: Literal["RUB"] | None = None
    as_of: datetime | None = None
    synced_at: datetime | None = None
    is_stale: bool = False


EligibilityStatus = Literal["eligible", "not_eligible", "temporarily_unavailable"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_counterparty_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not _COUNTERPARTY_REF_RE.fullmatch(normalized):
        raise ValueError("counterparty_ref must be 0x followed by 32 hexadecimal characters")
    return "0x" + normalized[2:].lower()


def normalize_guid(value: str) -> str:
    normalized = str(value or "").strip().strip("{}").lower()
    if not _GUID_RE.fullmatch(normalized):
        raise ValueError("GUID must use canonical 8-4-4-4-12 format")
    return normalized


def onec_ref_to_guid(value: str) -> str:
    normalized = normalize_counterparty_ref(value)[2:]
    return "-".join(
        (
            normalized[24:32],
            normalized[20:24],
            normalized[16:20],
            normalized[0:4],
            normalized[4:16],
        )
    )


def onec_guid_to_ref(value: str) -> str:
    normalized = normalize_guid(value)
    first, second, third, fourth, fifth = normalized.split("-")
    return normalize_counterparty_ref(f"0x{fourth}{fifth}{third}{second}{first}")


def normalize_site_user_id(value: str | int) -> str:
    normalized = str(value).strip()
    if not _SITE_USER_ID_RE.fullmatch(normalized):
        raise ValueError("site_user_id must be a positive decimal identifier")
    return normalized


def normalize_money(value: Decimal | str | int | float) -> Decimal:
    normalized = Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return Decimal("0.00") if normalized == 0 else normalized


def settlement_state(value: Decimal) -> tuple[Literal["debt", "advance", "zero"], Decimal]:
    normalized = normalize_money(value)
    if normalized > 0:
        return "debt", normalized
    if normalized < 0:
        return "advance", abs(normalized)
    return "zero", Decimal("0.00")


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_balance_rows(
    rows: Iterable[SettlementBalanceInput],
) -> list[SettlementBalanceInput]:
    result: list[SettlementBalanceInput] = []
    seen: set[str] = set()
    for item in rows:
        counterparty_ref = normalize_counterparty_ref(item.counterparty_ref)
        counterparty_guid = (
            normalize_guid(item.counterparty_guid)
            if item.counterparty_guid is not None
            else onec_ref_to_guid(counterparty_ref)
        )
        if onec_guid_to_ref(counterparty_guid) != counterparty_ref:
            raise ValueError("counterparty_guid_does_not_match_ref")
        if counterparty_ref in seen:
            raise ValueError("duplicate_counterparty_ref")
        seen.add(counterparty_ref)
        if item.currency != "RUB":
            raise ValueError("unsupported_currency")
        if not item.exists or item.marked_deleted:
            raise ValueError("invalid_counterparty_mapping")
        result.append(
            SettlementBalanceInput(
                counterparty_ref=counterparty_ref,
                signed_balance=normalize_money(item.signed_balance),
                counterparty_guid=counterparty_guid,
                currency="RUB",
            )
        )
    return sorted(result, key=lambda item: item.counterparty_ref)


def activate_financial_revision(
    session: Session,
    *,
    organization_ref: str,
    organization_guid: str | None = None,
    as_of: datetime,
    source_db_time: datetime,
    source_mode: str,
    expected_counterparty_refs: Sequence[str],
    balances: Sequence[SettlementBalanceInput],
    synced_at: datetime | None = None,
) -> tuple[CustomerSettlementRevision, bool]:
    organization = normalize_counterparty_ref(organization_ref)
    normalized_organization_guid = normalize_guid(
        organization_guid or onec_ref_to_guid(organization)
    )
    if onec_guid_to_ref(normalized_organization_guid) != organization:
        raise ValueError("organization_guid_does_not_match_ref")
    expected_refs = {normalize_counterparty_ref(value) for value in expected_counterparty_refs}
    normalized_rows = _normalized_balance_rows(balances)
    loaded_refs = {item.counterparty_ref for item in normalized_rows}
    if expected_refs != loaded_refs:
        raise ValueError("incomplete_financial_revision")

    as_of_utc = ensure_utc(as_of)
    source_db_time_utc = ensure_utc(source_db_time)
    synced_at_utc = ensure_utc(synced_at or utc_now())
    source_hash = _canonical_hash(
        {
            "organization_ref": organization,
            "organization_guid": normalized_organization_guid,
            "currency": "RUB",
            "as_of": as_of_utc.isoformat(),
            "source_db_time": source_db_time_utc.isoformat(),
            "source_mode": source_mode,
            "balances": [
                {
                    "counterparty_ref": item.counterparty_ref,
                    "counterparty_guid": item.counterparty_guid,
                    "signed_balance": format(item.signed_balance, ".2f"),
                }
                for item in normalized_rows
            ],
        }
    )
    existing = session.scalar(
        select(CustomerSettlementRevision).where(
            CustomerSettlementRevision.source_hash == source_hash
        )
    )
    if existing is not None:
        return existing, False

    revision = CustomerSettlementRevision(
        status="loading",
        organization_ref=organization,
        organization_guid=normalized_organization_guid,
        currency="RUB",
        as_of=as_of_utc,
        source_db_time=source_db_time_utc,
        synced_at=synced_at_utc,
        source_mode=source_mode,
        source_hash=source_hash,
        expected_row_count=len(expected_refs),
        loaded_row_count=0,
        zero_row_count=0,
    )
    session.add(revision)
    session.flush()
    for item in normalized_rows:
        session.add(
            CustomerSettlementBalance(
                revision_id=revision.id,
                counterparty_ref=item.counterparty_ref,
                counterparty_guid=str(item.counterparty_guid),
                signed_balance=item.signed_balance,
                currency="RUB",
            )
        )
    session.flush()
    revision.loaded_row_count = len(normalized_rows)
    revision.zero_row_count = sum(1 for item in normalized_rows if item.signed_balance == 0)
    if revision.loaded_row_count != revision.expected_row_count:
        raise ValueError("financial_revision_count_mismatch")

    session.execute(
        update(CustomerSettlementRevision)
        .where(CustomerSettlementRevision.status == REVISION_ACTIVE)
        .values(status=REVISION_SUPERSEDED, updated_at=synced_at_utc)
    )
    session.flush()
    revision.status = REVISION_ACTIVE
    revision.activated_at = synced_at_utc
    revision.updated_at = synced_at_utc
    session.flush()
    return revision, True


def mark_financial_revision_failed(
    session: Session,
    *,
    organization_ref: str,
    organization_guid: str | None = None,
    as_of: datetime,
    source_mode: str,
    error_code: str,
    error_detail: str | None = None,
) -> CustomerSettlementRevision:
    now = utc_now()
    normalized_organization_ref = normalize_counterparty_ref(organization_ref)
    normalized_organization_guid = normalize_guid(
        organization_guid or onec_ref_to_guid(normalized_organization_ref)
    )
    revision = CustomerSettlementRevision(
        status="failed",
        organization_ref=normalized_organization_ref,
        organization_guid=normalized_organization_guid,
        currency="RUB",
        as_of=ensure_utc(as_of),
        source_db_time=now,
        synced_at=now,
        source_mode=source_mode,
        source_hash=_canonical_hash(
            {
                "failed_at": now.isoformat(),
                "organization_ref": organization_ref,
                "error_code": error_code,
            }
        ),
        expected_row_count=0,
        loaded_row_count=0,
        zero_row_count=0,
        error_code=error_code[:96],
        error_detail=(error_detail or "")[:1000] or None,
    )
    session.add(revision)
    session.flush()
    return revision


def _materialize_linked_customer_account(
    session: Session,
    *,
    revision_id: int,
    item: SettlementMappingInput,
    checked_at: datetime,
    source_system: str,
    organization_ref: str,
    organization_guid: str,
) -> tuple[int, int, str]:
    if item.status != MAPPING_LINKED or not item.cluster_id or not item.counterparty_ref:
        raise ValueError("linked_mapping_requires_cluster_and_counterparty")
    counterparty_ref = normalize_counterparty_ref(item.counterparty_ref)
    counterparty_guid = normalize_guid(item.counterparty_guid or onec_ref_to_guid(counterparty_ref))
    if onec_guid_to_ref(counterparty_guid) != counterparty_ref:
        raise ValueError("counterparty_guid_does_not_match_ref")

    site_binding = session.scalar(
        select(CustomerAccountSiteBinding).where(
            CustomerAccountSiteBinding.site_code == DEFAULT_SITE_CODE,
            CustomerAccountSiteBinding.site_user_id == item.site_user_id,
            CustomerAccountSiteBinding.status == "active",
        )
    )
    identity_binding = session.scalar(
        select(CustomerAccountSourceBinding).where(
            CustomerAccountSourceBinding.source_system == source_system,
            CustomerAccountSourceBinding.counterparty_guid == counterparty_guid,
            CustomerAccountSourceBinding.organization_guid == organization_guid,
            CustomerAccountSourceBinding.status == "active",
        )
    )
    account_ids = {
        value
        for value in (
            site_binding.customer_account_id if site_binding is not None else None,
            identity_binding.customer_account_id if identity_binding is not None else None,
        )
        if value is not None
    }
    if len(account_ids) > 1:
        raise ValueError("durable_customer_account_mapping_conflict")
    if account_ids:
        account_id = next(iter(account_ids))
        account = session.get(CustomerAccount, account_id)
        if account is None or account.status != "active":
            raise ValueError("customer_account_is_not_active")
    else:
        account = CustomerAccount(status="active")
        session.add(account)
        session.flush()
        account_id = account.id

    account_source = session.scalar(
        select(CustomerAccountSourceBinding).where(
            CustomerAccountSourceBinding.customer_account_id == account_id,
            CustomerAccountSourceBinding.source_system == source_system,
            CustomerAccountSourceBinding.organization_guid == organization_guid,
            CustomerAccountSourceBinding.status == "active",
        )
    )
    if account_source is not None and account_source.counterparty_guid != counterparty_guid:
        account_source.status = "revoked"
        account_source.valid_to = checked_at
        account_source.updated_at = checked_at
        session.flush()
        account_source = None
    if identity_binding is not None and identity_binding.customer_account_id != account_id:
        raise ValueError("counterparty_guid_is_linked_to_another_customer_account")
    source_binding = account_source or identity_binding
    if source_binding is None:
        source_binding = CustomerAccountSourceBinding(
            customer_account_id=account_id,
            source_system=source_system,
            counterparty_guid=counterparty_guid,
            counterparty_ref=counterparty_ref,
            organization_guid=organization_guid,
            organization_ref=organization_ref,
            counterparty_code=(
                (str(item.counterparty_code).strip()[:64] or None)
                if item.counterparty_code
                else None
            ),
            identity_control_hash=(
                str(item.identity_control_hash).strip().lower()[:64] or None
                if item.identity_control_hash
                else None
            ),
            status="active",
            mapping_revision_id=revision_id,
            valid_from=checked_at,
            verified_at=checked_at,
        )
        session.add(source_binding)
        session.flush()
    else:
        source_binding.counterparty_ref = counterparty_ref
        source_binding.organization_ref = organization_ref
        source_binding.mapping_revision_id = revision_id
        source_binding.verified_at = checked_at
        source_binding.updated_at = checked_at
        if item.counterparty_code:
            source_binding.counterparty_code = str(item.counterparty_code).strip()[:64] or None
        if item.identity_control_hash:
            source_binding.identity_control_hash = (
                str(item.identity_control_hash).strip().lower()[:64] or None
            )

    if site_binding is None:
        site_binding = CustomerAccountSiteBinding(
            customer_account_id=account_id,
            site_code=DEFAULT_SITE_CODE,
            site_user_id=item.site_user_id,
            cluster_id=item.cluster_id,
            status="active",
            mapping_revision_id=revision_id,
            valid_from=checked_at,
            verified_at=checked_at,
        )
        session.add(site_binding)
    else:
        if site_binding.customer_account_id != account_id:
            raise ValueError("site_user_is_linked_to_another_customer_account")
        site_binding.cluster_id = item.cluster_id
        site_binding.mapping_revision_id = revision_id
        site_binding.verified_at = checked_at
        site_binding.updated_at = checked_at
    session.flush()
    return account_id, source_binding.id, counterparty_guid


def activate_mapping_revision(
    session: Session,
    *,
    entries: Sequence[SettlementMappingInput],
    source_checked_at: datetime | None = None,
    source_name: str = "bitrix_crm_customer_cluster",
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    organization_ref: str = DEFAULT_ORGANIZATION_REF,
    organization_guid: str = DEFAULT_ORGANIZATION_GUID,
) -> tuple[CustomerSettlementMappingRevision, bool]:
    checked_at = ensure_utc(source_checked_at or utc_now())
    normalized_source_name = str(source_name or "").strip()[:64]
    if not normalized_source_name:
        raise ValueError("mapping_source_name_is_required")
    normalized_source_system = str(source_system or "").strip().lower()
    if normalized_source_system not in {"ut103", "ka2"}:
        raise ValueError("unsupported_customer_settlement_source_system")
    normalized_organization_ref = normalize_counterparty_ref(organization_ref)
    normalized_organization_guid = normalize_guid(organization_guid)
    if onec_guid_to_ref(normalized_organization_guid) != normalized_organization_ref:
        raise ValueError("organization_guid_does_not_match_ref")
    normalized_entries: list[SettlementMappingInput] = []
    seen: set[str] = set()
    for item in entries:
        site_user_id = normalize_site_user_id(item.site_user_id)
        if site_user_id in seen:
            raise ValueError("duplicate_mapping_site_user")
        seen.add(site_user_id)
        if item.status not in {MAPPING_LINKED, MAPPING_NOT_LINKED, MAPPING_AMBIGUOUS}:
            raise ValueError("unsupported_mapping_status")
        counterparty_ref = (
            normalize_counterparty_ref(item.counterparty_ref)
            if item.counterparty_ref is not None
            else None
        )
        if item.status == MAPPING_LINKED and (not item.cluster_id or not counterparty_ref):
            raise ValueError("linked_mapping_requires_cluster_and_counterparty")
        counterparty_guid = (
            normalize_guid(item.counterparty_guid or onec_ref_to_guid(counterparty_ref))
            if counterparty_ref is not None
            else None
        )
        if (
            counterparty_ref is not None
            and onec_guid_to_ref(str(counterparty_guid)) != counterparty_ref
        ):
            raise ValueError("counterparty_guid_does_not_match_ref")
        if item.status != MAPPING_LINKED:
            counterparty_ref = None
            counterparty_guid = None
        normalized_entries.append(
            SettlementMappingInput(
                site_user_id=site_user_id,
                cluster_id=str(item.cluster_id).strip() if item.cluster_id else None,
                counterparty_ref=counterparty_ref,
                status=item.status,
                counterparty_guid=counterparty_guid,
                counterparty_code=(
                    str(item.counterparty_code).strip()[:64] or None
                    if item.counterparty_code
                    else None
                ),
                identity_control_hash=(
                    str(item.identity_control_hash).strip().lower()[:64] or None
                    if item.identity_control_hash
                    else None
                ),
                source_updated_at=(
                    ensure_utc(item.source_updated_at) if item.source_updated_at else None
                ),
            )
        )
    normalized_entries.sort(key=lambda item: item.site_user_id)
    source_hash = _canonical_hash(
        {
            "source_name": normalized_source_name,
            "source_system": normalized_source_system,
            "organization_guid": normalized_organization_guid,
            "entries": [
                {
                    "site_user_id": item.site_user_id,
                    "cluster_id": item.cluster_id,
                    "counterparty_ref": item.counterparty_ref,
                    "counterparty_guid": item.counterparty_guid,
                    "status": item.status,
                }
                for item in normalized_entries
            ],
        }
    )
    existing = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.source_hash == source_hash
        )
    )
    if existing is not None:
        existing.source_checked_at = checked_at
        existing.updated_at = checked_at
        if existing.status != REVISION_ACTIVE:
            session.execute(
                update(CustomerSettlementMappingRevision)
                .where(CustomerSettlementMappingRevision.status == REVISION_ACTIVE)
                .values(status=REVISION_SUPERSEDED, updated_at=checked_at)
            )
            session.flush()
            existing.status = REVISION_ACTIVE
            existing.activated_at = checked_at
        existing_entries = {
            row.site_user_id: row
            for row in session.scalars(
                select(CustomerSettlementMappingEntry).where(
                    CustomerSettlementMappingEntry.revision_id == existing.id
                )
            )
        }
        for item in normalized_entries:
            row = existing_entries.get(item.site_user_id)
            if row is None or item.status != MAPPING_LINKED:
                continue
            account_id, source_binding_id, counterparty_guid = _materialize_linked_customer_account(
                session,
                revision_id=existing.id,
                item=item,
                checked_at=checked_at,
                source_system=normalized_source_system,
                organization_ref=normalized_organization_ref,
                organization_guid=normalized_organization_guid,
            )
            row.customer_account_id = account_id
            row.source_binding_id = source_binding_id
            row.counterparty_guid = counterparty_guid
            row.source_system = normalized_source_system
            row.organization_guid = normalized_organization_guid
        session.flush()
        return existing, False

    revision = CustomerSettlementMappingRevision(
        status="loading",
        source_name=normalized_source_name,
        source_hash=source_hash,
        source_checked_at=checked_at,
        expected_entry_count=len(normalized_entries),
        loaded_entry_count=0,
        ambiguous_count=0,
    )
    session.add(revision)
    session.flush()
    for item in normalized_entries:
        account_id = None
        source_binding_id = None
        counterparty_guid = item.counterparty_guid
        if item.status == MAPPING_LINKED:
            account_id, source_binding_id, counterparty_guid = _materialize_linked_customer_account(
                session,
                revision_id=revision.id,
                item=item,
                checked_at=checked_at,
                source_system=normalized_source_system,
                organization_ref=normalized_organization_ref,
                organization_guid=normalized_organization_guid,
            )
        session.add(
            CustomerSettlementMappingEntry(
                revision_id=revision.id,
                site_user_id=item.site_user_id,
                cluster_id=item.cluster_id,
                counterparty_ref=item.counterparty_ref,
                counterparty_guid=counterparty_guid,
                source_system=(normalized_source_system if item.status == MAPPING_LINKED else None),
                organization_guid=(
                    normalized_organization_guid if item.status == MAPPING_LINKED else None
                ),
                customer_account_id=account_id,
                source_binding_id=source_binding_id,
                status=item.status,
                source_updated_at=item.source_updated_at,
            )
        )
    session.flush()
    revision.loaded_entry_count = len(normalized_entries)
    revision.ambiguous_count = sum(
        1 for item in normalized_entries if item.status == MAPPING_AMBIGUOUS
    )
    if revision.loaded_entry_count != revision.expected_entry_count:
        raise ValueError("mapping_revision_count_mismatch")

    session.execute(
        update(CustomerSettlementMappingRevision)
        .where(CustomerSettlementMappingRevision.status == REVISION_ACTIVE)
        .values(status=REVISION_SUPERSEDED, updated_at=checked_at)
    )
    session.flush()
    revision.status = REVISION_ACTIVE
    revision.activated_at = checked_at
    revision.updated_at = checked_at
    session.flush()
    return revision, True


def mark_mapping_revision_failed(
    session: Session,
    *,
    error_code: str,
    error_detail: str | None = None,
    source_name: str = "bitrix_crm_customer_cluster",
) -> CustomerSettlementMappingRevision:
    now = utc_now()
    revision = CustomerSettlementMappingRevision(
        status="failed",
        source_name=source_name,
        source_hash=_canonical_hash(
            {
                "failed_at": now.isoformat(),
                "source_name": source_name,
                "error_code": error_code,
            }
        ),
        source_checked_at=now,
        expected_entry_count=0,
        loaded_entry_count=0,
        ambiguous_count=0,
        error_code=error_code[:96],
        error_detail=(error_detail or "")[:1000] or None,
    )
    session.add(revision)
    session.flush()
    return revision


def set_pilot_access(
    session: Session,
    *,
    site_user_id: str | int,
    enabled: bool,
    reason: str | None = None,
) -> tuple[CustomerSettlementPilotAccess, bool]:
    normalized = normalize_site_user_id(site_user_id)
    item = session.scalar(
        select(CustomerSettlementPilotAccess).where(
            CustomerSettlementPilotAccess.site_user_id == normalized
        )
    )
    if enabled and (item is None or not item.enabled):
        enabled_count = session.scalar(
            select(func.count())
            .select_from(CustomerSettlementPilotAccess)
            .where(CustomerSettlementPilotAccess.enabled.is_(True))
        )
        if int(enabled_count or 0) >= MAX_PILOT_USERS:
            raise ValueError("pilot_whitelist_limit_exceeded")
    created = item is None
    if item is None:
        item = CustomerSettlementPilotAccess(site_user_id=normalized)
        session.add(item)
    item.enabled = bool(enabled)
    item.reason = str(reason).strip()[:255] if reason else None
    session.flush()
    return item, created


def active_pilot_counterparty_refs(session: Session) -> tuple[str, ...]:
    mapping_revision = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.status == REVISION_ACTIVE
        )
    )
    if mapping_revision is None:
        return ()
    rows = session.execute(
        select(CustomerSettlementMappingEntry.counterparty_ref)
        .join(
            CustomerSettlementPilotAccess,
            CustomerSettlementPilotAccess.site_user_id
            == CustomerSettlementMappingEntry.site_user_id,
        )
        .join(
            CustomerAccountSourceBinding,
            CustomerAccountSourceBinding.id == CustomerSettlementMappingEntry.source_binding_id,
        )
        .join(
            CustomerAccount,
            CustomerAccount.id == CustomerSettlementMappingEntry.customer_account_id,
        )
        .where(
            CustomerSettlementMappingEntry.revision_id == mapping_revision.id,
            CustomerSettlementMappingEntry.status == MAPPING_LINKED,
            CustomerSettlementMappingEntry.counterparty_ref.is_not(None),
            CustomerSettlementMappingEntry.counterparty_guid.is_not(None),
            CustomerSettlementMappingEntry.customer_account_id.is_not(None),
            CustomerSettlementMappingEntry.source_binding_id.is_not(None),
            CustomerAccountSourceBinding.status == "active",
            CustomerAccountSourceBinding.counterparty_guid
            == CustomerSettlementMappingEntry.counterparty_guid,
            CustomerAccountSourceBinding.counterparty_ref
            == CustomerSettlementMappingEntry.counterparty_ref,
            CustomerAccount.status == "active",
            CustomerSettlementPilotAccess.enabled.is_(True),
        )
        .distinct()
    ).scalars()
    return tuple(sorted(str(value) for value in rows if value))


def active_pilot_site_user_ids(session: Session) -> tuple[str, ...]:
    rows = session.scalars(
        select(CustomerSettlementPilotAccess.site_user_id).where(
            CustomerSettlementPilotAccess.enabled.is_(True)
        )
    )
    return tuple(sorted(str(value) for value in rows if value))


def get_customer_settlement_eligibility(
    session: Session,
    *,
    site_user_id: str | int,
    enabled: bool,
    mapping_stale_after_seconds: int,
    now: datetime | None = None,
) -> EligibilityStatus:
    if not enabled:
        return "not_eligible"
    current_time = ensure_utc(now or utc_now())
    user_id = normalize_site_user_id(site_user_id)
    pilot = session.scalar(
        select(CustomerSettlementPilotAccess.id).where(
            CustomerSettlementPilotAccess.site_user_id == user_id,
            CustomerSettlementPilotAccess.enabled.is_(True),
        )
    )
    if pilot is None:
        return "not_eligible"
    mapping_revision = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.status == REVISION_ACTIVE
        )
    )
    if mapping_revision is None:
        return "temporarily_unavailable"
    if mapping_revision.source_name != MANUAL_MAPPING_SOURCE_NAME and current_time - ensure_utc(
        mapping_revision.source_checked_at
    ) > timedelta(seconds=mapping_stale_after_seconds):
        return "temporarily_unavailable"
    mapping = session.scalar(
        select(CustomerSettlementMappingEntry).where(
            CustomerSettlementMappingEntry.revision_id == mapping_revision.id,
            CustomerSettlementMappingEntry.site_user_id == user_id,
        )
    )
    if (
        mapping is None
        or mapping.status != MAPPING_LINKED
        or not mapping.counterparty_ref
        or not mapping.counterparty_guid
        or mapping.customer_account_id is None
        or mapping.source_binding_id is None
    ):
        return "not_eligible"
    account = session.get(CustomerAccount, mapping.customer_account_id)
    source_binding = session.get(CustomerAccountSourceBinding, mapping.source_binding_id)
    if (
        account is None
        or account.status != "active"
        or source_binding is None
        or source_binding.status != "active"
        or source_binding.customer_account_id != mapping.customer_account_id
        or source_binding.counterparty_ref != mapping.counterparty_ref
        or source_binding.counterparty_guid != mapping.counterparty_guid
        or source_binding.organization_guid != mapping.organization_guid
    ):
        return "not_eligible"
    return "eligible"


def get_customer_settlement_summary(
    session: Session,
    *,
    site_user_id: str | int,
    enabled: bool,
    stale_after_seconds: int,
    hide_after_seconds: int,
    mapping_stale_after_seconds: int,
    now: datetime | None = None,
) -> SettlementSummary:
    current_time = ensure_utc(now or utc_now())
    user_id = normalize_site_user_id(site_user_id)
    if not enabled:
        return SettlementSummary(status="pilot_disabled")
    pilot = session.scalar(
        select(CustomerSettlementPilotAccess).where(
            CustomerSettlementPilotAccess.site_user_id == user_id,
            CustomerSettlementPilotAccess.enabled.is_(True),
        )
    )
    if pilot is None:
        return SettlementSummary(status="pilot_disabled")

    mapping_revision = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.status == REVISION_ACTIVE
        )
    )
    if mapping_revision is None:
        return SettlementSummary(status="temporarily_unavailable")
    mapping_checked_at = ensure_utc(mapping_revision.source_checked_at)
    if (
        mapping_revision.source_name != MANUAL_MAPPING_SOURCE_NAME
        and current_time - mapping_checked_at > timedelta(seconds=mapping_stale_after_seconds)
    ):
        return SettlementSummary(status="temporarily_unavailable")
    mapping = session.scalar(
        select(CustomerSettlementMappingEntry).where(
            CustomerSettlementMappingEntry.revision_id == mapping_revision.id,
            CustomerSettlementMappingEntry.site_user_id == user_id,
        )
    )
    if mapping is None or mapping.status == MAPPING_NOT_LINKED:
        return SettlementSummary(status="not_linked")
    if (
        mapping.status == MAPPING_AMBIGUOUS
        or not mapping.counterparty_ref
        or not mapping.counterparty_guid
        or mapping.customer_account_id is None
        or mapping.source_binding_id is None
    ):
        return SettlementSummary(status="ambiguous_link")

    account = session.get(CustomerAccount, mapping.customer_account_id)
    source_binding = session.get(CustomerAccountSourceBinding, mapping.source_binding_id)
    if (
        account is None
        or account.status != "active"
        or source_binding is None
        or source_binding.status != "active"
        or source_binding.customer_account_id != mapping.customer_account_id
        or source_binding.counterparty_guid != mapping.counterparty_guid
        or source_binding.counterparty_ref != mapping.counterparty_ref
        or source_binding.organization_guid != mapping.organization_guid
    ):
        return SettlementSummary(status="ambiguous_link")

    revision = session.scalar(
        select(CustomerSettlementRevision).where(
            CustomerSettlementRevision.status == REVISION_ACTIVE
        )
    )
    if revision is None:
        return SettlementSummary(status="temporarily_unavailable")
    if revision.organization_guid != mapping.organization_guid:
        return SettlementSummary(status="temporarily_unavailable")
    balance = session.scalar(
        select(CustomerSettlementBalance).where(
            CustomerSettlementBalance.revision_id == revision.id,
            CustomerSettlementBalance.counterparty_guid == mapping.counterparty_guid,
        )
    )
    if balance is None:
        return SettlementSummary(status="temporarily_unavailable")

    synced_at = ensure_utc(revision.synced_at)
    age = current_time - synced_at
    if age > timedelta(seconds=hide_after_seconds):
        return SettlementSummary(status="temporarily_unavailable")
    state, amount = settlement_state(Decimal(balance.signed_balance))
    is_stale = age > timedelta(seconds=stale_after_seconds)
    return SettlementSummary(
        status="stale" if is_stale else "available",
        state=state,
        amount=amount,
        currency="RUB",
        as_of=ensure_utc(revision.as_of),
        synced_at=synced_at,
        is_stale=is_stale,
    )


def customer_settlement_health_metrics(
    session: Session,
    *,
    stale_after_seconds: int,
    hide_after_seconds: int,
    mapping_stale_after_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = ensure_utc(now or utc_now())
    financial = session.scalar(
        select(CustomerSettlementRevision).where(
            CustomerSettlementRevision.status == REVISION_ACTIVE
        )
    )
    mapping = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.status == REVISION_ACTIVE
        )
    )
    financial_age = (
        max(0, int((current_time - ensure_utc(financial.synced_at)).total_seconds()))
        if financial is not None
        else None
    )
    mapping_age = (
        max(0, int((current_time - ensure_utc(mapping.source_checked_at)).total_seconds()))
        if mapping is not None
        else None
    )
    if financial_age is None or financial_age > hide_after_seconds:
        freshness_status = "critical"
    elif financial_age > stale_after_seconds:
        freshness_status = "warning"
    else:
        freshness_status = "ok"
    mapping_status = "critical"
    if mapping is not None and (
        mapping.source_name == MANUAL_MAPPING_SOURCE_NAME
        or (mapping_age is not None and mapping_age <= mapping_stale_after_seconds)
    ):
        mapping_status = "ok"
    return {
        "freshness_status": freshness_status,
        "mapping_status": mapping_status,
        "financial_age_seconds": financial_age,
        "mapping_age_seconds": mapping_age,
        "expected_rows": financial.expected_row_count if financial else 0,
        "loaded_rows": financial.loaded_row_count if financial else 0,
        "zero_rows": financial.zero_row_count if financial else 0,
        "mapping_entries": mapping.loaded_entry_count if mapping else 0,
        "ambiguous_entries": mapping.ambiguous_count if mapping else 0,
    }


def cleanup_customer_settlements(
    session: Session,
    *,
    successful_retention_days: int,
    failed_retention_days: int,
    jti_retention_hours: int,
    now: datetime | None = None,
) -> dict[str, int]:
    current_time = ensure_utc(now or utc_now())
    success_cutoff = current_time - timedelta(days=successful_retention_days)
    failed_cutoff = current_time - timedelta(days=failed_retention_days)
    jti_cutoff = current_time - timedelta(hours=jti_retention_hours)

    financial_ids = tuple(
        session.execute(
            select(CustomerSettlementRevision.id).where(
                (
                    (CustomerSettlementRevision.status == REVISION_SUPERSEDED)
                    & (CustomerSettlementRevision.created_at < success_cutoff)
                )
                | (
                    CustomerSettlementRevision.status.in_(("failed", "loading"))
                    & (CustomerSettlementRevision.created_at < failed_cutoff)
                )
            )
        ).scalars()
    )
    mapping_ids = tuple(
        session.execute(
            select(CustomerSettlementMappingRevision.id).where(
                (
                    (CustomerSettlementMappingRevision.status == REVISION_SUPERSEDED)
                    & (CustomerSettlementMappingRevision.created_at < success_cutoff)
                )
                | (
                    CustomerSettlementMappingRevision.status.in_(("failed", "loading"))
                    & (CustomerSettlementMappingRevision.created_at < failed_cutoff)
                )
            )
        ).scalars()
    )
    balance_count = 0
    mapping_entry_count = 0
    if financial_ids:
        balance_count = (
            session.execute(
                delete(CustomerSettlementBalance).where(
                    CustomerSettlementBalance.revision_id.in_(financial_ids)
                )
            ).rowcount
            or 0
        )
    if mapping_ids:
        mapping_entry_count = (
            session.execute(
                delete(CustomerSettlementMappingEntry).where(
                    CustomerSettlementMappingEntry.revision_id.in_(mapping_ids)
                )
            ).rowcount
            or 0
        )
    financial_count = 0
    if financial_ids:
        financial_count = (
            session.execute(
                delete(CustomerSettlementRevision).where(
                    CustomerSettlementRevision.id.in_(financial_ids)
                )
            ).rowcount
            or 0
        )
    mapping_count = 0
    if mapping_ids:
        mapping_count = (
            session.execute(
                delete(CustomerSettlementMappingRevision).where(
                    CustomerSettlementMappingRevision.id.in_(mapping_ids)
                )
            ).rowcount
            or 0
        )
    jti_count = (
        session.execute(
            delete(CustomerSettlementAssertionJti).where(
                CustomerSettlementAssertionJti.expires_at < jti_cutoff
            )
        ).rowcount
        or 0
    )
    reconciliation_count = (
        session.execute(
            delete(CustomerSettlementReconciliationRun).where(
                (
                    CustomerSettlementReconciliationRun.status.in_(("matched", "mismatched"))
                    & (CustomerSettlementReconciliationRun.created_at < success_cutoff)
                )
                | (
                    (CustomerSettlementReconciliationRun.status == "blocked")
                    & (CustomerSettlementReconciliationRun.created_at < failed_cutoff)
                )
            )
        ).rowcount
        or 0
    )
    alert_outbox_count = (
        session.execute(
            delete(CustomerSettlementAlertOutbox).where(
                (
                    CustomerSettlementAlertOutbox.status.in_(("sent", "pending"))
                    & (CustomerSettlementAlertOutbox.created_at < success_cutoff)
                )
                | (
                    (CustomerSettlementAlertOutbox.status == "failed")
                    & (CustomerSettlementAlertOutbox.updated_at < failed_cutoff)
                )
            )
        ).rowcount
        or 0
    )
    session.flush()
    return {
        "financial_revisions": financial_count,
        "financial_balances": balance_count,
        "mapping_revisions": mapping_count,
        "mapping_entries": mapping_entry_count,
        "assertion_jti": jti_count,
        "reconciliation_runs": reconciliation_count,
        "alert_outbox": alert_outbox_count,
    }
