from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.infrastructure.db import build_onec_engine, get_application_session_factory
from app.models.customer_settlement import CustomerSettlementMappingEntry
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    ManualCustomerSettlementControl,
    fetch_manual_customer_settlement_controls,
)
from app.services.customer_settlements import (
    SettlementMappingInput,
    activate_mapping_revision,
    normalize_guid,
    normalize_site_user_id,
    onec_guid_to_ref,
)

CSV_FIELDS = (
    "site_user_id",
    "counterparty_guid",
    "organization_guid",
    "source_system",
    "expected_code",
    "expected_name",
    "expected_inn",
)
MAX_PILOT_ROWS = 10
MANUAL_SOURCE_NAME = "manual_confirmed_pilot"
_INN_RE = re.compile(r"^(?:[0-9]{10}|[0-9]{12})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManualMappingImportError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ManualMappingRow:
    site_user_id: str
    counterparty_guid: str
    organization_guid: str
    source_system: str
    expected_code: str
    expected_name: str
    expected_inn: str


def _canonical_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split())


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_manual_mapping_csv(path: Path) -> tuple[ManualMappingRow, ...]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise ManualMappingImportError("invalid_csv_header")
            raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ManualMappingImportError("csv_read_failed") from exc
    if not raw_rows:
        raise ManualMappingImportError("manual_mapping_batch_is_empty")
    if len(raw_rows) > MAX_PILOT_ROWS:
        raise ManualMappingImportError("manual_mapping_batch_limit_exceeded")

    result: list[ManualMappingRow] = []
    seen_users: set[str] = set()
    for raw in raw_rows:
        try:
            site_user_id = normalize_site_user_id(raw.get("site_user_id") or "")
            counterparty_guid = normalize_guid(raw.get("counterparty_guid") or "")
            organization_guid = normalize_guid(raw.get("organization_guid") or "")
        except ValueError as exc:
            raise ManualMappingImportError("invalid_mapping_identifier") from exc
        if site_user_id in seen_users:
            raise ManualMappingImportError("duplicate_site_user_id")
        seen_users.add(site_user_id)
        source_system = _canonical_text(raw.get("source_system")).lower()
        if source_system != "ut103":
            raise ManualMappingImportError("unsupported_source_system")
        expected_code = _canonical_text(raw.get("expected_code"))
        expected_name = _canonical_text(raw.get("expected_name"))
        expected_inn = re.sub(r"\s+", "", str(raw.get("expected_inn") or ""))
        if (
            not expected_code
            or not expected_name
            or (expected_inn and not _INN_RE.fullmatch(expected_inn))
        ):
            raise ManualMappingImportError("invalid_identity_controls")
        result.append(
            ManualMappingRow(
                site_user_id=site_user_id,
                counterparty_guid=counterparty_guid,
                organization_guid=organization_guid,
                source_system=source_system,
                expected_code=expected_code,
                expected_name=expected_name,
                expected_inn=expected_inn,
            )
        )
    return tuple(sorted(result, key=lambda item: item.site_user_id))


def _validated_mapping_inputs(
    rows: Sequence[ManualMappingRow],
    controls: Sequence[ManualCustomerSettlementControl],
) -> tuple[SettlementMappingInput, ...]:
    controls_by_guid = {item.counterparty_guid: item for item in controls}
    if len(controls_by_guid) != len({item.counterparty_guid for item in rows}):
        raise ManualMappingImportError("incomplete_manual_mapping_controls")

    entries: list[SettlementMappingInput] = []
    for item in rows:
        control = controls_by_guid.get(item.counterparty_guid)
        if control is None:
            raise ManualMappingImportError("incomplete_manual_mapping_controls")
        if any(code != "643" for code in control.active_contract_currency_codes):
            raise ManualMappingImportError("counterparty_has_non_rub_contract")
        actual_code = _canonical_text(control.counterparty_code)
        actual_name = _canonical_text(control.counterparty_name)
        actual_inn = re.sub(r"\s+", "", str(control.counterparty_inn or ""))
        if (actual_code, actual_name) != (item.expected_code, item.expected_name):
            raise ManualMappingImportError("identity_control_mismatch")
        if item.expected_inn and actual_inn != item.expected_inn:
            raise ManualMappingImportError("identity_control_mismatch")
        identity_control_hash = _hash_payload(
            {
                "counterparty_guid": item.counterparty_guid,
                "organization_guid": item.organization_guid,
                "source_system": item.source_system,
                "code": actual_code,
                "name": actual_name,
                "inn": actual_inn if item.expected_inn else None,
            }
        )
        entries.append(
            SettlementMappingInput(
                site_user_id=item.site_user_id,
                cluster_id=f"manual:{_hash_payload(item.site_user_id)[:24]}",
                counterparty_ref=onec_guid_to_ref(item.counterparty_guid),
                counterparty_guid=item.counterparty_guid,
                counterparty_code=actual_code,
                identity_control_hash=identity_control_hash,
                status="linked",
            )
        )
    return tuple(entries)


def import_manual_customer_settlement_mappings(
    session: Session,
    onec_engine: Engine,
    *,
    rows: Sequence[ManualMappingRow],
    settings: Settings,
    apply: bool,
    approved_by: str | None,
    approved_input_hash: str | None = None,
    approved_controls_hash: str | None = None,
) -> dict[str, object]:
    if apply and not _canonical_text(approved_by):
        raise ManualMappingImportError("approved_by_required")
    if apply and (
        not _SHA256_RE.fullmatch(str(approved_input_hash or "").strip().lower())
        or not _SHA256_RE.fullmatch(str(approved_controls_hash or "").strip().lower())
    ):
        raise ManualMappingImportError("approved_dry_run_hashes_required")
    if not settings.customer_settlements_organization_ref:
        raise ManualMappingImportError("organization_ref_not_configured")
    if not settings.customer_settlements_organization_guid:
        raise ManualMappingImportError("organization_guid_not_configured")
    try:
        configured_organization_guid = normalize_guid(
            settings.customer_settlements_organization_guid
        )
    except ValueError as exc:
        raise ManualMappingImportError("organization_guid_not_configured") from exc
    if {item.organization_guid for item in rows} != {configured_organization_guid}:
        raise ManualMappingImportError("organization_control_mismatch")
    if {item.source_system for item in rows} != {"ut103"}:
        raise ManualMappingImportError("unsupported_source_system")

    controls = fetch_manual_customer_settlement_controls(
        onec_engine,
        organization_ref=settings.customer_settlements_organization_ref,
        organization_guid=configured_organization_guid,
        counterparty_guids=[item.counterparty_guid for item in rows],
        counterparty_inn_field=settings.customer_settlements_counterparty_inn_field,
        query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
    )
    entries = _validated_mapping_inputs(rows, controls)
    expected_inn_by_guid = {item.counterparty_guid: item.expected_inn for item in rows}
    input_hash = _hash_payload(
        [
            {
                "site_user_id": item.site_user_id,
                "counterparty_guid": item.counterparty_guid,
                "organization_guid": item.organization_guid,
                "source_system": item.source_system,
                "expected_code": item.expected_code,
                "expected_name": item.expected_name,
                "expected_inn": item.expected_inn,
            }
            for item in rows
        ]
    )
    controls_hash = _hash_payload(
        [
            {
                "counterparty_guid": item.counterparty_guid,
                "counterparty_code": item.counterparty_code,
                "counterparty_name": item.counterparty_name,
                "counterparty_inn": (
                    item.counterparty_inn
                    if expected_inn_by_guid.get(item.counterparty_guid)
                    else None
                ),
                "active_contract_currency_codes": item.active_contract_currency_codes,
            }
            for item in controls
        ]
    )
    if apply and (
        not hmac.compare_digest(input_hash, str(approved_input_hash).strip().lower())
        or not hmac.compare_digest(controls_hash, str(approved_controls_hash).strip().lower())
    ):
        raise ManualMappingImportError("approved_dry_run_hash_mismatch")
    try:
        revision, activated = activate_mapping_revision(
            session,
            entries=entries,
            source_name=MANUAL_SOURCE_NAME,
            source_system="ut103",
            organization_ref=settings.customer_settlements_organization_ref,
            organization_guid=configured_organization_guid,
        )
        loaded_count = session.scalar(
            select(func.count())
            .select_from(CustomerSettlementMappingEntry)
            .where(
                CustomerSettlementMappingEntry.revision_id == revision.id,
                CustomerSettlementMappingEntry.status == "linked",
                CustomerSettlementMappingEntry.customer_account_id.is_not(None),
                CustomerSettlementMappingEntry.source_binding_id.is_not(None),
            )
        )
        if revision.source_name != MANUAL_SOURCE_NAME or loaded_count != len(rows):
            raise ManualMappingImportError("mapping_readback_failed")
        if apply:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise

    result: dict[str, object] = {
        "status": "applied" if apply else "validated",
        "mode": "apply" if apply else "dry-run",
        "row_count": len(rows),
        "unique_counterparty_count": len({item.counterparty_guid for item in rows}),
        "inn_control_count": sum(bool(item.expected_inn) for item in rows),
        "input_hash": input_hash,
        "controls_hash": controls_hash,
        "mapping_changed": bool(activated),
    }
    if apply:
        result["approval_hash"] = _hash_payload(_canonical_text(approved_by))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and optionally apply a maximum of 10 manually approved "
            "customer-settlement mappings."
        )
    )
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-by")
    parser.add_argument("--approved-input-hash")
    parser.add_argument("--approved-controls-hash")
    args = parser.parse_args(argv)
    if args.apply and not _canonical_text(args.approved_by):
        parser.error("--approved-by is required with --apply")
    if args.apply and (not args.approved_input_hash or not args.approved_controls_hash):
        parser.error("--approved-input-hash and --approved-controls-hash are required with --apply")

    mode = "apply" if args.apply else "dry-run"
    session = None
    onec_engine = None
    try:
        settings = get_settings()
        if not settings.onec_database_url:
            raise ManualMappingImportError("onec_source_not_configured")
        rows = load_manual_mapping_csv(args.csv_path)
        onec_engine = build_onec_engine(
            settings.onec_database_url,
            query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
            login_timeout_seconds=min(settings.customer_settlements_query_timeout_seconds, 6),
            poolclass=NullPool,
        )
        session = get_application_session_factory()()
        result = import_manual_customer_settlement_mappings(
            session,
            onec_engine,
            rows=rows,
            settings=settings,
            apply=args.apply,
            approved_by=args.approved_by,
            approved_input_hash=args.approved_input_hash,
            approved_controls_hash=args.approved_controls_hash,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ManualMappingImportError, CustomerSettlementSourceError) as exc:
        if session is not None:
            session.rollback()
        print(
            json.dumps(
                {"status": "blocked", "mode": mode, "error_code": exc.code},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    finally:
        if session is not None:
            session.close()
        if onec_engine is not None:
            onec_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
