from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from math import ceil
from typing import Iterable

from app.services.exporters.ut103_nomenclature_properties import NomenclaturePropertyUpdateRow

STATUS_PROPERTY_NAME = "Статус ассортимента"
STATUS_REASON_PROPERTY_NAME = "Причина статуса ассортимента"
STATUS_CHANGED_AT_PROPERTY_NAME = "Дата изменения статуса ассортимента"
STATUS_SOURCE_PROPERTY_NAME = "Источник статуса ассортимента"
STATUS_APPROVED_BY_PROPERTY_NAME = "Утвердил статус ассортимента"
PROCUREMENT_PROFILE_PROPERTY_NAME = "Профиль закупочного поведения"
MANUAL_MIN_STOCK_PROPERTY_NAME = "Ручной минимальный остаток"
AVAILABILITY_RULE_REVIEW_AT_PROPERTY_NAME = "Дата пересмотра правила наличия"

DEFAULT_STATUS_SOURCE = "assortment_lifecycle_v1"
DEFAULT_EXCLUSIVE_REVIEW_PERIOD_DAYS = 30
WORKING_RECEIPT_WINDOW_DAYS = 180
WORKING_MIN_RECEIPTS = 5
FAST_EXPENSIVE_MAX_ROUTE_DAYS = 7
EXPENSIVE_TOP_QUARTILE = Decimal("0.75")


class AssortmentStatus(StrEnum):
    FRUIT = "fruit"
    NEWBORN = "newborn"
    NEWBORN_NEED = "newborn_need"
    NEW_ITEM = "new_item"
    SALES_START = "sales_start"
    SALE = "sale"
    WORKING = "working"
    EXCLUSIVE = "exclusive"
    MATRIX = "matrix"
    ON_DEMAND = "on_demand"
    REPLACE_CANDIDATE = "replace_candidate"
    NONLIQUID = "nonliquid"
    DO_NOT_ORDER = "do_not_order"


class ProcurementBehaviorProfile(StrEnum):
    FAST_EXPENSIVE = "fast_expensive"
    SLOW_EXPENSIVE = "slow_expensive"


ASSORTMENT_STATUS_LABELS = {
    AssortmentStatus.FRUIT: "Плод",
    AssortmentStatus.NEWBORN: "Новорожденный",
    AssortmentStatus.NEWBORN_NEED: "ПН / Потребность новорожденного",
    AssortmentStatus.NEW_ITEM: "Новинка",
    AssortmentStatus.SALES_START: "СП / Старт продаж",
    AssortmentStatus.SALE: "ПРОДАЖА",
    AssortmentStatus.WORKING: "Рабочий",
    AssortmentStatus.EXCLUSIVE: "Эксклюзив",
    AssortmentStatus.MATRIX: "Матричный",
    AssortmentStatus.ON_DEMAND: "Под заказ",
    AssortmentStatus.REPLACE_CANDIDATE: "Кандидат на замену",
    AssortmentStatus.NONLIQUID: "Кандидат на неликвид",
    AssortmentStatus.DO_NOT_ORDER: "Не закупать",
}

PROCUREMENT_PROFILE_LABELS = {
    ProcurementBehaviorProfile.FAST_EXPENSIVE: "Дорогой быстрый",
    ProcurementBehaviorProfile.SLOW_EXPENSIVE: "Дорогой медленный",
}

MANUAL_ASSORTMENT_STATUSES = frozenset(
    {
        AssortmentStatus.EXCLUSIVE,
        AssortmentStatus.MATRIX,
        AssortmentStatus.ON_DEMAND,
        AssortmentStatus.NONLIQUID,
        AssortmentStatus.DO_NOT_ORDER,
    }
)


@dataclass(frozen=True)
class AssortmentLifecycleInput:
    nomenclature_code: str
    created_at: date | None = None
    first_supplier_order_at: date | None = None
    supplier_order_cargo_handoff_dates: tuple[date, ...] = ()
    receipt_dates: tuple[date, ...] = ()
    has_need_signal: bool = False
    working_confirmed_by_folder_responsible: bool = False
    manual_status: AssortmentStatus | str | None = None
    manual_reason: str = ""
    manual_approved_by: str = ""
    manual_changed_at: date | None = None
    exclusive_min_stock_qty: Decimal | int | str | None = None
    exclusive_review_period_days: int = DEFAULT_EXCLUSIVE_REVIEW_PERIOD_DAYS


@dataclass(frozen=True)
class AssortmentLifecycleDecision:
    nomenclature_code: str
    status: AssortmentStatus
    status_label: str
    reason_codes: tuple[str, ...]
    reason_text: str
    recommended_status: AssortmentStatus | None = None
    requires_human_approval: bool = False
    manual_review_required: bool = False
    auto_order_allowed: bool = False
    blockers: tuple[str, ...] = ()
    changed_at: date | None = None
    approved_by: str = ""
    exclusive_review_at: date | None = None
    exclusive_min_stock_qty: Decimal | None = None

    @property
    def recommended_status_label(self) -> str:
        if self.recommended_status is None:
            return ""
        return ASSORTMENT_STATUS_LABELS[self.recommended_status]


@dataclass(frozen=True)
class ExpensiveProfileInput:
    item_value: Decimal | int | str
    group_values: tuple[Decimal | int | str, ...]
    route_days: int | None = None
    manual_profile: ProcurementBehaviorProfile | str | None = None


@dataclass(frozen=True)
class ExpensiveProfileDecision:
    profile: ProcurementBehaviorProfile | None
    profile_label: str
    threshold_value: Decimal | None
    item_value: Decimal
    is_expensive: bool
    reason_codes: tuple[str, ...]
    manual_review_required: bool = False


@dataclass(frozen=True)
class WarehouseSalesPointInput:
    warehouse_code: str
    sells_systematically: bool = True
    is_central: bool = False
    is_defect_warehouse: bool = False
    is_transit: bool = False
    is_non_systematic_sale: bool = False


@dataclass(frozen=True)
class ManagerNeedSignal:
    nomenclature_code: str
    manager_id: str
    quantity: Decimal | int | str
    source: str
    signal_date: date
    comment: str = ""


@dataclass(frozen=True)
class ManagerNeedSignalDecision:
    signal: ManagerNeedSignal
    normalized_quantity: Decimal
    accepted: bool
    suspicious: bool
    issues: tuple[str, ...] = ()


def decide_assortment_status(item: AssortmentLifecycleInput) -> AssortmentLifecycleDecision:
    """Return the v1 assortment lifecycle status from immutable product events."""

    _require_nomenclature_code(item.nomenclature_code)
    manual_status = _normalize_status(item.manual_status)
    if manual_status is not None:
        return _manual_status_decision(item, manual_status)

    cargo_dates = tuple(sorted(item.supplier_order_cargo_handoff_dates))
    receipt_dates = tuple(sorted(item.receipt_dates))
    first_cargo_at = cargo_dates[0] if cargo_dates else None
    second_cargo_at = cargo_dates[1] if len(cargo_dates) >= 2 else None

    if first_cargo_at is not None:
        working_receipts = _receipt_dates_in_working_window(receipt_dates, first_cargo_at)
        reached_working = len(working_receipts) >= WORKING_MIN_RECEIPTS
        if reached_working and item.working_confirmed_by_folder_responsible:
            return _decision(
                item,
                AssortmentStatus.WORKING,
                "working_confirmed",
                reason_text=(
                    f"За {WORKING_RECEIPT_WINDOW_DAYS} дней от Новинки есть "
                    f"{len(working_receipts)} поступлений, ответственный за папку подтвердил Рабочий."
                ),
                auto_order_allowed=True,
            )
        if reached_working:
            fallback_status = _post_cargo_status(second_cargo_at, receipt_dates)
            return _decision(
                item,
                fallback_status,
                "working_confirmation_required",
                reason_text=(
                    f"Товар набрал {len(working_receipts)} поступлений за "
                    f"{WORKING_RECEIPT_WINDOW_DAYS} дней от Новинки, нужен ответственный за папку."
                ),
                recommended_status=AssortmentStatus.WORKING,
                requires_human_approval=True,
                manual_review_required=True,
                blockers=("working_confirmation_required",),
            )
        reason_code, reason_text = _post_cargo_reason(second_cargo_at, receipt_dates)
        return _decision(
            item,
            _post_cargo_status(second_cargo_at, receipt_dates),
            reason_code,
            reason_text=reason_text,
        )

    if item.has_need_signal and item.first_supplier_order_at is not None:
        return _decision(
            item,
            AssortmentStatus.NEWBORN_NEED,
            "newborn_need_signal",
            reason_text="Есть сигнал спроса по новорожденному товару, нужно передать менеджеру/закупщику.",
            manual_review_required=True,
        )

    if item.first_supplier_order_at is not None:
        return _decision(
            item,
            AssortmentStatus.NEWBORN,
            "first_supplier_order_created",
            reason_text="Появился первый заказ поставщику, контролируем первое движение товара.",
            manual_review_required=True,
        )

    reason_codes = ("product_created",)
    reason_text = "Номенклатура создана, первого заказа поставщику еще нет."
    if item.has_need_signal:
        reason_codes = ("product_created", "need_signal_before_first_supplier_order")
        reason_text = (
            "Номенклатура создана и уже есть сигнал спроса, но первого заказа поставщику еще нет."
        )
    return _decision(
        item,
        AssortmentStatus.FRUIT,
        *reason_codes,
        reason_text=reason_text,
        manual_review_required=item.has_need_signal,
    )


def classify_expensive_profile(item: ExpensiveProfileInput) -> ExpensiveProfileDecision:
    """Classify expensive goods as only fast-expensive or slow-expensive."""

    value = _to_decimal(item.item_value, "item_value")
    manual_profile = _normalize_profile(item.manual_profile)
    if manual_profile is not None:
        return ExpensiveProfileDecision(
            profile=manual_profile,
            profile_label=PROCUREMENT_PROFILE_LABELS[manual_profile],
            threshold_value=None,
            item_value=value,
            is_expensive=True,
            reason_codes=("manual_profile",),
            manual_review_required=manual_profile == ProcurementBehaviorProfile.SLOW_EXPENSIVE,
        )

    group_values = tuple(sorted(_to_decimal(raw, "group_values") for raw in item.group_values))
    if not group_values:
        raise ValueError("group_values is required")
    threshold = _top_quartile_threshold(group_values)
    if value < threshold:
        return ExpensiveProfileDecision(
            profile=None,
            profile_label="",
            threshold_value=threshold,
            item_value=value,
            is_expensive=False,
            reason_codes=("below_expensive_threshold",),
        )

    if item.route_days is not None and item.route_days <= FAST_EXPENSIVE_MAX_ROUTE_DAYS:
        profile = ProcurementBehaviorProfile.FAST_EXPENSIVE
        reason_codes = ("top_quartile_value", "route_up_to_7_days")
    else:
        profile = ProcurementBehaviorProfile.SLOW_EXPENSIVE
        reason_codes = (
            ("top_quartile_value", "route_unknown_or_over_7_days")
            if item.route_days is None
            else ("top_quartile_value", "route_over_7_days")
        )
    return ExpensiveProfileDecision(
        profile=profile,
        profile_label=PROCUREMENT_PROFILE_LABELS[profile],
        threshold_value=threshold,
        item_value=value,
        is_expensive=True,
        reason_codes=reason_codes,
        manual_review_required=profile == ProcurementBehaviorProfile.SLOW_EXPENSIVE,
    )


def is_systemic_sales_point(warehouse: WarehouseSalesPointInput) -> bool:
    return (
        warehouse.sells_systematically
        and not warehouse.is_central
        and not warehouse.is_defect_warehouse
        and not warehouse.is_transit
        and not warehouse.is_non_systematic_sale
    )


def systemic_sales_point_codes(warehouses: Iterable[WarehouseSalesPointInput]) -> tuple[str, ...]:
    return tuple(
        warehouse.warehouse_code
        for warehouse in warehouses
        if warehouse.warehouse_code.strip() and is_systemic_sales_point(warehouse)
    )


def validate_manager_need_signal(
    signal: ManagerNeedSignal,
    *,
    suspicious_quantity_threshold: Decimal | int | str | None = None,
) -> ManagerNeedSignalDecision:
    _require_nomenclature_code(signal.nomenclature_code)
    quantity = _to_decimal(signal.quantity, "quantity")
    issues: list[str] = []
    if not signal.manager_id.strip():
        issues.append("manager_id_required")
    if quantity <= 0:
        issues.append("quantity_must_be_positive")
    if not signal.source.strip():
        issues.append("source_required")
    threshold = (
        _to_decimal(suspicious_quantity_threshold, "suspicious_quantity_threshold")
        if suspicious_quantity_threshold is not None
        else None
    )
    suspicious = bool(threshold is not None and quantity > threshold)
    if suspicious:
        issues.append("suspicious_quantity")
    return ManagerNeedSignalDecision(
        signal=signal,
        normalized_quantity=quantity,
        accepted=not issues or issues == ["suspicious_quantity"],
        suspicious=suspicious,
        issues=tuple(issues),
    )


def build_status_property_update_rows(
    decision: AssortmentLifecycleDecision,
    *,
    source: str = DEFAULT_STATUS_SOURCE,
    changed_at: date | None = None,
) -> tuple[NomenclaturePropertyUpdateRow, ...]:
    effective_date = changed_at or decision.changed_at or date.today()
    reason = decision.reason_text
    approved_by = decision.approved_by
    suffix = f"{effective_date.isoformat()}:{decision.status.value}"
    rows = [
        NomenclaturePropertyUpdateRow(
            idempotency_key=_idempotency_key(
                decision.nomenclature_code,
                STATUS_PROPERTY_NAME,
                suffix,
            ),
            nomenclature_code=decision.nomenclature_code,
            property_name=STATUS_PROPERTY_NAME,
            value_type="property_value",
            new_value_name=decision.status_label,
            new_value_tag=decision.status.value,
            reason=reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=_idempotency_key(
                decision.nomenclature_code,
                STATUS_REASON_PROPERTY_NAME,
                suffix,
            ),
            nomenclature_code=decision.nomenclature_code,
            property_name=STATUS_REASON_PROPERTY_NAME,
            value_type="string",
            new_value=reason,
            reason=reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=_idempotency_key(
                decision.nomenclature_code,
                STATUS_CHANGED_AT_PROPERTY_NAME,
                suffix,
            ),
            nomenclature_code=decision.nomenclature_code,
            property_name=STATUS_CHANGED_AT_PROPERTY_NAME,
            value_type="date",
            new_value=effective_date,
            reason=reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=_idempotency_key(
                decision.nomenclature_code,
                STATUS_SOURCE_PROPERTY_NAME,
                suffix,
            ),
            nomenclature_code=decision.nomenclature_code,
            property_name=STATUS_SOURCE_PROPERTY_NAME,
            value_type="string",
            new_value=source,
            reason=reason,
            approved_by=approved_by,
        ),
    ]
    if approved_by:
        rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    STATUS_APPROVED_BY_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=STATUS_APPROVED_BY_PROPERTY_NAME,
                value_type="string",
                new_value=approved_by,
                reason=reason,
                approved_by=approved_by,
            )
        )
    if decision.status == AssortmentStatus.EXCLUSIVE:
        if decision.exclusive_min_stock_qty is not None:
            rows.append(
                NomenclaturePropertyUpdateRow(
                    idempotency_key=_idempotency_key(
                        decision.nomenclature_code,
                        MANUAL_MIN_STOCK_PROPERTY_NAME,
                        suffix,
                    ),
                    nomenclature_code=decision.nomenclature_code,
                    property_name=MANUAL_MIN_STOCK_PROPERTY_NAME,
                    value_type="number",
                    new_value=decision.exclusive_min_stock_qty,
                    reason=reason,
                    approved_by=approved_by,
                )
            )
        if decision.exclusive_review_at is not None:
            rows.append(
                NomenclaturePropertyUpdateRow(
                    idempotency_key=_idempotency_key(
                        decision.nomenclature_code,
                        AVAILABILITY_RULE_REVIEW_AT_PROPERTY_NAME,
                        suffix,
                    ),
                    nomenclature_code=decision.nomenclature_code,
                    property_name=AVAILABILITY_RULE_REVIEW_AT_PROPERTY_NAME,
                    value_type="date",
                    new_value=decision.exclusive_review_at,
                    reason=reason,
                    approved_by=approved_by,
                )
            )
    return tuple(rows)


def build_procurement_profile_property_update_row(
    nomenclature_code: str,
    decision: ExpensiveProfileDecision,
    *,
    changed_at: date | None = None,
    reason: str = "",
    approved_by: str = "",
) -> NomenclaturePropertyUpdateRow | None:
    if decision.profile is None:
        return None
    _require_nomenclature_code(nomenclature_code)
    effective_date = changed_at or date.today()
    profile_reason = reason or "; ".join(decision.reason_codes)
    return NomenclaturePropertyUpdateRow(
        idempotency_key=_idempotency_key(
            nomenclature_code,
            PROCUREMENT_PROFILE_PROPERTY_NAME,
            f"{effective_date.isoformat()}:{decision.profile.value}",
        ),
        nomenclature_code=nomenclature_code,
        property_name=PROCUREMENT_PROFILE_PROPERTY_NAME,
        value_type="property_value",
        new_value_name=decision.profile_label,
        new_value_tag=decision.profile.value,
        reason=profile_reason,
        approved_by=approved_by,
    )


def _manual_status_decision(
    item: AssortmentLifecycleInput,
    manual_status: AssortmentStatus,
) -> AssortmentLifecycleDecision:
    if manual_status not in MANUAL_ASSORTMENT_STATUSES:
        raise ValueError(f"manual_status must be a manual status, got: {manual_status}")
    blockers: list[str] = []
    if not item.manual_reason.strip():
        blockers.append("manual_reason_required")
    if not item.manual_approved_by.strip():
        blockers.append("manual_approved_by_required")
    if item.manual_changed_at is None:
        blockers.append("manual_changed_at_required")

    exclusive_min_stock_qty: Decimal | None = None
    exclusive_review_at: date | None = None
    if manual_status == AssortmentStatus.EXCLUSIVE:
        if item.exclusive_review_period_days <= 0:
            blockers.append("exclusive_review_period_must_be_positive")
        if item.exclusive_min_stock_qty is None:
            blockers.append("exclusive_min_stock_required")
        else:
            exclusive_min_stock_qty = _to_decimal(
                item.exclusive_min_stock_qty, "exclusive_min_stock_qty"
            )
            if exclusive_min_stock_qty <= 0:
                blockers.append("exclusive_min_stock_must_be_positive")
        if item.manual_changed_at is not None and item.exclusive_review_period_days > 0:
            exclusive_review_at = item.manual_changed_at + timedelta(
                days=item.exclusive_review_period_days
            )

    status_label = ASSORTMENT_STATUS_LABELS[manual_status]
    reason = item.manual_reason.strip() or f"Ручное решение: {status_label}."
    return AssortmentLifecycleDecision(
        nomenclature_code=item.nomenclature_code,
        status=manual_status,
        status_label=status_label,
        reason_codes=("manual_status",),
        reason_text=reason,
        requires_human_approval=True,
        manual_review_required=bool(blockers),
        auto_order_allowed=not blockers and manual_status not in {AssortmentStatus.DO_NOT_ORDER},
        blockers=tuple(blockers),
        changed_at=item.manual_changed_at,
        approved_by=item.manual_approved_by.strip(),
        exclusive_review_at=exclusive_review_at,
        exclusive_min_stock_qty=exclusive_min_stock_qty,
    )


def _post_cargo_status(
    second_cargo_at: date | None, receipt_dates: tuple[date, ...]
) -> AssortmentStatus:
    if second_cargo_at is not None:
        if any(receipt_at >= second_cargo_at for receipt_at in receipt_dates):
            return AssortmentStatus.SALE
        return AssortmentStatus.SALES_START
    return AssortmentStatus.NEW_ITEM


def _post_cargo_reason(
    second_cargo_at: date | None, receipt_dates: tuple[date, ...]
) -> tuple[str, str]:
    if second_cargo_at is not None:
        if any(receipt_at >= second_cargo_at for receipt_at in receipt_dates):
            return (
                "receipt_after_sales_start",
                "Товар поступил после этапа СП, запускаем режим ПРОДАЖА с недельным анализом.",
            )
        return (
            "second_supplier_order_handed_to_cargo",
            "Второй заказ поставщику сдан в cargo, запускаем СП / Старт продаж.",
        )
    return (
        "first_supplier_order_handed_to_cargo",
        "Первый заказ поставщику сдан в cargo, товар стал Новинкой.",
    )


def _receipt_dates_in_working_window(
    receipt_dates: tuple[date, ...],
    first_cargo_at: date,
) -> tuple[date, ...]:
    window_end = first_cargo_at + timedelta(days=WORKING_RECEIPT_WINDOW_DAYS)
    return tuple(
        receipt_at for receipt_at in receipt_dates if first_cargo_at <= receipt_at <= window_end
    )


def _decision(
    item: AssortmentLifecycleInput,
    status: AssortmentStatus,
    *reason_codes: str,
    reason_text: str,
    recommended_status: AssortmentStatus | None = None,
    requires_human_approval: bool = False,
    manual_review_required: bool = False,
    auto_order_allowed: bool = False,
    blockers: tuple[str, ...] = (),
) -> AssortmentLifecycleDecision:
    return AssortmentLifecycleDecision(
        nomenclature_code=item.nomenclature_code,
        status=status,
        status_label=ASSORTMENT_STATUS_LABELS[status],
        reason_codes=tuple(reason_codes),
        reason_text=reason_text,
        recommended_status=recommended_status,
        requires_human_approval=requires_human_approval,
        manual_review_required=manual_review_required,
        auto_order_allowed=auto_order_allowed,
        blockers=blockers,
    )


def _normalize_status(value: AssortmentStatus | str | None) -> AssortmentStatus | None:
    if value is None or value == "":
        return None
    if isinstance(value, AssortmentStatus):
        return value
    try:
        return AssortmentStatus(str(value))
    except ValueError as error:
        raise ValueError(f"unknown assortment status: {value}") from error


def _normalize_profile(
    value: ProcurementBehaviorProfile | str | None,
) -> ProcurementBehaviorProfile | None:
    if value is None or value == "":
        return None
    if isinstance(value, ProcurementBehaviorProfile):
        return value
    try:
        return ProcurementBehaviorProfile(str(value))
    except ValueError as error:
        raise ValueError(f"unknown procurement behavior profile: {value}") from error


def _top_quartile_threshold(values: tuple[Decimal, ...]) -> Decimal:
    index = max(0, ceil(len(values) * EXPENSIVE_TOP_QUARTILE) - 1)
    return values[index]


def _to_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a decimal value, got: {value}") from error
    return result


def _require_nomenclature_code(value: str) -> None:
    if not value.strip():
        raise ValueError("nomenclature_code is required")


def _idempotency_key(nomenclature_code: str, property_name: str, suffix: str) -> str:
    return f"nom-prop:{nomenclature_code}:{property_name}:{suffix}"
