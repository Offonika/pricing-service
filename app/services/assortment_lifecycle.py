from __future__ import annotations

from dataclasses import dataclass, replace
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
COMMERCIAL_MARKS_PROPERTY_NAME = "Коммерческие признаки"
EXCLUSIVE_KIND_PROPERTY_NAME = "Тип эксклюзивности"
EXCLUSIVE_REASON_PROPERTY_NAME = "Причина эксклюзивности"
EXCLUSIVE_CHECKED_AT_PROPERTY_NAME = "Дата проверки эксклюзивности"
EXCLUSIVE_REVIEW_AT_PROPERTY_NAME = "Дата пересмотра эксклюзивности"
EXCLUSIVE_APPROVED_BY_PROPERTY_NAME = "Утвердил эксклюзивность"
EXCLUSIVE_EVIDENCE_PROPERTY_NAME = "Доказательства эксклюзивности"
MANUAL_MIN_STOCK_PROPERTY_NAME = "Ручной минимальный остаток"
AVAILABILITY_RULE_REVIEW_AT_PROPERTY_NAME = "Дата пересмотра правила наличия"

DEFAULT_STATUS_SOURCE = "assortment_lifecycle_v1"
DEFAULT_EXCLUSIVE_REVIEW_PERIOD_DAYS = 30
WORKING_RECEIPT_WINDOW_DAYS = 180
WORKING_MIN_RECEIPTS = 5
# Окна наблюдения спроса. Те же три, что уже считает автозаказ
# (TREND_WINDOW_* в tasks/build_display_auto_order_dry_run.py): держим числа
# одинаковыми, иначе статус и объём заказа начнут спорить о том, что такое
# «последний месяц».
DEMAND_WINDOW_SHORT_DAYS = 30
DEMAND_WINDOW_MEDIUM_DAYS = 90
DEMAND_WINDOW_LONG_DAYS = 180
# Вход в «Растим»: 12 продаж за 180 дней = две продажи в месяц. Решение
# пользователя 2026-08-02 на реальном распределении дисплеев (порог отсекает
# нижние 19%, 246 карточек из 1294). Условие «30 дней на полке» намеренно
# отвергнуто: оно меряет работу закупщика, а не товар, и бьёт по дефицитным
# ходовым позициям (РБ000051864 — 63 продажи за 23 дня на полке).
SALE_MIN_SALES_QTY = Decimal("12")
# Насколько окно должно отличаться от соседнего, чтобы это считалось трендом,
# а не шумом. Тот же множитель, что в автозаказе
# (ACCELERATING_MIN_GROWTH_MULTIPLIER): строгое «чуть больше» ловило шум на
# маленьких числах — реальный пример РБ000064147 (0.1485/0.1341/0.1161,
# формально рост, по факту его нет).
DEMAND_TREND_MIN_CHANGE_MULTIPLIER = Decimal("1.2")
# Обязательное условие вывода в угасание: товар РЕАЛЬНО был на полке. Иначе
# падение продаж означает дефицит, а не спад спроса. Порог тот же, что у гейта
# «Пенсия» в автозаказе (PENSION_CANDIDATE_MIN_DAYS_IN_SALE): 15 дней наличия
# из 90 — решение пользователя, отдельного числа для статусов не заводим.
DECLINE_MIN_DAYS_IN_SALE = Decimal("15")
# «Родился мёртвым»: сколько дней карточка может молчать без единого движения,
# прежде чем попасть к человеку на разбор. 12 месяцев — решение пользователя
# 2026-08-02 на распределении реальных данных (см. _is_dead_born_candidate).
DEAD_BORN_SILENCE_DAYS = 365
# «Пенсия»: сколько дней товар может не продаваться, прежде чем попасть к
# менеджеру на вывод из активного оборота. 18 месяцев — решение пользователя
# 2026-08-02, тот же срок, что и у правила 2c из инвентаризации legacy-правил.
PENSION_SILENCE_DAYS = 548
# «Старт продаж» — статус про НАЧАЛО продаж. Если первая продажа была давно,
# карточка уже не стартует, что бы ни говорило окно наблюдения. Найдено
# 2026-08-02 на РБ000016562 (дисплей iPhone 4): 15703 продажи с 2014 года, вся
# история заказов и поступлений вне окна 24 месяцев — формула видела только
# продажи и ставила «Старт продаж» ветерану с 26 тысячами проданных штук.
SALES_START_MAX_AGE_DAYS = 365
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
    # NB: "Эксклюзив" не является статусом жизненного цикла — это коммерческий
    # признак (CommercialMark.EXCLUSIVE). Ручное значение manual_status="exclusive"
    # конвертируется в коммерческий признак в assortment_lifecycle_facts.py.
    MATRIX = "matrix"
    ON_DEMAND = "on_demand"
    REPLACE_CANDIDATE = "replace_candidate"
    NONLIQUID = "nonliquid"
    DO_NOT_ORDER = "do_not_order"
    # "Пенсия" — товар продавался и заглох. Ручной статус по решению
    # пользователя 2026-08-02: формула только предлагает кандидата, присваивает
    # и снимает человек. Автовозврата нет намеренно — иначе одна случайная
    # продажа вернула бы в оборот то, что вывели осознанно.
    PENSION = "pension"


class ProcurementBehaviorProfile(StrEnum):
    FAST_EXPENSIVE = "fast_expensive"
    SLOW_EXPENSIVE = "slow_expensive"


class CommercialMark(StrEnum):
    EXCLUSIVE = "exclusive"
    OWN_BRAND = "own_brand"
    RARE_MARKET_ITEM = "rare_market_item"
    FLAGSHIP = "flagship"


# Человеческие названия статусов. Решение пользователя 2026-08-02: название
# должно говорить закупщику, ЧТО ДЕЛАТЬ, а не описывать состояние — прежние
# «ПРОДАЖА» и «Рабочий» не подсказывали действие и путались местами.
# ВАЖНО: эти названия свободно меняются, потому что они только для наших
# экранов и отчётов. Значение, уезжающее в 1С, живёт отдельно —
# ONEC_STATUS_VALUE_NAMES ниже.
ASSORTMENT_STATUS_LABELS = {
    AssortmentStatus.FRUIT: "Рассматриваем",
    AssortmentStatus.NEWBORN: "Заказали",
    AssortmentStatus.NEWBORN_NEED: "Добираем",
    AssortmentStatus.NEW_ITEM: "Завезли",
    AssortmentStatus.SALES_START: "Пошли продажи",
    AssortmentStatus.SALE: "Растим",
    AssortmentStatus.WORKING: "Поддерживаем",
    AssortmentStatus.MATRIX: "Держим всегда",
    AssortmentStatus.ON_DEMAND: "Только под заказ",
    AssortmentStatus.REPLACE_CANDIDATE: "Меняем на аналог",
    AssortmentStatus.NONLIQUID: "Выводим",
    AssortmentStatus.DO_NOT_ORDER: "Не закупаем",
    AssortmentStatus.PENSION: "Допродаём",
}

# Прежние названия статусов. Решение пользователя 2026-08-19: на экранах
# показывать действующее название, а прежнее оставлять рядом в скобках —
# иначе закупщик не связывает витрину со старыми отчётами и договорённостями.
# В данных (`status_label`, выгрузки, 1С) остаётся только новое название:
# скобки нужны глазам человека, а не сопоставлению строк.
ASSORTMENT_STATUS_LEGACY_LABELS = {
    AssortmentStatus.FRUIT: "Плод",
    AssortmentStatus.NEWBORN: "Новорожденный",
    AssortmentStatus.NEWBORN_NEED: "ДН / Добор новорождённого",
    AssortmentStatus.NEW_ITEM: "Новинка",
    AssortmentStatus.SALES_START: "СП / Старт продаж",
    AssortmentStatus.SALE: "ПРОДАЖА",
    AssortmentStatus.WORKING: "Рабочий",
    AssortmentStatus.MATRIX: "Матричный",
    AssortmentStatus.ON_DEMAND: "Под заказ",
    AssortmentStatus.REPLACE_CANDIDATE: "Кандидат на замену",
    AssortmentStatus.NONLIQUID: "Кандидат на неликвид",
    AssortmentStatus.DO_NOT_ORDER: "Не закупать",
    AssortmentStatus.PENSION: "Пенсия",
}


def status_legacy_label(status: AssortmentStatus | str | None) -> str:
    """Прежнее название статуса или пустая строка, если его не переименовывали."""

    if status is None:
        return ""
    try:
        normalized = AssortmentStatus(str(status))
    except ValueError:
        return ""
    return ASSORTMENT_STATUS_LEGACY_LABELS.get(normalized, "")


def status_display_label(status: AssortmentStatus | str | None) -> str:
    """Экранное название: действующее плюс прежнее в скобках."""

    if status is None:
        return ""
    try:
        normalized = AssortmentStatus(str(status))
    except ValueError:
        return str(status)
    label = ASSORTMENT_STATUS_LABELS[normalized]
    legacy = ASSORTMENT_STATUS_LEGACY_LABELS.get(normalized, "")
    return f"{label} ({legacy})" if legacy and legacy != label else label

# Значения свойства «Статус ассортимента» для обмена с 1С. Держим отдельно от
# человеческих названий: обмен сломается, если отправить в справочник 1С
# незнакомую строку.
#
# Решение 2026-08-18: в 1С у свойства не было заведено ни одного значения, им
# никогда не пользовались. Поэтому значения заводятся сразу в действующих
# названиях, а старые (`Плод`, `Рабочий`, `Пенсия` и прочие) в учётную систему
# не переносятся. В 1С уходят ТОЛЬКО управленческие метки ниже: стадии лестницы
# считаются на сервере и не выгружаются до отдельного решения о передаче
# изменений.
ONEC_STATUS_VALUE_NAMES = {
    AssortmentStatus.MATRIX: "Держим всегда",
    AssortmentStatus.ON_DEMAND: "Только под заказ",
    AssortmentStatus.REPLACE_CANDIDATE: "Меняем на аналог",
    AssortmentStatus.NONLIQUID: "Выводим",
    AssortmentStatus.DO_NOT_ORDER: "Не закупаем",
    AssortmentStatus.PENSION: "Допродаём",
}

PROCUREMENT_PROFILE_LABELS = {
    ProcurementBehaviorProfile.FAST_EXPENSIVE: "Дорогой быстрый",
    ProcurementBehaviorProfile.SLOW_EXPENSIVE: "Дорогой медленный",
}

COMMERCIAL_MARK_LABELS = {
    CommercialMark.EXCLUSIVE: "Эксклюзив",
    CommercialMark.OWN_BRAND: "Собственная марка",
    CommercialMark.RARE_MARKET_ITEM: "Редкий товар",
    CommercialMark.FLAGSHIP: "Флагман",
}

EXCLUSIVE_KINDS = frozenset(
    {
        "only_in_country",
        "only_among_competitors",
        "own_import",
        "supplier_agreement",
        "own_brand",
    }
)

MANUAL_ASSORTMENT_STATUSES = frozenset(
    {
        AssortmentStatus.MATRIX,
        AssortmentStatus.ON_DEMAND,
        AssortmentStatus.REPLACE_CANDIDATE,
        AssortmentStatus.NONLIQUID,
        AssortmentStatus.DO_NOT_ORDER,
        AssortmentStatus.PENSION,
    }
)


@dataclass(frozen=True)
class AssortmentLifecycleInput:
    nomenclature_code: str
    created_at: date | None = None
    first_supplier_order_at: date | None = None
    supplier_order_cargo_handoff_dates: tuple[date, ...] = ()
    receipt_dates: tuple[date, ...] = ()
    # Дата первой реализации покупателю. Определяет вход в СП / Старт продаж:
    # решение 2026-08-02 — статус называется "стартанули продажи" и должен
    # означать факт продажи, а не второй заказ поставщику. Пусто = продаж не
    # было (не путать с "нет данных": сборщик фактов всегда заполняет поле).
    first_sale_at: date | None = None
    # Дата последней реализации покупателю. Нужна правилу «Пенсия»: товар
    # продавался и заглох. Пусто = продаж не было вообще (тогда это не Пенсия,
    # а «Родился мёртвым» или обычный Плод — см. решение 2026-08-02).
    last_sale_at: date | None = None
    # Дата расчёта. Нужна правилу «Родился мёртвым»: без неё формула не может
    # сказать, сколько карточка уже молчит. Пусто — правило не применяется
    # (детерминированность важнее: функция не смотрит на системные часы).
    as_of: date | None = None
    # Продано штук за окна 30/90/180 дней, брутто (возвраты не вычитаем — тот
    # же «спрос брутто», что уже принят для расчёта количества заказа).
    # None = данных нет (старый факт/фикстура), не путать с «продаж не было».
    sales_qty_short: Decimal | int | str | None = None
    sales_qty_medium: Decimal | int | str | None = None
    sales_qty_long: Decimal | int | str | None = None
    # Дни, когда товар реально лежал на полке, за те же три окна (среднее по
    # физическим точкам продаж). Нужны, чтобы отличить угасание спроса от
    # дефицита: пустая полка занижает продажи, но это не спад.
    days_in_sale_short: Decimal | int | str | None = None
    days_in_sale_medium: Decimal | int | str | None = None
    days_in_sale_long: Decimal | int | str | None = None
    # Статус, присвоенный на прошлом расчёте. Нужен гистерезису: для входа в
    # рост достаточно двух окон, для вывода в угасание нужны три, а плоская
    # карточка остаётся там, где стояла, и не дёргается от прогона к прогону.
    previous_status: AssortmentStatus | str | None = None
    has_need_signal: bool = False
    working_confirmed_by_folder_responsible: bool = False
    analog_winner_confirmed_by_folder_responsible: bool = False
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


@dataclass(frozen=True)
class CommercialMarksInput:
    nomenclature_code: str
    commercial_marks: tuple[CommercialMark | str, ...] = ()
    exclusive_kind: str = ""
    exclusive_confidence: str = ""
    exclusive_checked_at: date | None = None
    exclusive_review_at: date | None = None
    exclusive_review_period_days: int = DEFAULT_EXCLUSIVE_REVIEW_PERIOD_DAYS
    exclusive_reason: str = ""
    exclusive_approved_by: str = ""
    exclusive_evidence_refs: tuple[str, ...] = ()
    exclusive_min_stock_qty: Decimal | int | str | None = None


@dataclass(frozen=True)
class CommercialMarksDecision:
    nomenclature_code: str
    commercial_marks: tuple[CommercialMark, ...]
    commercial_mark_labels: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    manual_review_required: bool = False
    exclusive_kind: str = ""
    exclusive_confidence: str = ""
    exclusive_checked_at: date | None = None
    exclusive_review_at: date | None = None
    exclusive_reason: str = ""
    exclusive_approved_by: str = ""
    exclusive_evidence_refs: tuple[str, ...] = ()
    exclusive_min_stock_qty: Decimal | None = None


def decide_assortment_status(item: AssortmentLifecycleInput) -> AssortmentLifecycleDecision:
    """Return the v1 assortment lifecycle status from immutable product events."""

    _require_nomenclature_code(item.nomenclature_code)
    manual_status = _normalize_status(item.manual_status)
    if manual_status is not None:
        return _manual_status_decision(item, manual_status)

    decision = _decide_by_events(item)
    # «Пенсия» не меняет вычисленный статус, а вешает на него рекомендацию:
    # присваивает и снимает статус человек (решение 2026-08-02). Проверка идёт
    # поверх готового решения, потому что кандидат может оказаться в любом
    # рабочем статусе — от Новинки до Рабочего.
    if _is_pension_candidate(item):
        silent_days = (item.as_of - item.last_sale_at).days
        return replace(
            decision,
            reason_codes=(*decision.reason_codes, "pension_candidate"),
            reason_text=(
                f"Товар продавался, но последней продаже {silent_days} дней "
                f"(порог {PENSION_SILENCE_DAYS}), новых поставок за этот срок не было. "
                "Кандидат в «Пенсию»: автозаказ выключить, остаток допродать до нуля. "
                "Статус присваивает менеджер."
            ),
            recommended_status=AssortmentStatus.PENSION,
            requires_human_approval=True,
            manual_review_required=True,
            auto_order_allowed=False,
        )
    return decision


def _decide_by_events(item: AssortmentLifecycleInput) -> AssortmentLifecycleDecision:

    # Ветка "победитель группы аналогов -> сразу Рабочий" снята 2026-08-02.
    # Консолидация по аналогам отменена целиком 2026-07-26 (каждый SKU считается
    # независимо), но этот флаг продолжал раздавать статус Рабочий в обход
    # формулы: из 132 карточек в Рабочем 93 держались только на нём, без
    # необходимых 5 поступлений. Поле входа сохранено для совместимости с
    # конфигом ручных решений, но на статус больше не влияет.

    cargo_dates = tuple(sorted(item.supplier_order_cargo_handoff_dates))
    receipt_dates = tuple(sorted(item.receipt_dates))
    first_cargo_at = cargo_dates[0] if cargo_dates else None
    second_cargo_at = cargo_dates[1] if len(cargo_dates) >= 2 else None

    if first_cargo_at is not None:
        if _demand_data_available(item):
            status, reason_code, reason_text = _demand_stage(item)
            return _decision(
                item,
                status,
                reason_code,
                reason_text=reason_text,
                auto_order_allowed=status in (AssortmentStatus.SALE, AssortmentStatus.WORKING),
                changed_at=item.manual_changed_at,
                approved_by=item.manual_approved_by.strip(),
            )
        # Данных о спросе нет (старый факт, фикстура, вызов из витрины
        # «Формирование заказа») — работаем по прежней, поставочной логике.
        # Отдельная ветка нужна, чтобы «нет цифр продаж» не читалось как
        # «продаж не было»: иначе карточка молча съехала бы с «Растим» на
        # «Завезли».
        working_receipts = _receipt_dates_in_working_window(receipt_dates, first_cargo_at)
        if len(working_receipts) >= WORKING_MIN_RECEIPTS:
            return _decision(
                item,
                AssortmentStatus.WORKING,
                "working_receipts_reached",
                "demand_data_missing",
                reason_text=(
                    f"За {WORKING_RECEIPT_WINDOW_DAYS} дней от Новинки есть "
                    f"{len(working_receipts)} поступлений — товар подтвердил повторяемость. "
                    "Цифры продаж по окнам не переданы, статус определён по поставкам."
                ),
                auto_order_allowed=True,
                changed_at=item.manual_changed_at,
                approved_by=item.manual_approved_by.strip(),
            )
        reason_code, reason_text = _post_cargo_reason(
            second_cargo_at, receipt_dates, item.first_sale_at, item.as_of
        )
        post_cargo_status = _post_cargo_status(
            second_cargo_at, receipt_dates, item.first_sale_at, item.as_of
        )
        return _decision(
            item,
            post_cargo_status,
            reason_code,
            "demand_data_missing",
            reason_text=reason_text,
            auto_order_allowed=post_cargo_status is AssortmentStatus.SALE,
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

    if _is_dead_born_candidate(item):
        silent_days = (item.as_of - item.created_at).days
        return _decision(
            item,
            AssortmentStatus.FRUIT,
            "product_created",
            "dead_born_candidate",
            reason_text=(
                f"Карточка заведена {silent_days} дней назад и не дала ни одного движения: "
                f"нет заказа поставщику, поступления и продажи. Порог "
                f"{DEAD_BORN_SILENCE_DAYS} дней пройден — кандидат «Родился мёртвым», "
                "нужна ручная проверка нового спроса."
            ),
            recommended_status=AssortmentStatus.DO_NOT_ORDER,
            requires_human_approval=True,
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


def _demand_data_available(item: AssortmentLifecycleInput) -> bool:
    """Переданы ли цифры продаж по всем трём окнам.

    Проверяем именно наличие поля, а не «больше нуля»: 0 продаж за окно — это
    факт спроса, а отсутствие поля — отсутствие данных. Путать их нельзя, на
    этом уже обжигались в автозаказе (trend_data_available).
    """
    return all(
        _to_optional_decimal(value) is not None
        for value in (item.sales_qty_short, item.sales_qty_medium, item.sales_qty_long)
    )


def _demand_stage(item: AssortmentLifecycleInput) -> tuple[AssortmentStatus, str, str]:
    """Ступень лестницы после первого карго — по динамике спроса, не по поставкам.

    Решение пользователя 2026-08-02: «Растим» и «Поддерживаем» описывают товар,
    а не работу закупщика, поэтому определять их числом поступлений неверно.
      * «Пошли продажи» -> «Растим»: 12 продаж за 180 дней;
      * «Растим» -> «Поддерживаем»: спад по трём окнам И товар был на полке;
      * «Поддерживаем» -> «Растим»: рост по двум окнам;
      * ни рост, ни спад — статус не меняется (гистерезис по прошлому статусу).
    Асимметрия «два окна на рост, три на спад» намеренная: ошибочно объявить
    товар угасающим дороже, чем ошибочно дать ему вырасти.
    """
    previous_status = _previous_status(item.previous_status)
    sales_long = _to_optional_decimal(item.sales_qty_long) or Decimal("0")
    trend, trend_text = _demand_trend(item)
    already_grown = previous_status in (AssortmentStatus.SALE, AssortmentStatus.WORKING)
    proven_demand = sales_long >= SALE_MIN_SALES_QTY or already_grown

    if proven_demand:
        if trend == "declining":
            days_on_shelf = _to_optional_decimal(item.days_in_sale_medium)
            if days_on_shelf is None:
                return (
                    previous_status or AssortmentStatus.SALE,
                    "availability_data_missing",
                    (
                        f"Продажи падают ({trend_text}), но дней наличия по товару нет "
                        "в данных — присутствие на полке не доказано. Понижать статус "
                        "нельзя: это может быть дефицит, а не спад спроса."
                    ),
                )
            if days_on_shelf >= DECLINE_MIN_DAYS_IN_SALE:
                return (
                    AssortmentStatus.WORKING,
                    "demand_declining",
                    (
                        f"Спрос падает три окна подряд ({trend_text}), и товар при этом "
                        "был на полке — это угасание, а не дефицит. Поддерживаем без "
                        "запаса: закрываем спрос и не больше."
                    ),
                )
            return (
                previous_status or AssortmentStatus.SALE,
                "demand_declining_without_shelf_presence",
                (
                    f"Продажи падают ({trend_text}), но товара почти не было на полке "
                    f"(меньше {DECLINE_MIN_DAYS_IN_SALE} дней наличия за "
                    f"{DEMAND_WINDOW_MEDIUM_DAYS} дней). Это дефицит, а не спад спроса — "
                    "статус не понижаем."
                ),
            )
        if trend == "growing":
            return (
                AssortmentStatus.SALE,
                "demand_growing",
                (
                    f"Спрос растёт ({trend_text}). Растим: заправляем магазин полностью, "
                    "при необходимости с излишком."
                ),
            )
        if already_grown and previous_status is not None:
            return (
                previous_status,
                "demand_stable",
                (
                    f"Спрос держится ровно ({trend_text}) — ни роста, ни спада по окнам "
                    f"{DEMAND_WINDOW_SHORT_DAYS}/{DEMAND_WINDOW_MEDIUM_DAYS}/"
                    f"{DEMAND_WINDOW_LONG_DAYS} дней. Статус оставляем прежним."
                ),
            )
        return (
            AssortmentStatus.SALE,
            "sales_threshold_reached",
            (
                f"Продано {_format_qty(sales_long)} шт за {DEMAND_WINDOW_LONG_DAYS} дней "
                f"(порог {_format_qty(SALE_MIN_SALES_QTY)}) — спрос подтверждён. Растим."
            ),
        )

    if item.first_sale_at is not None:
        if _sales_start_expired(item.first_sale_at, item.as_of):
            return (
                AssortmentStatus.WORKING,
                "sales_history_beyond_start",
                (
                    "Первая продажа была давно, а за последние полгода спрос ниже порога "
                    f"({_format_qty(sales_long)} шт из {_format_qty(SALE_MIN_SALES_QTY)}): "
                    "карточка не стартует заново. Поддерживаем без запаса."
                ),
            )
        return (
            AssortmentStatus.SALES_START,
            "first_sale_registered",
            (
                f"Продажи пошли ({_format_qty(sales_long)} шт за "
                f"{DEMAND_WINDOW_LONG_DAYS} дней), но порога "
                f"{_format_qty(SALE_MIN_SALES_QTY)} шт для «Растим» ещё нет."
            ),
        )
    return (
        AssortmentStatus.NEW_ITEM,
        "first_supplier_order_handed_to_cargo",
        "Первый заказ поставщику сдан в cargo, продаж ещё не было.",
    )


def _demand_trend(item: AssortmentLifecycleInput) -> tuple[str, str]:
    """Куда идёт спрос: ``growing`` / ``declining`` / ``flat``.

    Сравниваем скорость продаж на трёх окнах. Скорость восстанавливаем мягкой
    формулой (см. ``_soft_availability_rate``), чтобы дни без товара не выдавали
    голодание за падение спроса.
    """
    rate_short = _soft_availability_rate(
        item.sales_qty_short, DEMAND_WINDOW_SHORT_DAYS, item.days_in_sale_short
    )
    rate_medium = _soft_availability_rate(
        item.sales_qty_medium, DEMAND_WINDOW_MEDIUM_DAYS, item.days_in_sale_medium
    )
    rate_long = _soft_availability_rate(
        item.sales_qty_long, DEMAND_WINDOW_LONG_DAYS, item.days_in_sale_long
    )
    trend_text = (
        f"{_format_rate(rate_short)}/{_format_rate(rate_medium)}/{_format_rate(rate_long)} "
        f"шт в день за {DEMAND_WINDOW_SHORT_DAYS}/{DEMAND_WINDOW_MEDIUM_DAYS}/"
        f"{DEMAND_WINDOW_LONG_DAYS} дней"
    )
    # Рост — две ступени: последний месяц заметно быстрее квартала. Условие
    # rate_short > 0 обязательно: без него карточка без единой продажи прошла бы
    # проверку 0 >= 0 и объявилась растущей.
    if rate_short > 0 and rate_short >= rate_medium * DEMAND_TREND_MIN_CHANGE_MULTIPLIER:
        return "growing", trend_text
    # Спад — три ступени подряд: месяц медленнее квартала, квартал медленнее
    # полугода. Одного окна мало: короткое окно шумит, а объявить товар
    # угасающим по ошибке дороже, чем передержать его в «Растим».
    if (
        rate_short * DEMAND_TREND_MIN_CHANGE_MULTIPLIER <= rate_medium
        and rate_medium * DEMAND_TREND_MIN_CHANGE_MULTIPLIER <= rate_long
    ):
        return "declining", trend_text
    return "flat", trend_text


def _soft_availability_rate(
    sales_qty: Decimal | int | str | None,
    calendar_days: int,
    days_in_sale: Decimal | int | str | None,
) -> Decimal:
    """Скорость продаж с мягким восстановлением дней без товара.

    Утверждённая методология (спека `assortment-status-legacy-rule-inventory.md`,
    раздел 1, решение 2026-07-20): дни без остатка не выбрасываются из
    знаменателя, а достраиваются виртуальными продажами по фактической скорости
    окна::

        виртуальные = дни_без_остатка × (продажи / календарные_дни)
        скорость    = (продажи + виртуальные) / календарные_дни

    Жёсткий вариант (``продажи / дни_наличия``) намеренно не используется: на
    реальных данных он расходится с мягким в 29 раз и уже приводил к росту
    ежедневного заказа в 18 раз. Нет данных о днях наличия — считаем по
    календарю, то есть просто не применяем поправку.
    """
    if calendar_days <= 0:
        return Decimal("0")
    qty = _to_optional_decimal(sales_qty)
    if qty is None or qty <= 0:
        return Decimal("0")
    window = Decimal(str(calendar_days))
    base_rate = qty / window
    available_days = _to_optional_decimal(days_in_sale)
    if available_days is None or available_days <= 0:
        return base_rate
    days_without_stock = max(Decimal("0"), window - min(available_days, window))
    virtual_qty = days_without_stock * base_rate
    return (qty + virtual_qty) / window


def _previous_status(value: AssortmentStatus | str | None) -> AssortmentStatus | None:
    """Прошлый статус из базы, терпимо к неизвестным значениям.

    В таблице классификации могут лежать статусы прежних версий формулы —
    падать из-за них нельзя: гистерезис не настолько важен, чтобы ронять весь
    расчёт. Неизвестное значение читаем как «прошлого статуса нет».
    """
    try:
        return _normalize_status(value)
    except ValueError:
        return None


def _to_optional_decimal(value: Decimal | int | str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _format_qty(value: Decimal) -> str:
    return str(value.quantize(Decimal("1")))


def _format_rate(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001")))


def _is_pension_candidate(item: AssortmentLifecycleInput) -> bool:
    """Товар продавался, но заглох: последней продаже больше порога.

    Условия по решению пользователя 2026-08-02:
      * продажи БЫЛИ (иначе это «Родился мёртвым», не «Пенсия»);
      * последней продаже больше PENSION_SILENCE_DAYS;
      * новых поставок за тот же срок не было — иначе товар только что завезли
        и он ещё не успел продаться, это не пенсия.

    Формула только помечает кандидата: статус «Пенсия» ручной, присваивает и
    снимает его менеджер. Автовозврата нет намеренно — одна случайная продажа
    не должна возвращать в оборот то, что вывели осознанно.
    """
    if item.as_of is None or item.last_sale_at is None:
        return False
    if (item.as_of - item.last_sale_at).days < PENSION_SILENCE_DAYS:
        return False
    threshold = item.as_of - timedelta(days=PENSION_SILENCE_DAYS)
    if any(receipt_at >= threshold for receipt_at in item.receipt_dates):
        return False
    return True


def _is_dead_born_candidate(item: AssortmentLifecycleInput) -> bool:
    """Карточка молчит дольше порога и не дала ни одного движения.

    Порог 12 месяцев выбран на реальных данных 2026-08-02: из 2097 дисплеев,
    у которых движение было, 95.2% дали его в первые 365 дней. Оставшиеся 4.8%
    (100 карточек) оживали позже, рекорд — 580 дней; дольше 730 дней не ожил
    никто. То есть 12 месяцев отсекают небольшой живой хвост, и решение
    намеренно оставлено человеку: формула только помечает кандидата.

    Статус при этом НЕ меняется — карточка остаётся «Плодом», как и записано в
    procurement-order-auto-order-unified-contour.md: программа открывает
    РМ-кандидата и ставит в очередь, присваивает статус человек.
    """
    if item.as_of is None or item.created_at is None:
        return False
    if item.has_need_signal:
        return False
    moved = (
        item.first_supplier_order_at is not None
        or item.supplier_order_cargo_handoff_dates
        or item.receipt_dates
        or item.first_sale_at is not None
    )
    if moved:
        return False
    return (item.as_of - item.created_at).days >= DEAD_BORN_SILENCE_DAYS


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


def decide_commercial_marks(item: CommercialMarksInput) -> CommercialMarksDecision:
    """Return commercial marks that work next to, not instead of, lifecycle status."""

    _require_nomenclature_code(item.nomenclature_code)
    marks = _normalize_commercial_marks(item.commercial_marks)
    labels = tuple(COMMERCIAL_MARK_LABELS[mark] for mark in marks)
    blockers: list[str] = []

    exclusive_kind = item.exclusive_kind.strip()
    exclusive_confidence = item.exclusive_confidence.strip()
    exclusive_reason = item.exclusive_reason.strip()
    exclusive_approved_by = item.exclusive_approved_by.strip()
    exclusive_evidence_refs = tuple(
        ref.strip() for ref in item.exclusive_evidence_refs if ref.strip()
    )
    exclusive_min_stock_qty: Decimal | None = None
    exclusive_review_at = item.exclusive_review_at

    if CommercialMark.EXCLUSIVE in marks:
        if not exclusive_kind:
            blockers.append("exclusive_kind_required")
        elif exclusive_kind not in EXCLUSIVE_KINDS:
            blockers.append("exclusive_kind_unsupported")
        if item.exclusive_checked_at is None:
            blockers.append("exclusive_checked_at_required")
        if not exclusive_reason:
            blockers.append("exclusive_reason_required")
        if not exclusive_approved_by:
            blockers.append("exclusive_approved_by_required")
        if not exclusive_evidence_refs:
            blockers.append("exclusive_evidence_required")
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
        if exclusive_review_at is None and item.exclusive_checked_at is not None:
            exclusive_review_at = item.exclusive_checked_at + timedelta(
                days=item.exclusive_review_period_days
            )

    return CommercialMarksDecision(
        nomenclature_code=item.nomenclature_code,
        commercial_marks=marks,
        commercial_mark_labels=labels,
        blockers=tuple(blockers),
        manual_review_required=bool(blockers),
        exclusive_kind=exclusive_kind,
        exclusive_confidence=exclusive_confidence,
        exclusive_checked_at=item.exclusive_checked_at,
        exclusive_review_at=exclusive_review_at,
        exclusive_reason=exclusive_reason,
        exclusive_approved_by=exclusive_approved_by,
        exclusive_evidence_refs=exclusive_evidence_refs,
        exclusive_min_stock_qty=exclusive_min_stock_qty,
    )


def build_status_property_update_rows(
    decision: AssortmentLifecycleDecision,
    *,
    source: str = DEFAULT_STATUS_SOURCE,
    changed_at: date | None = None,
) -> tuple[NomenclaturePropertyUpdateRow, ...]:
    if decision.status not in ONEC_STATUS_VALUE_NAMES:
        # Решение 2026-08-18: в 1С уходят только управленческие метки. Стадии
        # лестницы считаются на сервере; их выгрузка требует отдельного решения
        # вместе с передачей одних изменений, а не всего пересчёта.
        raise ValueError(
            "lifecycle stage export to UT 10.3 is disabled; "
            f"status {decision.status.value!r} stays in pricing-service"
        )
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
            new_value_name=ONEC_STATUS_VALUE_NAMES[decision.status],
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
    return tuple(rows)


def build_commercial_mark_property_update_rows(
    decision: CommercialMarksDecision,
    *,
    changed_at: date | None = None,
) -> tuple[NomenclaturePropertyUpdateRow, ...]:
    if not decision.commercial_marks:
        return ()
    if decision.blockers:
        return ()
    effective_date = changed_at or date.today()
    mark_tags = ", ".join(mark.value for mark in decision.commercial_marks)
    mark_labels = ", ".join(decision.commercial_mark_labels)
    suffix = f"{effective_date.isoformat()}:{mark_tags}"
    reason = decision.exclusive_reason or f"Коммерческие признаки: {mark_labels}"
    approved_by = decision.exclusive_approved_by
    rows = [
        NomenclaturePropertyUpdateRow(
            idempotency_key=_idempotency_key(
                decision.nomenclature_code,
                COMMERCIAL_MARKS_PROPERTY_NAME,
                suffix,
            ),
            nomenclature_code=decision.nomenclature_code,
            property_name=COMMERCIAL_MARKS_PROPERTY_NAME,
            value_type="string",
            new_value=mark_tags,
            reason=reason,
            approved_by=approved_by,
        )
    ]
    if CommercialMark.EXCLUSIVE not in decision.commercial_marks:
        return tuple(rows)

    rows.extend(
        [
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    EXCLUSIVE_KIND_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=EXCLUSIVE_KIND_PROPERTY_NAME,
                value_type="string",
                new_value=decision.exclusive_kind,
                reason=reason,
                approved_by=approved_by,
            ),
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    EXCLUSIVE_REASON_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=EXCLUSIVE_REASON_PROPERTY_NAME,
                value_type="string",
                new_value=decision.exclusive_reason,
                reason=reason,
                approved_by=approved_by,
            ),
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    EXCLUSIVE_CHECKED_AT_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=EXCLUSIVE_CHECKED_AT_PROPERTY_NAME,
                value_type="date",
                new_value=decision.exclusive_checked_at,
                reason=reason,
                approved_by=approved_by,
            ),
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    EXCLUSIVE_REVIEW_AT_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=EXCLUSIVE_REVIEW_AT_PROPERTY_NAME,
                value_type="date",
                new_value=decision.exclusive_review_at,
                reason=reason,
                approved_by=approved_by,
            ),
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    EXCLUSIVE_APPROVED_BY_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=EXCLUSIVE_APPROVED_BY_PROPERTY_NAME,
                value_type="string",
                new_value=decision.exclusive_approved_by,
                reason=reason,
                approved_by=approved_by,
            ),
            NomenclaturePropertyUpdateRow(
                idempotency_key=_idempotency_key(
                    decision.nomenclature_code,
                    EXCLUSIVE_EVIDENCE_PROPERTY_NAME,
                    suffix,
                ),
                nomenclature_code=decision.nomenclature_code,
                property_name=EXCLUSIVE_EVIDENCE_PROPERTY_NAME,
                value_type="string",
                new_value="; ".join(decision.exclusive_evidence_refs),
                reason=reason,
                approved_by=approved_by,
            ),
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
            ),
        ]
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
        auto_order_allowed=not blockers
        and manual_status
        not in {
            AssortmentStatus.ON_DEMAND,
            AssortmentStatus.REPLACE_CANDIDATE,
            AssortmentStatus.NONLIQUID,
            AssortmentStatus.DO_NOT_ORDER,
            AssortmentStatus.PENSION,
        },
        blockers=tuple(blockers),
        changed_at=item.manual_changed_at,
        approved_by=item.manual_approved_by.strip(),
    )


def _post_cargo_status(
    second_cargo_at: date | None,
    receipt_dates: tuple[date, ...],
    first_sale_at: date | None = None,
    as_of: date | None = None,
) -> AssortmentStatus:
    # Решение 2026-08-02: вход в СП определяет ФАКТ ПЕРВОЙ ПРОДАЖИ, а не второй
    # заказ поставщику, сданный в cargo. Раньше карточка получала статус
    # "Старт продаж", ничего не продав: на 2026-08-02 таких было 20 из 112.
    if second_cargo_at is not None and any(
        receipt_at >= second_cargo_at for receipt_at in receipt_dates
    ):
        return AssortmentStatus.SALE
    if first_sale_at is not None:
        if _sales_start_expired(first_sale_at, as_of):
            return AssortmentStatus.SALE
        return AssortmentStatus.SALES_START
    return AssortmentStatus.NEW_ITEM


def _sales_start_expired(first_sale_at: date, as_of: date | None) -> bool:
    """Старт продаж давно позади — карточка не может «стартовать» повторно."""
    if as_of is None:
        return False
    return (as_of - first_sale_at).days > SALES_START_MAX_AGE_DAYS


def _post_cargo_reason(
    second_cargo_at: date | None,
    receipt_dates: tuple[date, ...],
    first_sale_at: date | None = None,
    as_of: date | None = None,
) -> tuple[str, str]:
    if second_cargo_at is not None and any(
        receipt_at >= second_cargo_at for receipt_at in receipt_dates
    ):
        return (
            "receipt_after_sales_start",
            "Товар поступил после этапа СП, запускаем режим ПРОДАЖА с недельным анализом.",
        )
    if first_sale_at is not None:
        if _sales_start_expired(first_sale_at, as_of):
            return (
                "sales_history_beyond_start",
                "Первая продажа была давно: карточка уже не стартует, режим ПРОДАЖА.",
            )
        return (
            "first_sale_registered",
            "Есть первая продажа покупателю, запускаем СП / Старт продаж.",
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
    changed_at: date | None = None,
    approved_by: str = "",
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
        changed_at=changed_at,
        approved_by=approved_by,
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


def _normalize_commercial_marks(
    values: Iterable[CommercialMark | str],
) -> tuple[CommercialMark, ...]:
    marks: list[CommercialMark] = []
    seen: set[CommercialMark] = set()
    for value in values:
        if value is None or value == "":
            continue
        mark = value if isinstance(value, CommercialMark) else CommercialMark(str(value).strip())
        if mark not in seen:
            marks.append(mark)
            seen.add(mark)
    return tuple(marks)


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
