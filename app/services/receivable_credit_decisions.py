from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.receivable_credit_decision import ReceivableCreditDecisionOperation
from app.services.exporters.ut103_credit_terms import (
    MAX_CREDIT_DEPTH,
    MAX_CREDIT_LIMIT,
    CreditTermsCommand,
    CreditTermsExchangeResult,
    CreditTermsMessage,
    build_receivable_credit_decision_message_id,
    parse_credit_terms_result,
    validate_credit_terms_message_id,
    write_credit_terms_message,
)
from app.services.exporters.ut103_customer_price_types import (
    one_c_guid_from_counterparty_ref,
)

ACTIVE_STATES = frozenset(
    {
        "pending_dry_run",
        "dry_run_sent",
        "dry_run_ok",
        "apply_sent",
        "applying",
    }
)
TERMINAL_STATES = frozenset({"applied", "failed", "cancelled"})
BitrixCaller = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ApprovedDecision:
    bitrix_item_id: str
    bitrix_stage_id: str
    bitrix_revision: str
    moved_by_user_id: str
    approved_at: datetime
    counterparty_ref: str
    counterparty_guid: str
    counterparty_code: str
    counterparty_name: str
    contract_ref: str
    contract_guid: str
    contract_code: str
    contract_name: str
    contract_organization_ref: str
    contract_organization_guid: str
    expected_current_limit: Decimal
    expected_current_depth: int
    expected_control_enabled: bool
    proposed_limit: Decimal
    proposed_depth: int
    proposed_control_enabled: bool
    reason: str
    decision_hash: str
    card_decision_hash: str
    raw: dict[str, Any]


def calculate_decision_hash(
    *,
    bitrix_item_id: str,
    revision: str,
    counterparty_ref: str,
    counterparty_guid: str,
    counterparty_code: str,
    contract_ref: str,
    contract_guid: str,
    contract_code: str,
    contract_organization_ref: str,
    contract_organization_guid: str,
    expected_current_limit: Decimal,
    expected_current_depth: int,
    expected_control_enabled: bool,
    proposed_limit: Decimal,
    proposed_depth: int,
    proposed_control_enabled: bool,
    reason: str,
    approved_by: str,
    approved_at: datetime,
) -> str:
    canonical = {
        "approved_at": _as_aware_utc(approved_at).isoformat(timespec="seconds"),
        "approved_by": str(approved_by).strip(),
        "bitrix_item_id": str(bitrix_item_id).strip(),
        "counterparty_code": str(counterparty_code).strip(),
        "counterparty_guid": str(counterparty_guid).strip().lower(),
        "counterparty_ref": str(counterparty_ref).strip().upper(),
        "contract_ref": str(contract_ref).strip().upper(),
        "contract_guid": str(contract_guid).strip().lower(),
        "contract_code": str(contract_code).strip(),
        "contract_organization_ref": str(contract_organization_ref).strip().upper(),
        "contract_organization_guid": str(contract_organization_guid).strip().lower(),
        "expected_current_depth": expected_current_depth,
        "expected_current_limit": _canonical_money(expected_current_limit),
        "expected_control_enabled": expected_control_enabled,
        "proposed_depth": proposed_depth,
        "proposed_limit": _canonical_money(proposed_limit),
        "proposed_control_enabled": proposed_control_enabled,
        "reason": str(reason).strip(),
        "revision": str(revision).strip(),
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_credit_decision_mapping(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path = Path(settings.receivable_credit_decision_mapping_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    payload: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("receivable credit-decision mapping must be a JSON object")
        payload = loaded
    stage_map = {
        **(payload.get("stage_map") or {}),
        **settings.receivable_credit_decision_stage_map,
    }
    fields = {
        **(payload.get("fields") or {}),
        **settings.receivable_credit_decision_field_map,
    }
    process = dict(payload.get("process") or {})
    if settings.receivable_credit_decision_entity_type_id:
        process["entity_type_id"] = settings.receivable_credit_decision_entity_type_id
    if settings.receivable_credit_decision_category_id is not None:
        process["category_id"] = settings.receivable_credit_decision_category_id
    mapping = {**payload, "process": process, "stage_map": stage_map, "fields": fields}
    _validate_mapping(mapping)
    return mapping


def parse_approved_decision(
    item: dict[str, Any],
    *,
    mapping: dict[str, Any],
    approved_by: str | None = None,
    approved_at: datetime | None = None,
) -> ApprovedDecision:
    fields = mapping["fields"]
    item_id = str(_item_value(item, "id") or "").strip()
    revision = str(_mapped_value(item, fields, "decision_revision") or "").strip()
    counterparty_ref = str(_mapped_value(item, fields, "counterparty_ref") or "").strip()
    counterparty_guid = str(_mapped_value(item, fields, "counterparty_guid") or "").strip().lower()
    counterparty_code = str(_mapped_value(item, fields, "counterparty_code") or "").strip()
    counterparty_name = str(_mapped_value(item, fields, "counterparty_name") or "").strip()
    contract_ref = str(_mapped_value(item, fields, "contract_ref") or "").strip()
    contract_guid = str(_mapped_value(item, fields, "contract_guid") or "").strip().lower()
    contract_code = str(_mapped_value(item, fields, "contract_code") or "").strip()
    contract_name = str(_mapped_value(item, fields, "contract_name") or "").strip()
    contract_organization_ref = str(
        _mapped_value(item, fields, "contract_organization_ref") or ""
    ).strip()
    contract_organization_guid = (
        str(_mapped_value(item, fields, "contract_organization_guid") or "").strip().lower()
    )
    reason = str(_mapped_value(item, fields, "reason") or "").strip()
    moved_by = str(
        approved_by if approved_by is not None else (_item_value(item, "movedBy") or "")
    ).strip()
    approved_time = approved_at or _parse_datetime(
        _item_value(item, "movedTime")
        or _item_value(item, "updatedTime")
        or _mapped_value(item, fields, "approved_at")
    )
    missing = [
        name
        for name, value in {
            "id": item_id,
            "decision_revision": revision,
            "counterparty_ref": counterparty_ref,
            "counterparty_guid": counterparty_guid,
            "counterparty_code": counterparty_code,
            "counterparty_name": counterparty_name,
            "contract_ref": contract_ref,
            "contract_guid": contract_guid,
            "contract_code": contract_code,
            "contract_name": contract_name,
            "contract_organization_ref": contract_organization_ref,
            "contract_organization_guid": contract_organization_guid,
            "reason": reason,
            "movedBy": moved_by,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Bitrix decision fields are empty: {', '.join(missing)}")
    if not item_id.isdigit():
        raise ValueError("Bitrix item id must be numeric")
    length_limits = {
        "id": (item_id, 64),
        "decision_revision": (revision, 96),
        "counterparty_ref": (counterparty_ref, 64),
        "counterparty_guid": (counterparty_guid, 36),
        "counterparty_code": (counterparty_code, 32),
        "counterparty_name": (counterparty_name, 255),
        "contract_ref": (contract_ref, 64),
        "contract_guid": (contract_guid, 36),
        "contract_code": (contract_code, 32),
        "contract_name": (contract_name, 255),
        "contract_organization_ref": (contract_organization_ref, 64),
        "contract_organization_guid": (contract_organization_guid, 36),
        "movedBy": (moved_by, 32),
    }
    too_long = [
        f"{name}>{limit}" for name, (value, limit) in length_limits.items() if len(value) > limit
    ]
    if too_long:
        raise ValueError(f"Bitrix decision fields exceed length limits: {', '.join(too_long)}")
    expected_guid = one_c_guid_from_counterparty_ref(counterparty_ref)
    if counterparty_guid != expected_guid:
        raise ValueError("counterparty_guid does not match counterparty_ref")
    expected_contract_guid = one_c_guid_from_counterparty_ref(contract_ref)
    if contract_guid != expected_contract_guid:
        raise ValueError("contract_guid does not match contract_ref")
    expected_organization_guid = one_c_guid_from_counterparty_ref(contract_organization_ref)
    if contract_organization_guid != expected_organization_guid:
        raise ValueError("contract_organization_guid does not match contract_organization_ref")
    current_limit = _parse_money(_mapped_value(item, fields, "current_limit"), "current_limit")
    current_depth = _parse_depth(_mapped_value(item, fields, "current_depth"), "current_depth")
    proposed_limit = _parse_money(_mapped_value(item, fields, "proposed_limit"), "proposed_limit")
    proposed_depth = _parse_depth(_mapped_value(item, fields, "proposed_depth"), "proposed_depth")
    expected_control_enabled = _parse_bool(
        _mapped_value(item, fields, "current_control_enabled"), "current_control_enabled"
    )
    proposed_control_enabled = _parse_bool(
        _mapped_value(item, fields, "proposed_control_enabled"), "proposed_control_enabled"
    )
    decision_hash = calculate_decision_hash(
        bitrix_item_id=item_id,
        revision=revision,
        counterparty_ref=counterparty_ref,
        counterparty_guid=counterparty_guid,
        counterparty_code=counterparty_code,
        contract_ref=contract_ref,
        contract_guid=contract_guid,
        contract_code=contract_code,
        contract_organization_ref=contract_organization_ref,
        contract_organization_guid=contract_organization_guid,
        expected_current_limit=current_limit,
        expected_current_depth=current_depth,
        expected_control_enabled=expected_control_enabled,
        proposed_limit=proposed_limit,
        proposed_depth=proposed_depth,
        proposed_control_enabled=proposed_control_enabled,
        reason=reason,
        approved_by=moved_by,
        approved_at=approved_time,
    )
    return ApprovedDecision(
        bitrix_item_id=item_id,
        bitrix_stage_id=str(_item_value(item, "stageId") or "").strip(),
        bitrix_revision=revision,
        moved_by_user_id=moved_by,
        approved_at=approved_time,
        counterparty_ref=counterparty_ref,
        counterparty_guid=counterparty_guid,
        counterparty_code=counterparty_code,
        counterparty_name=counterparty_name,
        contract_ref=contract_ref,
        contract_guid=contract_guid,
        contract_code=contract_code,
        contract_name=contract_name,
        contract_organization_ref=contract_organization_ref,
        contract_organization_guid=contract_organization_guid,
        expected_current_limit=current_limit,
        expected_current_depth=current_depth,
        expected_control_enabled=expected_control_enabled,
        proposed_limit=proposed_limit,
        proposed_depth=proposed_depth,
        proposed_control_enabled=proposed_control_enabled,
        reason=reason,
        decision_hash=decision_hash,
        card_decision_hash=str(_mapped_value(item, fields, "decision_hash") or "").strip(),
        raw=item,
    )


def run_credit_decision_worker_once(
    db: Session,
    *,
    exchange_root: str | Path,
    settings: Settings | None = None,
    mapping: dict[str, Any] | None = None,
    bitrix_caller: BitrixCaller | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.receivable_credit_decision_enabled:
        return {"disabled": 1, "polled": 0, "processed": 0, "errors": 0}
    mapping = mapping or load_credit_decision_mapping(settings)
    caller = bitrix_caller or _settings_bitrix_caller(settings)
    current_time = _as_aware_utc(now or datetime.now(timezone.utc))
    metrics: dict[str, Any] = {
        "disabled": 0,
        "polled": 0,
        "created": 0,
        "processed": 0,
        "applied": 0,
        "failed": 0,
        "cancelled": 0,
        "bitrix_sync_pending": 0,
        "errors": 0,
    }

    try:
        items = _list_approved_items(caller, mapping, settings)
        metrics["polled"] = len(items)
        for item in items:
            try:
                created = _ingest_item(
                    db,
                    item,
                    mapping=mapping,
                    settings=settings,
                    caller=caller,
                    now=current_time,
                )
                metrics["created"] += int(created)
            except (ValueError, RuntimeError, IntegrityError) as error:
                db.rollback()
                metrics["errors"] += 1
                metrics.setdefault("error_messages", []).append(str(error)[:500])
    except RuntimeError as error:
        metrics["errors"] += 1
        metrics.setdefault("error_messages", []).append(str(error)[:500])

    operations = list(
        db.scalars(
            select(ReceivableCreditDecisionOperation)
            .where(
                or_(
                    ReceivableCreditDecisionOperation.state.in_(tuple(ACTIVE_STATES)),
                    ReceivableCreditDecisionOperation.bitrix_sync_pending.is_(True),
                )
            )
            .order_by(ReceivableCreditDecisionOperation.id)
        )
    )
    for operation in operations:
        if operation.state == "applied" and not operation.bitrix_sync_pending:
            continue
        before = operation.state
        try:
            _advance_operation(
                db,
                operation,
                exchange_root=Path(exchange_root),
                mapping=mapping,
                settings=settings,
                caller=caller,
                now=current_time,
            )
            metrics["processed"] += 1
        except (ValueError, RuntimeError, OSError) as error:
            db.rollback()
            metrics["errors"] += 1
            metrics.setdefault("error_messages", []).append(str(error)[:500])
            continue
        if operation.state == "applied" and before != "applied":
            metrics["applied"] += 1
        if operation.state == "failed" and before != "failed":
            metrics["failed"] += 1
        if operation.state == "cancelled" and before != "cancelled":
            metrics["cancelled"] += 1
        if operation.bitrix_sync_pending:
            metrics["bitrix_sync_pending"] += 1

    metrics.update(_operation_monitoring(db, current_time))
    return metrics


def _ingest_item(
    db: Session,
    item: dict[str, Any],
    *,
    mapping: dict[str, Any],
    settings: Settings,
    caller: BitrixCaller,
    now: datetime,
) -> bool:
    decision = parse_approved_decision(item, mapping=mapping)
    allowed = {
        str(value).strip()
        for value in settings.receivable_credit_decision_approver_user_ids
        if str(value).strip()
    }
    if decision.moved_by_user_id not in allowed:
        _update_item_error(
            caller,
            mapping,
            decision.bitrix_item_id,
            f"Согласующий {decision.moved_by_user_id} не входит в allowlist",
        )
        return False
    pilots = {
        str(value).strip().upper()
        for value in settings.receivable_credit_decision_pilot_counterparty_codes
        if str(value).strip()
    }
    if pilots and decision.counterparty_code.upper() not in pilots:
        return False
    if decision.card_decision_hash and (decision.card_decision_hash != decision.decision_hash):
        _update_item_error(
            caller,
            mapping,
            decision.bitrix_item_id,
            "Хеш карточки не соответствует утвержденным значениям; требуется повторное согласование",
        )
        return False

    entity_type_id = int(mapping["process"]["entity_type_id"])
    existing = db.scalar(
        select(ReceivableCreditDecisionOperation).where(
            ReceivableCreditDecisionOperation.bitrix_entity_type_id == entity_type_id,
            ReceivableCreditDecisionOperation.bitrix_item_id == decision.bitrix_item_id,
            ReceivableCreditDecisionOperation.bitrix_revision == decision.bitrix_revision,
        )
    )
    if existing is not None:
        if existing.decision_hash != decision.decision_hash:
            _update_item_error(
                caller,
                mapping,
                decision.bitrix_item_id,
                "Ревизия решения уже зарегистрирована с другим хешем; "
                "создайте новую ревизию и повторно согласуйте карточку",
            )
        return False
    active_same_item = db.scalar(
        select(ReceivableCreditDecisionOperation).where(
            ReceivableCreditDecisionOperation.bitrix_entity_type_id == entity_type_id,
            ReceivableCreditDecisionOperation.bitrix_item_id == decision.bitrix_item_id,
            ReceivableCreditDecisionOperation.active_counterparty_key.is_not(None),
        )
    )
    if active_same_item is not None:
        if active_same_item.state in {"apply_sent", "applying"}:
            message = (
                "Предыдущая ревизия уже могла быть применена в 1С; "
                "новая ревизия заблокирована до result/readback"
            )
            active_same_item.last_error = message
            try:
                _update_item(
                    caller,
                    mapping,
                    decision.bitrix_item_id,
                    stage_key="applying",
                    logical_fields={
                        "connector_state": active_same_item.state,
                        "connector_error": message,
                    },
                )
            except RuntimeError:
                active_same_item.bitrix_sync_pending = True
            db.commit()
            return False
        active_same_item.state = "cancelled"
        active_same_item.active_counterparty_key = None
        active_same_item.last_error = (
            "Карточка изменена после согласования; требуется повторно перевести ее "
            "в стадию «Утверждено»"
        )
        active_same_item.bitrix_sync_pending = True
        db.commit()
        _sync_error_to_bitrix(
            db,
            active_same_item,
            mapping=mapping,
            caller=caller,
        )
        return False

    counterparty_key = decision.counterparty_guid
    active_same_counterparty = db.scalar(
        select(ReceivableCreditDecisionOperation).where(
            ReceivableCreditDecisionOperation.active_counterparty_key == counterparty_key,
        )
    )
    if active_same_counterparty is not None:
        _update_item_error(
            caller,
            mapping,
            decision.bitrix_item_id,
            "Для этого контрагента уже выполняется другое решение "
            f"(Bitrix item {active_same_counterparty.bitrix_item_id})",
        )
        return False

    operation = ReceivableCreditDecisionOperation(
        bitrix_entity_type_id=entity_type_id,
        bitrix_item_id=decision.bitrix_item_id,
        bitrix_category_id=int(mapping["process"].get("category_id") or 0),
        bitrix_stage_id=decision.bitrix_stage_id,
        bitrix_revision=decision.bitrix_revision,
        moved_by_user_id=decision.moved_by_user_id,
        decision_id=decision.bitrix_item_id,
        decision_hash=decision.decision_hash,
        counterparty_key=counterparty_key,
        active_counterparty_key=counterparty_key,
        counterparty_ref=decision.counterparty_ref,
        counterparty_guid=decision.counterparty_guid,
        counterparty_code=decision.counterparty_code,
        counterparty_name=decision.counterparty_name,
        contract_ref=decision.contract_ref,
        contract_guid=decision.contract_guid,
        contract_code=decision.contract_code,
        contract_name=decision.contract_name,
        contract_organization_ref=decision.contract_organization_ref,
        contract_organization_guid=decision.contract_organization_guid,
        expected_current_limit=decision.expected_current_limit,
        expected_current_depth=decision.expected_current_depth,
        expected_control_enabled=decision.expected_control_enabled,
        proposed_limit=decision.proposed_limit,
        proposed_depth=decision.proposed_depth,
        proposed_control_enabled=decision.proposed_control_enabled,
        currency="RUB",
        reason=decision.reason,
        approved_by=decision.moved_by_user_id,
        approved_at=_as_naive_utc(decision.approved_at),
        state="pending_dry_run",
        source_payload=decision.raw,
    )
    db.add(operation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    _update_item(
        caller,
        mapping,
        decision.bitrix_item_id,
        stage_key="onec_check",
        logical_fields={
            "decision_hash": decision.decision_hash,
            "approved_by": decision.moved_by_user_id,
            "approved_at": _as_aware_utc(decision.approved_at).isoformat(),
            "connector_state": "pending_dry_run",
            "connector_error": "",
        },
    )
    return True


def _advance_operation(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    exchange_root: Path,
    mapping: dict[str, Any],
    settings: Settings,
    caller: BitrixCaller,
    now: datetime,
) -> None:
    if operation.state == "applied":
        if operation.bitrix_sync_pending:
            _sync_applied_to_bitrix(db, operation, mapping=mapping, caller=caller)
        return
    if operation.state in {"failed", "cancelled"}:
        if operation.bitrix_sync_pending:
            _sync_error_to_bitrix(db, operation, mapping=mapping, caller=caller)
        return

    if operation.state == "pending_dry_run":
        _send_dry_run(db, operation, exchange_root=exchange_root, now=now)
    if operation.state == "dry_run_sent":
        try:
            item = _get_item(caller, mapping, operation.bitrix_item_id)
        except RuntimeError:
            item = None
        allowed_stages = {
            mapping["stage_map"].get("approved"),
            mapping["stage_map"].get("onec_check"),
        }
        if item is not None and str(_item_value(item, "stageId") or "") not in allowed_stages:
            _mark_cancelled(
                db,
                operation,
                "Карточка отменена или переведена с этапа проверки во время dry_run",
            )
            return
        result = _find_result(exchange_root, operation.dry_run_message_id)
        if result is not None:
            _consume_dry_run_result(
                db,
                operation,
                result,
                mapping=mapping,
                caller=caller,
                now=now,
            )
            _archive_result(result)
        elif _timed_out(
            operation.dry_run_sent_at,
            now,
            settings.receivable_credit_decision_result_timeout_seconds,
        ):
            if (
                operation.dry_run_attempts
                < settings.receivable_credit_decision_max_dry_run_attempts
            ):
                _send_dry_run(db, operation, exchange_root=exchange_root, now=now, retry=True)
            else:
                _mark_failed(
                    db,
                    operation,
                    "Не получен результат dry_run после допустимого числа повторов",
                    mapping=mapping,
                    caller=caller,
                )
                return
    if operation.state != "dry_run_ok":
        if operation.state in {"apply_sent", "applying"}:
            _consume_apply_if_available(
                db,
                operation,
                exchange_root=exchange_root,
                mapping=mapping,
                caller=caller,
                settings=settings,
                now=now,
            )
        return

    if not settings.receivable_credit_decision_auto_apply_enabled:
        return
    pilots = {
        str(value).strip().upper()
        for value in settings.receivable_credit_decision_pilot_counterparty_codes
    }
    if operation.counterparty_code.upper() not in pilots:
        return
    if operation.apply_message_id:
        ready = _ready_path(exchange_root, operation.apply_message_id)
        result = _find_result(exchange_root, operation.apply_message_id)
        if ready.exists() or result is not None:
            operation.state = "apply_sent"
            operation.apply_attempts = max(operation.apply_attempts, 1)
            operation.apply_sent_at = operation.apply_sent_at or _as_naive_utc(now)
            db.commit()
            _consume_apply_if_available(
                db,
                operation,
                exchange_root=exchange_root,
                mapping=mapping,
                caller=caller,
                settings=settings,
                now=now,
            )
        else:
            operation.state = "applying"
            operation.apply_sent_at = operation.apply_sent_at or _as_naive_utc(now)
            operation.last_error = (
                "Неопределенный исход публикации apply: result/ready файл не найден; "
                "повторная отправка заблокирована до readback"
            )
            db.commit()
        return

    item = _get_item(caller, mapping, operation.bitrix_item_id)
    allowed_stages = {
        mapping["stage_map"].get("approved"),
        mapping["stage_map"].get("onec_check"),
    }
    if str(_item_value(item, "stageId") or "") not in allowed_stages:
        _mark_cancelled(
            db,
            operation,
            "Карточка отменена или переведена с этапа проверки до apply",
        )
        return
    current = parse_approved_decision(
        item,
        mapping=mapping,
        approved_by=operation.approved_by,
        approved_at=_as_aware_utc(operation.approved_at),
    )
    if (
        current.decision_hash != operation.decision_hash
        or current.card_decision_hash != operation.decision_hash
    ):
        _mark_cancelled(
            db,
            operation,
            "Карточка изменена после dry_run; требуется повторное согласование",
        )
        operation.bitrix_sync_pending = True
        db.commit()
        _sync_error_to_bitrix(db, operation, mapping=mapping, caller=caller)
        return

    operation.apply_message_id = _message_id(operation, "apply")
    operation.apply_attempts = 1
    operation.apply_sent_at = _as_naive_utc(now)
    operation.state = "applying"
    operation.last_error = (
        "Публикация apply начата; до подтверждения result/readback " "повторная отправка запрещена"
    )
    db.commit()
    message = _message_for_operation(operation, mode="apply")
    write_credit_terms_message(exchange_root, message)
    operation.state = "apply_sent"
    operation.last_error = None
    db.commit()
    try:
        _update_item(
            caller,
            mapping,
            operation.bitrix_item_id,
            stage_key="applying",
            logical_fields={"connector_state": "apply_sent", "connector_error": ""},
        )
    except RuntimeError:
        operation.bitrix_sync_pending = True
        db.commit()
        return
    operation.bitrix_sync_pending = False
    db.commit()


def _send_dry_run(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    exchange_root: Path,
    now: datetime,
    retry: bool = False,
) -> None:
    operation.dry_run_message_id = operation.dry_run_message_id or _message_id(operation, "dry-run")
    message = _message_for_operation(operation, mode="dry_run")
    operation.dry_run_attempts += 1
    operation.dry_run_sent_at = _as_naive_utc(now)
    operation.state = "dry_run_sent"
    operation.last_error = "Публикация dry_run начата; повтор разрешен только с тем же MessageId"
    # Состояние фиксируется до файловой публикации. Если процесс завершится между
    # rename ready-файла и следующим commit, worker безопасно сверит тот же
    # детерминированный MessageId и не застрянет в pending_dry_run.
    db.commit()
    try:
        write_credit_terms_message(exchange_root, message)
    except FileExistsError:
        # Ready-файл мог сохраниться после сбоя между публикацией и commit.
        # dry_run безопасен и повторяем, поэтому существование того же
        # детерминированного файла эквивалентно успешной публикации.
        pass
    operation.last_error = None
    db.commit()


def _consume_dry_run_result(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    result: CreditTermsExchangeResult,
    *,
    mapping: dict[str, Any],
    caller: BitrixCaller,
    now: datetime,
) -> None:
    item = _verified_result_item(result, operation)
    operation.last_result_status = item.status
    operation.last_result_at = _as_naive_utc(now)
    if not result.ok or item.status not in {"validated", "already_actual"}:
        _mark_failed(
            db,
            operation,
            item.message or result.errors or "dry_run отклонен 1С",
            mapping=mapping,
            caller=caller,
        )
        return
    operation.state = "dry_run_ok"
    operation.last_error = None
    db.commit()


def _consume_apply_if_available(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    exchange_root: Path,
    mapping: dict[str, Any],
    caller: BitrixCaller,
    settings: Settings,
    now: datetime,
) -> None:
    result = _find_result(exchange_root, operation.apply_message_id)
    if result is None:
        recovery_result = _find_result(exchange_root, operation.readback_message_id)
        if recovery_result is not None:
            _consume_recovery_readback_result(
                db,
                operation,
                recovery_result,
                mapping=mapping,
                caller=caller,
                now=now,
            )
            _archive_result(recovery_result)
            return
        if operation.readback_message_id:
            if _timed_out(
                operation.readback_sent_at,
                now,
                settings.receivable_credit_decision_result_timeout_seconds,
            ):
                if (
                    operation.readback_attempts
                    < settings.receivable_credit_decision_max_readback_attempts
                ):
                    _ensure_recovery_readback(
                        db,
                        operation,
                        exchange_root=exchange_root,
                        now=now,
                        retry=True,
                    )
                else:
                    operation.last_error = (
                        "Recovery readback не получен после допустимого числа повторов; "
                        "apply не переотправлялся, требуется ручная проверка"
                    )
                    db.commit()
            return
        if _timed_out(
            operation.apply_sent_at,
            now,
            settings.receivable_credit_decision_result_timeout_seconds,
        ):
            operation.state = "applying"
            operation.last_error = (
                "Результат apply потерян или задержан; повторная отправка запрещена, "
                "требуется readback"
            )
            db.commit()
            _ensure_recovery_readback(
                db,
                operation,
                exchange_root=exchange_root,
                now=now,
            )
        return
    item = _verified_result_item(result, operation)
    operation.last_result_status = item.status
    operation.last_result_at = _as_naive_utc(now)
    if item.status in {"failed", "needs_review"}:
        _mark_failed(
            db,
            operation,
            item.message or result.errors or "apply отклонен 1С",
            mapping=mapping,
            caller=caller,
        )
        _archive_result(result)
        return
    if not result.ok or item.status not in {"applied", "already_actual"}:
        _mark_apply_ambiguous(
            db,
            operation,
            item.message
            or result.errors
            or "Ответ apply не доказывает атомарное применение утвержденной пары",
            mapping=mapping,
            caller=caller,
        )
        _archive_result(result)
        return
    if (
        item.readback_limit != operation.proposed_limit
        or item.readback_depth != operation.proposed_depth
        or item.readback_control_enabled != operation.proposed_control_enabled
    ):
        _mark_apply_ambiguous(
            db,
            operation,
            "Readback 1С не совпал с утвержденными лимитом, глубиной и флагом контроля; "
            "блокировка контрагента сохранена до recovery readback",
            mapping=mapping,
            caller=caller,
        )
        _archive_result(result)
        return
    _mark_applied_from_readback(
        db,
        operation,
        readback_limit=item.readback_limit,
        readback_depth=item.readback_depth,
        readback_control_enabled=item.readback_control_enabled,
        mapping=mapping,
        caller=caller,
        now=now,
    )
    _archive_result(result)


def _mark_apply_ambiguous(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    message: str,
    *,
    mapping: dict[str, Any],
    caller: BitrixCaller,
) -> None:
    operation.state = "applying"
    operation.last_error = str(message)[:2000]
    # active_counterparty_key намеренно не освобождается: applied без точного
    # readback не является доказанным результатом 1С.
    db.commit()
    try:
        _update_item(
            caller,
            mapping,
            operation.bitrix_item_id,
            logical_fields={
                "connector_state": "applying",
                "connector_error": operation.last_error[:1000],
            },
        )
    except RuntimeError:
        operation.bitrix_sync_pending = True
        db.commit()
        return
    operation.bitrix_sync_pending = False
    db.commit()


def _ensure_recovery_readback(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    exchange_root: Path,
    now: datetime,
    retry: bool = False,
) -> None:
    operation.readback_message_id = operation.readback_message_id or _message_id(
        operation, "readback"
    )
    if not retry and operation.readback_attempts:
        return
    operation.readback_attempts += 1
    operation.readback_sent_at = _as_naive_utc(now)
    db.commit()
    message = _message_for_operation(operation, mode="dry_run", recovery=True)
    try:
        write_credit_terms_message(exchange_root, message)
    except FileExistsError:
        pass


def _consume_recovery_readback_result(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    result: CreditTermsExchangeResult,
    *,
    mapping: dict[str, Any],
    caller: BitrixCaller,
    now: datetime,
) -> None:
    item = _verified_result_item(
        result,
        operation,
        expected_message_id=operation.readback_message_id,
    )
    operation.last_result_status = item.status
    operation.last_result_at = _as_naive_utc(now)
    if (
        result.ok
        and item.status == "already_actual"
        and item.readback_limit == operation.proposed_limit
        and item.readback_depth == operation.proposed_depth
        and item.readback_control_enabled == operation.proposed_control_enabled
    ):
        _mark_applied_from_readback(
            db,
            operation,
            readback_limit=item.readback_limit,
            readback_depth=item.readback_depth,
            readback_control_enabled=item.readback_control_enabled,
            mapping=mapping,
            caller=caller,
            now=now,
        )
        return
    operation.last_error = (
        "Recovery readback не подтвердил применение того же DecisionId/DecisionHash: "
        + (item.message or result.errors or item.status)
    )[:2000]
    db.commit()


def _mark_applied_from_readback(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    readback_limit: Decimal,
    readback_depth: int,
    readback_control_enabled: bool,
    mapping: dict[str, Any],
    caller: BitrixCaller,
    now: datetime,
) -> None:
    operation.readback_limit = readback_limit
    operation.readback_depth = readback_depth
    operation.readback_control_enabled = readback_control_enabled
    operation.state = "applied"
    operation.applied_at = _as_naive_utc(now)
    operation.active_counterparty_key = None
    operation.last_error = None
    operation.bitrix_sync_pending = True
    db.commit()
    _sync_applied_to_bitrix(db, operation, mapping=mapping, caller=caller)


def _sync_applied_to_bitrix(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    mapping: dict[str, Any],
    caller: BitrixCaller,
) -> None:
    try:
        bitrix_item = _get_item(caller, mapping, operation.bitrix_item_id)
        current: ApprovedDecision | None
        try:
            current = parse_approved_decision(
                bitrix_item,
                mapping=mapping,
                approved_by=operation.approved_by,
                approved_at=_as_aware_utc(operation.approved_at),
            )
        except ValueError:
            current = None
        allowed_stages = {
            mapping["stage_map"].get("approved"),
            mapping["stage_map"].get("onec_check"),
            mapping["stage_map"].get("applying"),
            mapping["stage_map"].get("applied"),
        }
        if (
            current is None
            or current.decision_hash != operation.decision_hash
            or current.card_decision_hash != operation.decision_hash
            or str(_item_value(bitrix_item, "stageId") or "") not in allowed_stages
        ):
            message = (
                "1С применила предыдущую утвержденную ревизию, но карточка Bitrix "
                "изменилась во время apply; требуется ручная сверка"
            )
            _update_item(
                caller,
                mapping,
                operation.bitrix_item_id,
                stage_key="onec_error",
                logical_fields={
                    "connector_state": "applied_card_changed",
                    "connector_error": message,
                    "readback_limit": _canonical_money(operation.readback_limit),
                    "readback_depth": operation.readback_depth,
                    "readback_control_enabled": operation.readback_control_enabled,
                },
            )
            operation.bitrix_sync_pending = False
            operation.last_error = message
            db.commit()
            return
        _update_item(
            caller,
            mapping,
            operation.bitrix_item_id,
            stage_key="applied",
            logical_fields={
                "connector_state": "applied",
                "connector_error": "",
                "readback_limit": _canonical_money(operation.readback_limit),
                "readback_depth": operation.readback_depth,
                "readback_control_enabled": operation.readback_control_enabled,
            },
        )
    except RuntimeError as error:
        operation.bitrix_sync_pending = True
        operation.last_error = f"1С применено; Bitrix не обновлен: {str(error)[:350]}"
        db.commit()
        return
    operation.bitrix_sync_pending = False
    operation.last_error = None
    db.commit()


def _sync_error_to_bitrix(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    *,
    mapping: dict[str, Any],
    caller: BitrixCaller,
) -> None:
    message = operation.last_error or "Операция обмена завершилась с ошибкой"
    try:
        _update_item(
            caller,
            mapping,
            operation.bitrix_item_id,
            stage_key="onec_error",
            logical_fields={
                "connector_state": operation.state,
                "connector_error": message[:1000],
            },
        )
    except RuntimeError:
        operation.bitrix_sync_pending = True
        db.commit()
        return
    operation.bitrix_sync_pending = False
    db.commit()


def _mark_failed(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    message: str,
    *,
    mapping: dict[str, Any],
    caller: BitrixCaller,
) -> None:
    operation.state = "failed"
    operation.active_counterparty_key = None
    operation.last_error = str(message)[:2000]
    db.commit()
    try:
        _update_item_error(caller, mapping, operation.bitrix_item_id, operation.last_error)
    except RuntimeError:
        operation.bitrix_sync_pending = True
        db.commit()


def _mark_cancelled(
    db: Session,
    operation: ReceivableCreditDecisionOperation,
    message: str,
) -> None:
    operation.state = "cancelled"
    operation.active_counterparty_key = None
    operation.last_error = message
    db.commit()


def _verified_result_item(
    result: CreditTermsExchangeResult,
    operation: ReceivableCreditDecisionOperation,
    *,
    expected_message_id: str | None = None,
):
    expected_message_id = expected_message_id or (
        operation.apply_message_id
        if operation.state in {"apply_sent", "applying"}
        else operation.dry_run_message_id
    )
    if result.message_id != expected_message_id:
        raise ValueError("result MessageId does not match durable operation")
    if len(result.command_results) != 1:
        raise ValueError("credit-terms result must contain exactly one command")
    item = result.command_results[0]
    if item.idempotency_key != _idempotency_key(operation):
        raise ValueError("result IdempotencyKey does not match durable operation")
    if item.decision_id != operation.decision_id:
        raise ValueError("result DecisionId does not match durable operation")
    if item.decision_hash != operation.decision_hash:
        raise ValueError("result DecisionHash does not match durable operation")
    if item.counterparty_ref != operation.counterparty_ref:
        raise ValueError("result CounterpartyRef does not match durable operation")
    if item.counterparty_guid.lower() != operation.counterparty_guid.lower():
        raise ValueError("result CounterpartyGuid does not match durable operation")
    if item.counterparty_code != operation.counterparty_code:
        raise ValueError("result CounterpartyCode does not match durable operation")
    if item.contract_ref != operation.contract_ref:
        raise ValueError("result ContractRef does not match durable operation")
    if item.contract_guid.lower() != operation.contract_guid.lower():
        raise ValueError("result ContractGuid does not match durable operation")
    if item.contract_code != operation.contract_code:
        raise ValueError("result ContractCode does not match durable operation")
    return item


def _message_for_operation(
    operation: ReceivableCreditDecisionOperation,
    *,
    mode: str,
    recovery: bool = False,
) -> CreditTermsMessage:
    message_id = (
        operation.readback_message_id
        if recovery
        else (operation.apply_message_id if mode == "apply" else operation.dry_run_message_id)
    )
    if not message_id:
        raise RuntimeError(f"{mode} message id is not prepared")
    return CreditTermsMessage(
        message_id=message_id,
        mode=mode,
        commands=(
            CreditTermsCommand(
                idempotency_key=_idempotency_key(operation),
                decision_id=operation.decision_id,
                decision_hash=operation.decision_hash,
                revision=operation.bitrix_revision,
                counterparty_ref=operation.counterparty_ref,
                counterparty_guid=operation.counterparty_guid,
                counterparty_code=operation.counterparty_code,
                counterparty_name=operation.counterparty_name,
                contract_ref=operation.contract_ref,
                contract_guid=operation.contract_guid,
                contract_code=operation.contract_code,
                contract_name=operation.contract_name,
                contract_organization_ref=operation.contract_organization_ref,
                contract_organization_guid=operation.contract_organization_guid,
                expected_current_limit=(
                    operation.proposed_limit if recovery else operation.expected_current_limit
                ),
                expected_current_depth=(
                    operation.proposed_depth if recovery else operation.expected_current_depth
                ),
                expected_control_enabled=(
                    operation.proposed_control_enabled
                    if recovery
                    else operation.expected_control_enabled
                ),
                new_limit=operation.proposed_limit,
                new_depth=operation.proposed_depth,
                new_control_enabled=operation.proposed_control_enabled,
                currency=operation.currency,
                reason=operation.reason,
                approved_by=operation.approved_by,
                approved_at=_as_aware_utc(operation.approved_at),
            ),
        ),
    )


def _list_approved_items(
    caller: BitrixCaller, mapping: dict[str, Any], settings: Settings
) -> list[dict[str, Any]]:
    response = caller(
        "crm.item.list",
        {
            "entityTypeId": int(mapping["process"]["entity_type_id"]),
            "filter": {
                "categoryId": int(mapping["process"].get("category_id") or 0),
                "stageId": mapping["stage_map"]["approved"],
            },
            "select": ["*", "ufCrm*"],
            "order": {"id": "ASC"},
            "start": 0,
        },
    )
    result = response.get("result") or {}
    items = result.get("items") if isinstance(result, dict) else result
    if not isinstance(items, list):
        raise RuntimeError("Bitrix crm.item.list returned invalid items")
    return [
        item
        for item in items[: settings.receivable_credit_decision_poll_limit]
        if isinstance(item, dict)
    ]


def _get_item(caller: BitrixCaller, mapping: dict[str, Any], item_id: str) -> dict[str, Any]:
    response = caller(
        "crm.item.get",
        {
            "entityTypeId": int(mapping["process"]["entity_type_id"]),
            "id": item_id,
        },
    )
    item = (response.get("result") or {}).get("item")
    if not isinstance(item, dict):
        raise RuntimeError(f"Bitrix item {item_id} is unavailable")
    return item


def _update_item(
    caller: BitrixCaller,
    mapping: dict[str, Any],
    item_id: str,
    *,
    stage_key: str | None = None,
    logical_fields: dict[str, Any] | None = None,
) -> None:
    fields: dict[str, Any] = {}
    if stage_key:
        stage_id = mapping["stage_map"].get(stage_key)
        if not stage_id:
            raise RuntimeError(f"Bitrix stage is missing in mapping: {stage_key}")
        fields["stageId"] = stage_id
    for key, value in (logical_fields or {}).items():
        field_code = mapping["fields"].get(key)
        if field_code:
            fields[field_code] = value
    caller(
        "crm.item.update",
        {
            "entityTypeId": int(mapping["process"]["entity_type_id"]),
            "id": item_id,
            "fields": fields,
        },
    )


def _update_item_error(
    caller: BitrixCaller,
    mapping: dict[str, Any],
    item_id: str,
    message: str,
) -> None:
    _update_item(
        caller,
        mapping,
        item_id,
        stage_key="onec_error",
        logical_fields={
            "connector_state": "failed",
            "connector_error": str(message)[:1000],
        },
    )


def _find_result(exchange_root: Path, message_id: str | None) -> CreditTermsExchangeResult | None:
    if not message_id:
        return None
    filename = f"onec_commands_{_safe_message_id(message_id)}.result.xml"
    path = exchange_root / "from_1c" / "new" / filename
    if not path.is_file():
        return None
    return parse_credit_terms_result(path)


def _archive_result(result: CreditTermsExchangeResult) -> Path | None:
    if result.path is None:
        return None
    source = result.path
    if not source.is_file():
        return None
    archive_dir = source.parent.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / source.name
    source.replace(target)
    return target


def _ready_path(exchange_root: Path, message_id: str) -> Path:
    return (
        exchange_root / "to_1c" / "new" / f"onec_commands_{_safe_message_id(message_id)}.ready.xml"
    )


def _message_id(operation: ReceivableCreditDecisionOperation, suffix: str) -> str:
    return build_receivable_credit_decision_message_id(
        entity_type_id=operation.bitrix_entity_type_id,
        item_id=operation.bitrix_item_id,
        revision=operation.bitrix_revision,
        decision_hash=operation.decision_hash,
        suffix=suffix,
    )


def _idempotency_key(operation: ReceivableCreditDecisionOperation) -> str:
    return (
        f"receivable-decision:{operation.bitrix_entity_type_id}:"
        f"{operation.bitrix_item_id}:{operation.bitrix_revision}"
    )


def _operation_monitoring(db: Session, now: datetime) -> dict[str, Any]:
    operations = list(
        db.scalars(
            select(ReceivableCreditDecisionOperation).order_by(ReceivableCreditDecisionOperation.id)
        )
    )
    active = [item for item in operations if item.state in ACTIVE_STATES]
    ages = [
        max(0, int((now - _as_aware_utc(item.updated_at)).total_seconds()))
        for item in active
        if item.updated_at is not None
    ]
    last_success = max(
        (_as_aware_utc(item.applied_at) for item in operations if item.applied_at is not None),
        default=None,
    )
    return {
        "unfinished": len(active),
        "oldest_unfinished_seconds": max(ages, default=0),
        "ambiguous_apply": sum(item.state == "applying" for item in active),
        "lost_result": sum(
            item.state == "applying" and "Результат apply потерян" in (item.last_error or "")
            for item in active
        ),
        "total_retries": sum(
            max(0, item.dry_run_attempts - 1)
            + max(0, item.apply_attempts - 1)
            + max(0, item.readback_attempts - 1)
            for item in operations
        ),
        "recovery_readbacks": sum(item.readback_attempts for item in operations),
        "readback_mismatch": sum(
            "Readback 1С не совпал" in (item.last_error or "") for item in operations
        ),
        "manual_review_queue": sum(
            bool(item.last_error) and item.state in {"applying", "failed", "applied"}
            for item in operations
        ),
        "last_successful_transfer_at": (
            last_success.isoformat(timespec="seconds") if last_success else None
        ),
    }


def _settings_bitrix_caller(settings: Settings) -> BitrixCaller:
    webhook = (settings.receivable_credit_decision_bitrix_webhook_url or "").strip()
    if not webhook:
        raise RuntimeError("RECEIVABLE_CREDIT_DECISION_BITRIX_WEBHOOK_URL is not configured")

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = urllib.parse.urlencode(_flatten_params(params)).encode("utf-8")
        request = urllib.request.Request(
            webhook.rstrip("/") + f"/{method}.json",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Bitrix API {method} is unavailable") from error
        if payload.get("error"):
            raise RuntimeError(
                f"Bitrix API {method}: {payload.get('error_description') or payload['error']}"
            )
        return payload

    return call


def _flatten_params(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}[{key}]" if prefix else str(key)
            rows.extend(_flatten_params(item, child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            rows.extend(_flatten_params(item, f"{prefix}[{index}]"))
    elif value is not None:
        rows.append((prefix, str(value)))
    return rows


def _validate_mapping(mapping: dict[str, Any]) -> None:
    process = mapping.get("process") or {}
    stage_map = mapping.get("stage_map") or {}
    fields = mapping.get("fields") or {}
    if not int(process.get("entity_type_id") or 0):
        raise RuntimeError("receivable credit-decision entity_type_id is missing")
    missing_stages = [
        key
        for key in (
            "approved",
            "onec_check",
            "applying",
            "applied",
            "onec_error",
        )
        if not stage_map.get(key)
    ]
    if missing_stages:
        raise RuntimeError(
            "receivable credit-decision stages are missing: " + ", ".join(missing_stages)
        )
    missing_fields = [
        key
        for key in (
            "counterparty_ref",
            "counterparty_guid",
            "counterparty_code",
            "counterparty_name",
            "contract_ref",
            "contract_guid",
            "contract_code",
            "contract_name",
            "contract_organization_ref",
            "contract_organization_guid",
            "current_limit",
            "current_depth",
            "current_control_enabled",
            "proposed_limit",
            "proposed_depth",
            "proposed_control_enabled",
            "reason",
            "decision_revision",
            "decision_hash",
        )
        if not fields.get(key)
    ]
    if missing_fields:
        raise RuntimeError(
            "receivable credit-decision fields are missing: " + ", ".join(missing_fields)
        )


def _mapped_value(item: dict[str, Any], fields: dict[str, str], logical_key: str) -> Any:
    code = fields.get(logical_key)
    return _item_value(item, code) if code else None


def _item_value(item: dict[str, Any], key: str | None) -> Any:
    if not key:
        return None
    if key in item:
        return item[key]
    lower = key.lower()
    for candidate, value in item.items():
        if str(candidate).lower() == lower:
            return value
    if key.upper().startswith("UF_CRM_"):
        parts = key.upper().split("_")
        camel = "ufCrm" + "".join(part.title() for part in parts[2:])
        if camel in item:
            return item[camel]
    return None


def _parse_money(value: Any, field_name: str) -> Decimal:
    if value in (None, ""):
        raise ValueError(f"{field_name} is required")
    try:
        parsed = Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a RUB amount") from error
    if (
        not parsed.is_finite()
        or parsed < 0
        or parsed > MAX_CREDIT_LIMIT
        or parsed.as_tuple().exponent < -2
    ):
        raise ValueError(f"{field_name} must be a non-negative RUB amount")
    return parsed


def _parse_depth(value: Any, field_name: str) -> int:
    if value in (None, "") or isinstance(value, bool):
        raise ValueError(f"{field_name} is required")
    try:
        parsed_decimal = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be an integer") from error
    if (
        parsed_decimal != parsed_decimal.to_integral_value()
        or parsed_decimal < 0
        or parsed_decimal > MAX_CREDIT_DEPTH
    ):
        raise ValueError(f"{field_name} must be a non-negative integer")
    return int(parsed_decimal)


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "y", "yes"}:
        return True
    if normalized in {"false", "0", "n", "no"}:
        return False
    raise ValueError(f"{field_name} must be boolean")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval time must include timezone")
        return _as_aware_utc(value)
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("approval time is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("approval time must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("approval time must include timezone")
    return _as_aware_utc(parsed)


def _canonical_money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.quantize(Decimal("0.01")), "f")


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_naive_utc(value: datetime) -> datetime:
    return _as_aware_utc(value).replace(tzinfo=None)


def _timed_out(started_at: datetime | None, now: datetime, timeout_seconds: int) -> bool:
    if started_at is None:
        return False
    return now - _as_aware_utc(started_at) >= timedelta(seconds=timeout_seconds)


def _safe_message_id(value: str) -> str:
    return validate_credit_terms_message_id(value)
