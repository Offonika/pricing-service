from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Literal, Sequence

from sqlalchemy import delete, distinct, func, select, text, update
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
CUSTOMER_SETTLEMENT_CONTEXT_LOCK = "customer-settlements:context"
CUSTOMER_SETTLEMENT_STALE_AFTER_SECONDS = 7200
CUSTOMER_SETTLEMENT_HIDE_AFTER_SECONDS = 21600
CUSTOMER_SETTLEMENT_MAPPING_STALE_AFTER_SECONDS = 7200


class CustomerSettlementRuntimeGuardError(RuntimeError):
    pass


class CustomerSettlementContextBusyError(RuntimeError):
    pass


def validate_customer_settlement_freshness_contract(
    *,
    stale_after_seconds: int,
    hide_after_seconds: int,
    mapping_stale_after_seconds: int,
) -> None:
    if (
        stale_after_seconds != CUSTOMER_SETTLEMENT_STALE_AFTER_SECONDS
        or hide_after_seconds != CUSTOMER_SETTLEMENT_HIDE_AFTER_SECONDS
        or mapping_stale_after_seconds != CUSTOMER_SETTLEMENT_MAPPING_STALE_AFTER_SECONDS
    ):
        raise CustomerSettlementRuntimeGuardError("customer_settlement_freshness_contract_invalid")


def _advisory_lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


def try_customer_settlement_context_lock(session: Session) -> bool:
    """Hold the exclusive settlement context lock until the transaction ends."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key(CUSTOMER_SETTLEMENT_CONTEXT_LOCK)},
        ).scalar()
    )


def try_customer_settlement_context_read_lock(session: Session) -> bool:
    """Hold a shared read lock while observing the active settlement context."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
            {"key": _advisory_lock_key(CUSTOMER_SETTLEMENT_CONTEXT_LOCK)},
        ).scalar()
    )


def _require_customer_settlement_context_lock(session: Session) -> None:
    if not try_customer_settlement_context_lock(session):
        raise CustomerSettlementContextBusyError("customer_settlement_context_busy")


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


def assert_expected_application_database(
    session: Session,
    *,
    expected_database_name: str | None,
) -> None:
    """Fail closed before a settlement job can touch an unexpected database."""

    expected = str(expected_database_name or "").strip()
    if not expected:
        raise CustomerSettlementRuntimeGuardError("runtime_database_guard_failed")
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        raise CustomerSettlementRuntimeGuardError("runtime_database_guard_failed")
    current = session.scalar(text("SELECT current_database()"))
    if not isinstance(current, str) or current != expected:
        raise CustomerSettlementRuntimeGuardError("runtime_database_guard_failed")


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


def _ref_guid_pair_is_canonical(counterparty_ref: str | None, guid: str | None) -> bool:
    if not counterparty_ref or not guid:
        return False
    raw_ref = str(counterparty_ref)
    raw_guid = str(guid)
    try:
        normalized_ref = normalize_counterparty_ref(raw_ref)
        normalized_guid = normalize_guid(raw_guid)
    except ValueError:
        return False
    return (
        raw_ref == normalized_ref
        and raw_guid == normalized_guid
        and onec_guid_to_ref(normalized_guid) == normalized_ref
    )


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
    candidate = Decimal(str(value))
    if not candidate.is_finite():
        raise ValueError("money value must be finite")
    normalized = candidate.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
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
    _require_customer_settlement_context_lock(session)
    organization = normalize_counterparty_ref(organization_ref)
    normalized_organization_guid = normalize_guid(
        organization_guid or onec_ref_to_guid(organization)
    )
    if onec_guid_to_ref(normalized_organization_guid) != organization:
        raise ValueError("organization_guid_does_not_match_ref")
    expected_refs = {normalize_counterparty_ref(value) for value in expected_counterparty_refs}
    if not expected_refs:
        raise ValueError("financial_revision_scope_is_empty")
    if len(expected_refs) > MAX_PILOT_USERS:
        raise ValueError("financial_revision_scope_limit_exceeded")
    normalized_rows = _normalized_balance_rows(balances)
    loaded_refs = {item.counterparty_ref for item in normalized_rows}
    if expected_refs != loaded_refs:
        raise ValueError("incomplete_financial_revision")

    as_of_utc = ensure_utc(as_of)
    source_db_time_utc = ensure_utc(source_db_time)
    synced_at_utc = ensure_utc(synced_at or utc_now())
    activation_time = utc_now()
    if as_of_utc > source_db_time_utc:
        raise ValueError("financial_revision_as_of_is_in_the_future")
    if (
        source_db_time_utc > synced_at_utc + timedelta(seconds=30)
        or source_db_time_utc > activation_time + timedelta(seconds=30)
        or synced_at_utc > activation_time + timedelta(seconds=30)
    ):
        raise ValueError("financial_revision_source_time_is_in_the_future")
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
        stored_rows = tuple(
            session.scalars(
                select(CustomerSettlementBalance).where(
                    CustomerSettlementBalance.revision_id == existing.id
                )
            )
        )
        stored_by_ref = {item.counterparty_ref: item for item in stored_rows}
        expected_by_ref = {item.counterparty_ref: item for item in normalized_rows}
        stored_zero_count = sum(
            1 for item in stored_rows if normalize_money(item.signed_balance) == 0
        )
        payload_matches = (
            existing.status in {REVISION_ACTIVE, REVISION_SUPERSEDED}
            and existing.organization_ref == organization
            and existing.organization_guid == normalized_organization_guid
            and existing.currency == "RUB"
            and ensure_utc(existing.as_of) == as_of_utc
            and ensure_utc(existing.source_db_time) == source_db_time_utc
            and existing.source_mode == source_mode
            and existing.expected_row_count == len(expected_refs)
            and existing.loaded_row_count == len(normalized_rows)
            and existing.zero_row_count == stored_zero_count
            and len(stored_by_ref) == len(stored_rows) == len(expected_by_ref)
            and all(
                stored_by_ref[ref].counterparty_guid == str(item.counterparty_guid)
                and normalize_money(stored_by_ref[ref].signed_balance) == item.signed_balance
                and stored_by_ref[ref].currency == "RUB"
                for ref, item in expected_by_ref.items()
                if ref in stored_by_ref
            )
            and set(stored_by_ref) == set(expected_by_ref)
        )
        if not payload_matches:
            raise ValueError("financial_revision_payload_mismatch")
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
    desired_identity_by_site_user: dict[str, tuple[str, str, str]],
    new_source_binding_ids: set[int],
) -> tuple[int, int, str, bool]:
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
    desired_identity = (source_system, organization_guid, counterparty_guid)
    if (
        site_binding is not None
        and identity_binding is not None
        and site_binding.customer_account_id != identity_binding.customer_account_id
    ):
        if identity_binding.id not in new_source_binding_ids:
            raise ValueError("durable_customer_account_mapping_conflict")
        site_binding.status = "revoked"
        site_binding.valid_to = checked_at
        site_binding.updated_at = checked_at
        session.flush()
        site_binding = None
    if site_binding is not None and identity_binding is None:
        current_account_source = session.scalar(
            select(CustomerAccountSourceBinding).where(
                CustomerAccountSourceBinding.customer_account_id
                == site_binding.customer_account_id,
                CustomerAccountSourceBinding.source_system == source_system,
                CustomerAccountSourceBinding.organization_guid == organization_guid,
                CustomerAccountSourceBinding.status == "active",
            )
        )
        if (
            current_account_source is not None
            and current_account_source.counterparty_guid != counterparty_guid
        ):
            other_active_site_bindings = tuple(
                session.scalars(
                    select(CustomerAccountSiteBinding).where(
                        CustomerAccountSiteBinding.customer_account_id
                        == site_binding.customer_account_id,
                        CustomerAccountSiteBinding.status == "active",
                        CustomerAccountSiteBinding.id != site_binding.id,
                    )
                )
            )
            if any(
                desired_identity_by_site_user.get(binding.site_user_id) != desired_identity
                for binding in other_active_site_bindings
            ):
                site_binding.status = "revoked"
                site_binding.valid_to = checked_at
                site_binding.updated_at = checked_at
                session.flush()
                site_binding = None
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
    source_binding_created = False
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
        source_binding_created = True
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
    return account_id, source_binding.id, counterparty_guid, source_binding_created


def _revoke_site_bindings_outside_linked_mapping(
    session: Session,
    *,
    linked_site_user_ids: set[str],
    checked_at: datetime,
) -> None:
    statement = select(CustomerAccountSiteBinding).where(
        CustomerAccountSiteBinding.site_code == DEFAULT_SITE_CODE,
        CustomerAccountSiteBinding.status == "active",
    )
    if linked_site_user_ids:
        statement = statement.where(
            CustomerAccountSiteBinding.site_user_id.not_in(linked_site_user_ids)
        )
    for binding in session.scalars(statement):
        binding.status = "revoked"
        binding.valid_to = checked_at
        binding.updated_at = checked_at
    session.flush()


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
    _require_customer_settlement_context_lock(session)
    checked_at = ensure_utc(source_checked_at or utc_now())
    if checked_at > utc_now() + timedelta(seconds=30):
        raise ValueError("mapping_revision_source_time_is_in_the_future")
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
    if not normalized_entries:
        raise ValueError("mapping_revision_scope_is_empty")
    if len(normalized_entries) > MAX_PILOT_USERS:
        raise ValueError("mapping_revision_scope_limit_exceeded")
    desired_identity_by_site_user = {
        item.site_user_id: (
            normalized_source_system,
            normalized_organization_guid,
            str(item.counterparty_guid),
        )
        for item in normalized_entries
        if item.status == MAPPING_LINKED
    }
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
        new_source_binding_ids: set[int] = set()
        existing_entries = {
            row.site_user_id: row
            for row in session.scalars(
                select(CustomerSettlementMappingEntry).where(
                    CustomerSettlementMappingEntry.revision_id == existing.id
                )
            )
        }
        expected_entries = {item.site_user_id: item for item in normalized_entries}
        payload_matches = (
            existing.status in {REVISION_ACTIVE, REVISION_SUPERSEDED}
            and existing.source_name == normalized_source_name
            and existing.expected_entry_count == len(normalized_entries)
            and existing.loaded_entry_count == len(normalized_entries)
            and existing.ambiguous_count
            == sum(1 for item in normalized_entries if item.status == MAPPING_AMBIGUOUS)
            and set(existing_entries) == set(expected_entries)
            and all(
                existing_entries[user_id].cluster_id == item.cluster_id
                and existing_entries[user_id].counterparty_ref == item.counterparty_ref
                and existing_entries[user_id].counterparty_guid == item.counterparty_guid
                and existing_entries[user_id].status == item.status
                and existing_entries[user_id].source_system
                == (normalized_source_system if item.status == MAPPING_LINKED else None)
                and existing_entries[user_id].organization_guid
                == (normalized_organization_guid if item.status == MAPPING_LINKED else None)
                for user_id, item in expected_entries.items()
            )
        )
        if not payload_matches:
            raise ValueError("mapping_revision_payload_mismatch")
        _revoke_site_bindings_outside_linked_mapping(
            session,
            linked_site_user_ids={
                item.site_user_id for item in normalized_entries if item.status == MAPPING_LINKED
            },
            checked_at=checked_at,
        )
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
        for item in normalized_entries:
            row = existing_entries.get(item.site_user_id)
            if row is None:
                raise ValueError("mapping_revision_payload_mismatch")
            row.source_updated_at = item.source_updated_at
            if item.status != MAPPING_LINKED:
                continue
            (
                account_id,
                source_binding_id,
                counterparty_guid,
                source_binding_created,
            ) = _materialize_linked_customer_account(
                session,
                revision_id=existing.id,
                item=item,
                checked_at=checked_at,
                source_system=normalized_source_system,
                organization_ref=normalized_organization_ref,
                organization_guid=normalized_organization_guid,
                desired_identity_by_site_user=desired_identity_by_site_user,
                new_source_binding_ids=new_source_binding_ids,
            )
            if source_binding_created:
                new_source_binding_ids.add(source_binding_id)
            row.customer_account_id = account_id
            row.source_binding_id = source_binding_id
            row.counterparty_guid = counterparty_guid
            row.source_system = normalized_source_system
            row.organization_guid = normalized_organization_guid
        session.flush()
        return existing, False

    _revoke_site_bindings_outside_linked_mapping(
        session,
        linked_site_user_ids={
            item.site_user_id for item in normalized_entries if item.status == MAPPING_LINKED
        },
        checked_at=checked_at,
    )
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
    new_source_binding_ids: set[int] = set()
    for item in normalized_entries:
        account_id = None
        source_binding_id = None
        counterparty_guid = item.counterparty_guid
        if item.status == MAPPING_LINKED:
            (
                account_id,
                source_binding_id,
                counterparty_guid,
                source_binding_created,
            ) = _materialize_linked_customer_account(
                session,
                revision_id=revision.id,
                item=item,
                checked_at=checked_at,
                source_system=normalized_source_system,
                organization_ref=normalized_organization_ref,
                organization_guid=normalized_organization_guid,
                desired_identity_by_site_user=desired_identity_by_site_user,
                new_source_binding_ids=new_source_binding_ids,
            )
            if source_binding_created:
                new_source_binding_ids.add(source_binding_id)
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
    _require_customer_settlement_context_lock(session)
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
            CustomerAccountSiteBinding,
            CustomerAccountSiteBinding.customer_account_id
            == CustomerSettlementMappingEntry.customer_account_id,
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
            CustomerAccountSourceBinding.customer_account_id
            == CustomerSettlementMappingEntry.customer_account_id,
            CustomerAccountSourceBinding.counterparty_guid
            == CustomerSettlementMappingEntry.counterparty_guid,
            CustomerAccountSourceBinding.counterparty_ref
            == CustomerSettlementMappingEntry.counterparty_ref,
            CustomerAccountSourceBinding.source_system
            == CustomerSettlementMappingEntry.source_system,
            CustomerAccountSourceBinding.organization_guid
            == CustomerSettlementMappingEntry.organization_guid,
            CustomerAccountSourceBinding.mapping_revision_id == mapping_revision.id,
            CustomerAccountSiteBinding.site_code == DEFAULT_SITE_CODE,
            CustomerAccountSiteBinding.site_user_id == CustomerSettlementMappingEntry.site_user_id,
            CustomerAccountSiteBinding.status == "active",
            CustomerAccountSiteBinding.cluster_id == CustomerSettlementMappingEntry.cluster_id,
            CustomerAccountSiteBinding.mapping_revision_id == mapping_revision.id,
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


def _mapping_revision_is_complete(
    session: Session,
    *,
    revision: CustomerSettlementMappingRevision,
) -> bool:
    rows = tuple(
        session.execute(
            select(
                CustomerSettlementMappingEntry.site_user_id,
                CustomerSettlementMappingEntry.status,
            ).where(CustomerSettlementMappingEntry.revision_id == revision.id)
        )
    )
    mapping_user_ids: list[str] = []
    ambiguous_count = 0
    for site_user_id, status in rows:
        raw_user_id = str(site_user_id)
        try:
            normalized_user_id = normalize_site_user_id(raw_user_id)
        except ValueError:
            return False
        if raw_user_id != normalized_user_id or status not in {
            MAPPING_LINKED,
            MAPPING_NOT_LINKED,
            MAPPING_AMBIGUOUS,
        }:
            return False
        mapping_user_ids.append(normalized_user_id)
        if status == MAPPING_AMBIGUOUS:
            ambiguous_count += 1
    return bool(
        0 < len(rows) <= MAX_PILOT_USERS
        and len(set(mapping_user_ids)) == len(mapping_user_ids)
        and revision.expected_entry_count == len(rows)
        and revision.loaded_entry_count == len(rows)
        and revision.ambiguous_count == ambiguous_count
    )


def _financial_revision_scope_snapshot(
    session: Session,
    *,
    revision_id: int,
) -> tuple[tuple[str, ...], int, bool]:
    rows = tuple(
        session.execute(
            select(
                CustomerSettlementBalance.counterparty_ref,
                CustomerSettlementBalance.counterparty_guid,
                CustomerSettlementBalance.currency,
                CustomerSettlementBalance.signed_balance,
            ).where(CustomerSettlementBalance.revision_id == revision_id)
        )
    )
    refs: list[str] = []
    zero_count = 0
    rows_are_valid = True
    for counterparty_ref, counterparty_guid, currency, signed_balance in rows:
        raw_ref = str(counterparty_ref)
        raw_guid = str(counterparty_guid)
        try:
            normalized_ref = normalize_counterparty_ref(raw_ref)
            normalized_guid = normalize_guid(raw_guid)
            normalized_balance = normalize_money(signed_balance)
        except (ArithmeticError, TypeError, ValueError):
            rows_are_valid = False
            refs.append(raw_ref)
            continue
        if (
            raw_ref != normalized_ref
            or raw_guid != normalized_guid
            or onec_guid_to_ref(normalized_guid) != normalized_ref
            or currency != "RUB"
        ):
            rows_are_valid = False
        refs.append(normalized_ref)
        if normalized_balance == 0:
            zero_count += 1
    return tuple(sorted(refs)), zero_count, rows_are_valid


def get_customer_settlement_eligibility(
    session: Session,
    *,
    site_user_id: str | int,
    enabled: bool,
    source_validated: bool = True,
    reconciliation_validated: bool = True,
    mapping_stale_after_seconds: int,
    expected_mapping_source_name: str | None = None,
    expected_source_system: str | None = None,
    expected_organization_ref: str | None = None,
    expected_organization_guid: str | None = None,
    now: datetime | None = None,
) -> EligibilityStatus:
    if not enabled:
        return "not_eligible"
    if not source_validated:
        return "temporarily_unavailable"
    if not reconciliation_validated:
        return "temporarily_unavailable"
    if not try_customer_settlement_context_read_lock(session):
        return "temporarily_unavailable"
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
    if (
        expected_mapping_source_name is not None
        and mapping_revision.source_name != expected_mapping_source_name
    ):
        return "temporarily_unavailable"
    mapping_age = current_time - ensure_utc(mapping_revision.source_checked_at)
    if mapping_age < timedelta(0) or (
        mapping_revision.source_name != MANUAL_MAPPING_SOURCE_NAME
        and mapping_age > timedelta(seconds=mapping_stale_after_seconds)
    ):
        return "temporarily_unavailable"
    if not _mapping_revision_is_complete(session, revision=mapping_revision):
        return "temporarily_unavailable"
    mapping = session.scalar(
        select(CustomerSettlementMappingEntry).where(
            CustomerSettlementMappingEntry.revision_id == mapping_revision.id,
            CustomerSettlementMappingEntry.site_user_id == user_id,
        )
    )
    if mapping is None:
        return "temporarily_unavailable"
    if (
        mapping.status != MAPPING_LINKED
        or not mapping.counterparty_ref
        or not mapping.counterparty_guid
        or mapping.customer_account_id is None
        or mapping.source_binding_id is None
    ):
        return "not_eligible"
    account = session.get(CustomerAccount, mapping.customer_account_id)
    source_binding = session.get(CustomerAccountSourceBinding, mapping.source_binding_id)
    site_binding = session.scalar(
        select(CustomerAccountSiteBinding).where(
            CustomerAccountSiteBinding.customer_account_id == mapping.customer_account_id,
            CustomerAccountSiteBinding.site_code == DEFAULT_SITE_CODE,
            CustomerAccountSiteBinding.site_user_id == user_id,
            CustomerAccountSiteBinding.status == "active",
        )
    )
    if (
        account is None
        or account.status != "active"
        or source_binding is None
        or source_binding.status != "active"
        or source_binding.customer_account_id != mapping.customer_account_id
        or source_binding.source_system != mapping.source_system
        or source_binding.counterparty_ref != mapping.counterparty_ref
        or source_binding.counterparty_guid != mapping.counterparty_guid
        or source_binding.organization_guid != mapping.organization_guid
        or source_binding.mapping_revision_id != mapping_revision.id
        or (expected_source_system is not None and mapping.source_system != expected_source_system)
        or (
            expected_organization_ref is not None
            and source_binding.organization_ref != expected_organization_ref
        )
        or (
            expected_organization_guid is not None
            and mapping.organization_guid != expected_organization_guid
        )
        or not _ref_guid_pair_is_canonical(
            mapping.counterparty_ref,
            mapping.counterparty_guid,
        )
        or not _ref_guid_pair_is_canonical(
            source_binding.organization_ref,
            source_binding.organization_guid,
        )
        or site_binding is None
        or site_binding.cluster_id != mapping.cluster_id
        or site_binding.mapping_revision_id != mapping_revision.id
    ):
        return "not_eligible"
    return "eligible"


def get_customer_settlement_summary(
    session: Session,
    *,
    site_user_id: str | int,
    enabled: bool,
    source_validated: bool = True,
    reconciliation_validated: bool = True,
    stale_after_seconds: int,
    hide_after_seconds: int,
    mapping_stale_after_seconds: int,
    expected_source_mode: str | None = None,
    expected_mapping_source_name: str | None = None,
    expected_source_system: str | None = None,
    expected_organization_ref: str | None = None,
    expected_organization_guid: str | None = None,
    now: datetime | None = None,
) -> SettlementSummary:
    current_time = ensure_utc(now or utc_now())
    user_id = normalize_site_user_id(site_user_id)
    if not enabled:
        return SettlementSummary(status="pilot_disabled")
    if not source_validated:
        return SettlementSummary(status="temporarily_unavailable")
    if not reconciliation_validated:
        return SettlementSummary(status="temporarily_unavailable")
    if not try_customer_settlement_context_read_lock(session):
        return SettlementSummary(status="temporarily_unavailable")
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
    if (
        expected_mapping_source_name is not None
        and mapping_revision.source_name != expected_mapping_source_name
    ):
        return SettlementSummary(status="temporarily_unavailable")
    mapping_checked_at = ensure_utc(mapping_revision.source_checked_at)
    mapping_age = current_time - mapping_checked_at
    if mapping_age < timedelta(0) or (
        mapping_revision.source_name != MANUAL_MAPPING_SOURCE_NAME
        and mapping_age > timedelta(seconds=mapping_stale_after_seconds)
    ):
        return SettlementSummary(status="temporarily_unavailable")
    if not _mapping_revision_is_complete(session, revision=mapping_revision):
        return SettlementSummary(status="temporarily_unavailable")
    mapping = session.scalar(
        select(CustomerSettlementMappingEntry).where(
            CustomerSettlementMappingEntry.revision_id == mapping_revision.id,
            CustomerSettlementMappingEntry.site_user_id == user_id,
        )
    )
    if mapping is None:
        return SettlementSummary(status="temporarily_unavailable")
    if mapping.status == MAPPING_NOT_LINKED:
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
    site_binding = session.scalar(
        select(CustomerAccountSiteBinding).where(
            CustomerAccountSiteBinding.customer_account_id == mapping.customer_account_id,
            CustomerAccountSiteBinding.site_code == DEFAULT_SITE_CODE,
            CustomerAccountSiteBinding.site_user_id == user_id,
            CustomerAccountSiteBinding.status == "active",
        )
    )
    if (
        account is None
        or account.status != "active"
        or source_binding is None
        or source_binding.status != "active"
        or source_binding.customer_account_id != mapping.customer_account_id
        or source_binding.source_system != mapping.source_system
        or source_binding.counterparty_guid != mapping.counterparty_guid
        or source_binding.counterparty_ref != mapping.counterparty_ref
        or source_binding.organization_guid != mapping.organization_guid
        or source_binding.mapping_revision_id != mapping_revision.id
        or (expected_source_system is not None and mapping.source_system != expected_source_system)
        or (
            expected_organization_ref is not None
            and source_binding.organization_ref != expected_organization_ref
        )
        or (
            expected_organization_guid is not None
            and mapping.organization_guid != expected_organization_guid
        )
        or not _ref_guid_pair_is_canonical(
            mapping.counterparty_ref,
            mapping.counterparty_guid,
        )
        or not _ref_guid_pair_is_canonical(
            source_binding.organization_ref,
            source_binding.organization_guid,
        )
        or site_binding is None
        or site_binding.cluster_id != mapping.cluster_id
        or site_binding.mapping_revision_id != mapping_revision.id
    ):
        return SettlementSummary(status="ambiguous_link")

    revision = session.scalar(
        select(CustomerSettlementRevision).where(
            CustomerSettlementRevision.status == REVISION_ACTIVE
        )
    )
    if revision is None:
        return SettlementSummary(status="temporarily_unavailable")
    revision_as_of = ensure_utc(revision.as_of)
    revision_source_db_time = ensure_utc(revision.source_db_time)
    if (
        revision.organization_guid != mapping.organization_guid
        or revision.organization_ref != source_binding.organization_ref
        or revision.currency != "RUB"
        or revision_as_of > revision_source_db_time
        or revision_as_of > current_time + timedelta(seconds=30)
        or revision_source_db_time > current_time + timedelta(seconds=30)
        or (expected_source_mode is not None and revision.source_mode != expected_source_mode)
        or (
            expected_organization_ref is not None
            and revision.organization_ref != expected_organization_ref
        )
        or (
            expected_organization_guid is not None
            and revision.organization_guid != expected_organization_guid
        )
        or not _ref_guid_pair_is_canonical(
            revision.organization_ref,
            revision.organization_guid,
        )
    ):
        return SettlementSummary(status="temporarily_unavailable")
    pilot_counterparty_refs = active_pilot_counterparty_refs(session)
    financial_counterparty_refs, actual_zero_rows, financial_rows_are_valid = (
        _financial_revision_scope_snapshot(session, revision_id=revision.id)
    )
    if (
        not pilot_counterparty_refs
        or revision.expected_row_count != len(pilot_counterparty_refs)
        or revision.loaded_row_count != len(pilot_counterparty_refs)
        or revision.zero_row_count != actual_zero_rows
        or financial_counterparty_refs != pilot_counterparty_refs
        or not financial_rows_are_valid
    ):
        return SettlementSummary(status="temporarily_unavailable")
    balance = session.scalar(
        select(CustomerSettlementBalance).where(
            CustomerSettlementBalance.revision_id == revision.id,
            CustomerSettlementBalance.counterparty_guid == mapping.counterparty_guid,
            CustomerSettlementBalance.counterparty_ref == mapping.counterparty_ref,
            CustomerSettlementBalance.currency == "RUB",
        )
    )
    if balance is None:
        return SettlementSummary(status="temporarily_unavailable")

    synced_at = ensure_utc(revision.synced_at)
    age = current_time - synced_at
    if age < timedelta(0):
        return SettlementSummary(status="temporarily_unavailable")
    if age > timedelta(seconds=hide_after_seconds):
        return SettlementSummary(status="temporarily_unavailable")
    state, amount = settlement_state(Decimal(balance.signed_balance))
    is_stale = age >= timedelta(seconds=stale_after_seconds)
    return SettlementSummary(
        status="stale" if is_stale else "available",
        state=state,
        amount=amount,
        currency="RUB",
        as_of=revision_as_of,
        synced_at=synced_at,
        is_stale=is_stale,
    )


def customer_settlement_health_metrics(
    session: Session,
    *,
    stale_after_seconds: int,
    hide_after_seconds: int,
    mapping_stale_after_seconds: int,
    expected_source_mode: str | None = None,
    expected_mapping_source_name: str | None = None,
    expected_source_system: str | None = None,
    expected_organization_ref: str | None = None,
    expected_organization_guid: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = ensure_utc(now or utc_now())
    if not try_customer_settlement_context_read_lock(session):
        return {
            "freshness_status": "critical",
            "mapping_status": "critical",
            "financial_age_seconds": None,
            "mapping_age_seconds": None,
            "expected_rows": 0,
            "loaded_rows": 0,
            "zero_rows": 0,
            "enabled_pilots": 0,
            "linked_pilots": 0,
            "pilot_counterparties": 0,
            "compatible_pilots": 0,
            "mapping_entries": 0,
            "ambiguous_entries": 0,
            "context_stable": False,
        }
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
    financial_age_delta = (
        current_time - ensure_utc(financial.synced_at) if financial is not None else None
    )
    mapping_age_delta = (
        current_time - ensure_utc(mapping.source_checked_at) if mapping is not None else None
    )
    financial_age_raw = (
        int(financial_age_delta.total_seconds()) if financial_age_delta is not None else None
    )
    mapping_age_raw = (
        int(mapping_age_delta.total_seconds()) if mapping_age_delta is not None else None
    )
    financial_age = max(0, financial_age_raw) if financial_age_raw is not None else None
    mapping_age = max(0, mapping_age_raw) if mapping_age_raw is not None else None
    enabled_pilots = (
        session.scalar(
            select(func.count())
            .select_from(CustomerSettlementPilotAccess)
            .where(CustomerSettlementPilotAccess.enabled.is_(True))
        )
        or 0
    )
    pilot_counterparty_refs = active_pilot_counterparty_refs(session)
    pilot_counterparties = len(pilot_counterparty_refs)
    financial_counterparty_refs: tuple[str, ...] = ()
    actual_zero_rows = 0
    financial_rows_are_valid = False
    if financial is not None:
        financial_counterparty_refs, actual_zero_rows, financial_rows_are_valid = (
            _financial_revision_scope_snapshot(session, revision_id=financial.id)
        )
    mapping_entries = 0
    ambiguous_entries = 0
    linked_pilots = 0
    compatible_pilots = 0
    if mapping is not None:
        mapping_entries = (
            session.scalar(
                select(func.count())
                .select_from(CustomerSettlementMappingEntry)
                .where(CustomerSettlementMappingEntry.revision_id == mapping.id)
            )
            or 0
        )
        ambiguous_entries = (
            session.scalar(
                select(func.count())
                .select_from(CustomerSettlementMappingEntry)
                .where(
                    CustomerSettlementMappingEntry.revision_id == mapping.id,
                    CustomerSettlementMappingEntry.status == MAPPING_AMBIGUOUS,
                )
            )
            or 0
        )
        linked_conditions = [
            CustomerSettlementPilotAccess.enabled.is_(True),
            CustomerSettlementMappingEntry.revision_id == mapping.id,
            CustomerSettlementMappingEntry.status == MAPPING_LINKED,
            CustomerAccount.status == "active",
            CustomerAccountSourceBinding.status == "active",
            CustomerAccountSourceBinding.customer_account_id
            == CustomerSettlementMappingEntry.customer_account_id,
            CustomerAccountSourceBinding.counterparty_ref
            == CustomerSettlementMappingEntry.counterparty_ref,
            CustomerAccountSourceBinding.counterparty_guid
            == CustomerSettlementMappingEntry.counterparty_guid,
            CustomerAccountSourceBinding.source_system
            == CustomerSettlementMappingEntry.source_system,
            CustomerAccountSourceBinding.organization_guid
            == CustomerSettlementMappingEntry.organization_guid,
            CustomerAccountSourceBinding.mapping_revision_id == mapping.id,
            CustomerAccountSiteBinding.site_code == DEFAULT_SITE_CODE,
            CustomerAccountSiteBinding.site_user_id == CustomerSettlementMappingEntry.site_user_id,
            CustomerAccountSiteBinding.status == "active",
            CustomerAccountSiteBinding.cluster_id == CustomerSettlementMappingEntry.cluster_id,
            CustomerAccountSiteBinding.mapping_revision_id == mapping.id,
        ]
        if expected_source_system is not None:
            linked_conditions.append(
                CustomerSettlementMappingEntry.source_system == expected_source_system
            )
        if expected_organization_guid is not None:
            linked_conditions.append(
                CustomerSettlementMappingEntry.organization_guid == expected_organization_guid
            )
        if expected_organization_ref is not None:
            linked_conditions.append(
                CustomerAccountSourceBinding.organization_ref == expected_organization_ref
            )
        linked_pilots = (
            session.scalar(
                select(func.count(distinct(CustomerSettlementPilotAccess.site_user_id)))
                .select_from(CustomerSettlementPilotAccess)
                .join(
                    CustomerSettlementMappingEntry,
                    CustomerSettlementMappingEntry.site_user_id
                    == CustomerSettlementPilotAccess.site_user_id,
                )
                .join(
                    CustomerAccountSourceBinding,
                    CustomerAccountSourceBinding.id
                    == CustomerSettlementMappingEntry.source_binding_id,
                )
                .join(
                    CustomerAccountSiteBinding,
                    CustomerAccountSiteBinding.customer_account_id
                    == CustomerSettlementMappingEntry.customer_account_id,
                )
                .join(
                    CustomerAccount,
                    CustomerAccount.id == CustomerSettlementMappingEntry.customer_account_id,
                )
                .where(*linked_conditions)
            )
            or 0
        )
        if financial is not None:
            compatible_conditions = [
                *linked_conditions,
                CustomerSettlementMappingEntry.organization_guid == financial.organization_guid,
                CustomerSettlementBalance.revision_id == financial.id,
                CustomerSettlementBalance.counterparty_ref
                == CustomerSettlementMappingEntry.counterparty_ref,
                CustomerSettlementBalance.currency == "RUB",
                CustomerAccountSourceBinding.organization_ref == financial.organization_ref,
            ]
            compatible_pilots = (
                session.scalar(
                    select(func.count(distinct(CustomerSettlementPilotAccess.site_user_id)))
                    .select_from(CustomerSettlementPilotAccess)
                    .join(
                        CustomerSettlementMappingEntry,
                        CustomerSettlementMappingEntry.site_user_id
                        == CustomerSettlementPilotAccess.site_user_id,
                    )
                    .join(
                        CustomerSettlementBalance,
                        CustomerSettlementBalance.counterparty_guid
                        == CustomerSettlementMappingEntry.counterparty_guid,
                    )
                    .join(
                        CustomerAccountSourceBinding,
                        CustomerAccountSourceBinding.id
                        == CustomerSettlementMappingEntry.source_binding_id,
                    )
                    .join(
                        CustomerAccountSiteBinding,
                        CustomerAccountSiteBinding.customer_account_id
                        == CustomerSettlementMappingEntry.customer_account_id,
                    )
                    .join(
                        CustomerAccount,
                        CustomerAccount.id == CustomerSettlementMappingEntry.customer_account_id,
                    )
                    .where(*compatible_conditions)
                )
                or 0
            )
    financial_scope_is_complete = (
        financial is not None
        and financial_age_delta is not None
        and financial_age_delta >= timedelta(0)
        and ensure_utc(financial.as_of) <= ensure_utc(financial.source_db_time)
        and ensure_utc(financial.as_of) <= current_time + timedelta(seconds=30)
        and ensure_utc(financial.source_db_time) <= current_time + timedelta(seconds=30)
        and 0 < pilot_counterparties <= MAX_PILOT_USERS
        and financial.currency == "RUB"
        and (expected_source_mode is None or financial.source_mode == expected_source_mode)
        and (
            expected_organization_ref is None
            or financial.organization_ref == expected_organization_ref
        )
        and (
            expected_organization_guid is None
            or financial.organization_guid == expected_organization_guid
        )
        and _ref_guid_pair_is_canonical(
            financial.organization_ref,
            financial.organization_guid,
        )
        and financial_rows_are_valid
        and financial_counterparty_refs == pilot_counterparty_refs
        and financial.expected_row_count == pilot_counterparties
        and financial.loaded_row_count == pilot_counterparties
        and financial.zero_row_count == actual_zero_rows
        and 0 <= actual_zero_rows <= financial.loaded_row_count
    )
    if (
        not financial_scope_is_complete
        or financial_age_delta is None
        or financial_age_delta > timedelta(seconds=hide_after_seconds)
    ):
        freshness_status = "critical"
    elif financial_age_delta >= timedelta(seconds=stale_after_seconds):
        freshness_status = "warning"
    else:
        freshness_status = "ok"
    mapping_status = "critical"
    mapping_is_fresh = mapping is not None and (
        mapping_age_delta is not None
        and mapping_age_delta >= timedelta(0)
        and (
            mapping.source_name == MANUAL_MAPPING_SOURCE_NAME
            or mapping_age_delta <= timedelta(seconds=mapping_stale_after_seconds)
        )
    )
    mapping_is_complete = (
        mapping is not None
        and (
            expected_mapping_source_name is None
            or mapping.source_name == expected_mapping_source_name
        )
        and 0 < enabled_pilots <= MAX_PILOT_USERS
        and mapping.expected_entry_count == enabled_pilots
        and mapping.loaded_entry_count == mapping_entries == enabled_pilots
        and mapping.ambiguous_count == ambiguous_entries == 0
        and linked_pilots == enabled_pilots
        and compatible_pilots == enabled_pilots
        and financial_scope_is_complete
    )
    if mapping_is_fresh and mapping_is_complete:
        mapping_status = "ok"
    active_financial_id = session.scalar(
        select(CustomerSettlementRevision.id).where(
            CustomerSettlementRevision.status == REVISION_ACTIVE
        )
    )
    active_mapping_id = session.scalar(
        select(CustomerSettlementMappingRevision.id).where(
            CustomerSettlementMappingRevision.status == REVISION_ACTIVE
        )
    )
    context_stable = active_financial_id == (financial.id if financial else None) and (
        active_mapping_id == (mapping.id if mapping else None)
    )
    if not context_stable:
        freshness_status = "critical"
        mapping_status = "critical"
        compatible_pilots = 0
    return {
        "freshness_status": freshness_status,
        "mapping_status": mapping_status,
        "financial_age_seconds": financial_age,
        "mapping_age_seconds": mapping_age,
        "expected_rows": financial.expected_row_count if financial else 0,
        "loaded_rows": financial.loaded_row_count if financial else 0,
        "zero_rows": financial.zero_row_count if financial else 0,
        "enabled_pilots": int(enabled_pilots),
        "linked_pilots": int(linked_pilots),
        "pilot_counterparties": int(pilot_counterparties),
        "compatible_pilots": int(compatible_pilots),
        "mapping_entries": int(mapping_entries),
        "ambiguous_entries": int(ambiguous_entries),
        "context_stable": context_stable,
    }


def cleanup_customer_settlements(
    session: Session,
    *,
    successful_retention_days: int,
    failed_retention_days: int,
    jti_retention_hours: int,
    now: datetime | None = None,
) -> dict[str, int]:
    if (
        successful_retention_days,
        failed_retention_days,
        jti_retention_hours,
    ) != (30, 7, 24):
        raise ValueError("customer_settlement_retention_configuration_invalid")
    _require_customer_settlement_context_lock(session)
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
    latest_reconciliation_id = session.scalar(
        select(func.max(CustomerSettlementReconciliationRun.id))
    )
    reconciliation_retention_filter = (
        CustomerSettlementReconciliationRun.status.in_(("matched", "mismatched"))
        & (CustomerSettlementReconciliationRun.created_at < success_cutoff)
    ) | (
        (CustomerSettlementReconciliationRun.status == "blocked")
        & (CustomerSettlementReconciliationRun.created_at < failed_cutoff)
    )
    if latest_reconciliation_id is not None:
        latest_reconciliation_is_expired = bool(
            session.scalar(
                select(func.count())
                .select_from(CustomerSettlementReconciliationRun)
                .where(
                    CustomerSettlementReconciliationRun.id == latest_reconciliation_id,
                    reconciliation_retention_filter,
                )
            )
        )
        reconciliation_retention_filter = (
            CustomerSettlementReconciliationRun.id <= latest_reconciliation_id
            if latest_reconciliation_is_expired
            else (
                reconciliation_retention_filter
                & (CustomerSettlementReconciliationRun.id != latest_reconciliation_id)
            )
        )
    reconciliation_count = (
        session.execute(
            delete(CustomerSettlementReconciliationRun).where(reconciliation_retention_filter)
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
