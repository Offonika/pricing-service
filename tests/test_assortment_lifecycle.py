from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.assortment_lifecycle import (
    ASSORTMENT_STATUS_LABELS,
    ASSORTMENT_STATUS_LEGACY_LABELS,
    MANUAL_ASSORTMENT_STATUSES,
    WORKING_MIN_RECEIPTS,
    WORKING_RECEIPT_WINDOW_DAYS,
    AssortmentLifecycleDecision,
    AssortmentLifecycleInput,
    AssortmentStatus,
    CommercialMark,
    CommercialMarksInput,
    ExpensiveProfileInput,
    ManagerNeedSignal,
    ProcurementBehaviorProfile,
    WarehouseSalesPointInput,
    build_commercial_mark_property_update_rows,
    build_procurement_profile_property_update_row,
    classify_expensive_profile,
    decide_assortment_status,
    decide_commercial_marks,
    status_display_label,
    systemic_sales_point_codes,
    validate_manager_need_signal,
)


def _decision(**kwargs) -> AssortmentLifecycleDecision:
    return decide_assortment_status(AssortmentLifecycleInput(nomenclature_code="РБ0001", **kwargs))


def test_assortment_lifecycle_status_ladder() -> None:
    assert _decision().status == AssortmentStatus.FRUIT
    assert _decision(first_supplier_order_at=date(2026, 1, 10)).status == AssortmentStatus.NEWBORN
    assert (
        _decision(first_supplier_order_at=date(2026, 1, 10), has_need_signal=True).status
        == AssortmentStatus.NEWBORN_NEED
    )
    new_item = _decision(supplier_order_cargo_handoff_dates=(date(2026, 1, 20),))
    assert new_item.status == AssortmentStatus.NEW_ITEM
    assert not new_item.auto_order_allowed

    # Второе карго само по себе больше НЕ даёт СП: статус называется "стартанули
    # продажи" и с 2026-08-02 требует факта первой продажи. Без неё карточка
    # остаётся Новинкой (товар едет или лежит, но не продавался).
    second_cargo_no_sale = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20))
    )
    assert second_cargo_no_sale.status == AssortmentStatus.NEW_ITEM

    sales_start = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        first_sale_at=date(2026, 2, 22),
    )
    assert sales_start.status == AssortmentStatus.SALES_START
    assert not sales_start.auto_order_allowed

    sale = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=(date(2026, 2, 25),),
    )
    assert sale.status == AssortmentStatus.SALE
    # Регрессия: до фикса 2026-07-30 эта ветка молча возвращала
    # auto_order_allowed=False (значение по умолчанию в _decision) без единого
    # блокера в reason/blockers — карточки статуса ПРОДАЖА без 5 поступлений
    # выпадали из автозаказа без объяснимой причины.
    assert sale.auto_order_allowed
    assert sale.blockers == ()


def test_working_reached_by_five_receipts_without_manual_confirmation() -> None:
    # Решение 2026-07-20: вход в Рабочий определяют только 5 поступлений за 180
    # дней, ручное подтверждение ответственного снято. Раньше формула сама в
    # Рабочий не пускала никого — карточка уходила в ПРОДАЖА с блокером.
    receipt_dates = (
        date(2026, 1, 25),
        date(2026, 2, 25),
        date(2026, 3, 25),
        date(2026, 4, 25),
        date(2026, 5, 25),
    )

    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=receipt_dates,
    )

    assert decision.status == AssortmentStatus.WORKING
    # demand_data_missing помечает откат на прежнюю, поставочную логику: цифры
    # продаж по окнам в этот вызов не переданы.
    assert decision.reason_codes == ("working_receipts_reached", "demand_data_missing")
    assert decision.auto_order_allowed
    assert decision.blockers == ()
    assert not decision.manual_review_required


def test_four_receipts_do_not_reach_working() -> None:
    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=(
            date(2026, 1, 25),
            date(2026, 2, 25),
            date(2026, 3, 25),
            date(2026, 4, 25),
        ),
    )

    assert decision.status != AssortmentStatus.WORKING


def test_sales_start_requires_first_sale_not_second_cargo() -> None:
    # На 2026-08-02 в статусе СП стояли 20 карточек из 112, не продав ни штуки:
    # старое условие давало "Старт продаж" по второму заказу, сданному в cargo.
    cargo = (date(2026, 1, 20), date(2026, 2, 20))

    without_sale = _decision(supplier_order_cargo_handoff_dates=cargo)
    assert without_sale.status == AssortmentStatus.NEW_ITEM
    assert without_sale.reason_codes == (
        "first_supplier_order_handed_to_cargo",
        "demand_data_missing",
    )

    with_sale = _decision(
        supplier_order_cargo_handoff_dates=cargo,
        first_sale_at=date(2026, 2, 22),
    )
    assert with_sale.status == AssortmentStatus.SALES_START
    assert with_sale.reason_codes == ("first_sale_registered", "demand_data_missing")

    # Одного карго и продажи достаточно: второй заказ для СП больше не нужен.
    single_cargo_with_sale = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20),),
        first_sale_at=date(2026, 2, 1),
    )
    assert single_cargo_with_sale.status == AssortmentStatus.SALES_START

    # Поступление после второго карго по-прежнему переводит в ПРОДАЖА.
    moved_on = _decision(
        supplier_order_cargo_handoff_dates=cargo,
        receipt_dates=(date(2026, 2, 25),),
        first_sale_at=date(2026, 2, 22),
    )
    assert moved_on.status == AssortmentStatus.SALE


def test_analog_winner_flag_no_longer_promotes_to_working() -> None:
    # Консолидация по аналогам отменена 2026-07-26; флаг остался в конфиге, но
    # на статус влиять не должен — иначе карточка получает Рабочий в обход
    # правила 5 поступлений (так держались 93 из 132 карточек).
    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=(date(2026, 2, 25),),
        analog_winner_confirmed_by_folder_responsible=True,
        manual_reason="Лучший аналог группы, расчетная потребность 12 шт.",
        manual_approved_by="Омар",
        manual_changed_at=date(2026, 7, 4),
    )

    assert decision.status != AssortmentStatus.WORKING
    assert "analog_winner_confirmed" not in decision.reason_codes


def test_manual_nonliquid_blocks_auto_order() -> None:
    decision = _decision(
        manual_status="nonliquid",
        manual_reason="Слабый спрос, остаток распродаем вручную.",
        manual_approved_by="Омар",
        manual_changed_at=date(2026, 7, 3),
    )

    assert decision.status == AssortmentStatus.NONLIQUID
    assert not decision.auto_order_allowed
    assert decision.blockers == ()


def test_manual_replace_candidate_blocks_auto_order() -> None:
    decision = _decision(
        manual_status="replace_candidate",
        manual_reason="Есть более сильный аналог, текущий товар не докупаем.",
        manual_approved_by="Омар",
        manual_changed_at=date(2026, 7, 6),
    )

    assert decision.status == AssortmentStatus.REPLACE_CANDIDATE
    assert not decision.auto_order_allowed
    assert decision.blockers == ()


def test_exclusive_is_commercial_mark_with_required_evidence() -> None:
    missing = decide_commercial_marks(
        CommercialMarksInput(
            nomenclature_code="РБ0001",
            commercial_marks=("exclusive",),
        )
    )

    assert missing.commercial_marks == (CommercialMark.EXCLUSIVE,)
    assert set(missing.blockers) == {
        "exclusive_kind_required",
        "exclusive_checked_at_required",
        "exclusive_reason_required",
        "exclusive_approved_by_required",
        "exclusive_evidence_required",
        "exclusive_min_stock_required",
    }
    assert missing.manual_review_required

    valid = decide_commercial_marks(
        CommercialMarksInput(
            nomenclature_code="РБ0001",
            commercial_marks=("exclusive",),
            exclusive_kind="only_in_country",
            exclusive_checked_at=date(2026, 6, 25),
            exclusive_reason="Эксклюзивная позиция, держать наличие",
            exclusive_approved_by="Омар",
            exclusive_evidence_refs=("parser:2026-06-25",),
            exclusive_min_stock_qty="2",
        )
    )

    assert valid.blockers == ()
    assert valid.exclusive_min_stock_qty == Decimal("2")
    assert valid.exclusive_review_at == date(2026, 7, 25)
    rows = build_commercial_mark_property_update_rows(valid, changed_at=date(2026, 6, 25))
    properties = {row.property_name: row for row in rows}
    assert properties["Коммерческие признаки"].new_value == "exclusive"
    assert properties["Тип эксклюзивности"].new_value == "only_in_country"
    assert properties["Ручной минимальный остаток"].new_value == Decimal("2")
    assert properties["Дата пересмотра эксклюзивности"].new_value == date(2026, 7, 25)


def test_expensive_profile_uses_top_quartile_and_route_days() -> None:
    group_values = ("100", "200", "300", "400")

    fast = classify_expensive_profile(
        ExpensiveProfileInput(item_value="300", group_values=group_values, route_days=7)
    )
    assert fast.profile == ProcurementBehaviorProfile.FAST_EXPENSIVE
    assert fast.threshold_value == Decimal("300")
    assert not fast.manual_review_required

    slow = classify_expensive_profile(
        ExpensiveProfileInput(item_value="300", group_values=group_values, route_days=8)
    )
    assert slow.profile == ProcurementBehaviorProfile.SLOW_EXPENSIVE
    assert slow.manual_review_required

    not_expensive = classify_expensive_profile(
        ExpensiveProfileInput(item_value="250", group_values=group_values, route_days=3)
    )
    assert not_expensive.profile is None
    assert not not_expensive.is_expensive


def test_expensive_profile_can_be_assigned_manually_and_exported() -> None:
    decision = classify_expensive_profile(
        ExpensiveProfileInput(
            item_value="50",
            group_values=("100", "200", "300", "400"),
            manual_profile="slow_expensive",
        )
    )

    row = build_procurement_profile_property_update_row(
        "РБ0001",
        decision,
        changed_at=date(2026, 6, 25),
        approved_by="Ответственный за папку",
    )

    assert decision.profile == ProcurementBehaviorProfile.SLOW_EXPENSIVE
    assert row is not None
    assert row.property_name == "Профиль закупочного поведения"
    assert row.new_value_name == "Дорогой медленный"
    assert row.new_value_tag == "slow_expensive"


def test_sales_points_exclude_central_defect_transit_and_non_systematic_sales() -> None:
    assert systemic_sales_point_codes(
        (
            WarehouseSalesPointInput("shop-1"),
            WarehouseSalesPointInput("central", is_central=True),
            WarehouseSalesPointInput("defect", is_defect_warehouse=True),
            WarehouseSalesPointInput("transit", is_transit=True),
            WarehouseSalesPointInput("rare", is_non_systematic_sale=True),
            WarehouseSalesPointInput("closed", sells_systematically=False),
        )
    ) == ("shop-1",)


def test_manager_need_signal_collects_facts_and_flags_suspicious_quantity() -> None:
    decision = validate_manager_need_signal(
        ManagerNeedSignal(
            nomenclature_code="РБ0001",
            manager_id="42",
            quantity="9",
            source="offline_call",
            signal_date=date(2026, 6, 25),
            comment="Клиент спрашивал",
        ),
        suspicious_quantity_threshold="5",
    )

    assert decision.accepted
    assert decision.suspicious
    assert decision.issues == ("suspicious_quantity",)


# --- Контрактные тесты целостности контура статусов (защита от дублирования/дрейфа) ---


def test_every_status_has_label_and_no_orphan_labels() -> None:
    # Каждый статус жизненного цикла обязан иметь человекочитаемую метку,
    # и в словаре меток не должно быть значений вне enum (ловит рассинхрон).
    assert set(ASSORTMENT_STATUS_LABELS) == set(AssortmentStatus)


def test_every_status_keeps_its_previous_name_for_screens() -> None:
    # Решение пользователя 2026-08-19: на экране рядом с действующим названием
    # показывается прежнее, поэтому забытый статус — это потерянная подсказка.
    assert set(ASSORTMENT_STATUS_LEGACY_LABELS) == set(AssortmentStatus)
    assert status_display_label(AssortmentStatus.WORKING) == "Поддерживаем (Рабочий)"
    assert status_display_label("fruit") == "Рассматриваем (Плод)"
    # Данные остаются без скобок: их читает не человек, а сопоставление строк.
    assert ASSORTMENT_STATUS_LABELS[AssortmentStatus.SALE] == "Растим"
    # Неизвестный код не должен обрушить экран.
    assert status_display_label("review") == "review"


def test_manual_statuses_are_subset_of_enum() -> None:
    assert MANUAL_ASSORTMENT_STATUSES <= set(AssortmentStatus)


def test_every_manual_status_classifies_without_error() -> None:
    # Прямой manual_status по каждому ручному статусу должен давать этот же статус
    # и не падать (защита от enum-призраков вроде бывшего EXCLUSIVE).
    for status in MANUAL_ASSORTMENT_STATUSES:
        decision = _decision(
            manual_status=status.value,
            manual_reason="Проверка ручного статуса.",
            manual_approved_by="Омар",
            manual_changed_at=date(2026, 7, 12),
        )
        assert decision.status == status


def test_exclusive_is_not_a_lifecycle_status() -> None:
    # "Эксклюзив" — коммерческий признак (CommercialMark), а не статус лестницы.
    assert not hasattr(AssortmentStatus, "EXCLUSIVE")
    assert "exclusive" not in {status.value for status in AssortmentStatus}
    assert CommercialMark.EXCLUSIVE.value == "exclusive"


def test_working_reason_text_derives_thresholds_from_constants() -> None:
    # Пороги 180/5 должны приходить в текст причины ИЗ констант, а не хардкодом,
    # иначе при смене константы тексты рассинхронизируются ("разработка дважды").
    receipt_dates = (
        date(2026, 1, 25),
        date(2026, 2, 25),
        date(2026, 3, 25),
        date(2026, 4, 25),
        date(2026, 5, 25),
    )
    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20), date(2026, 2, 20)),
        receipt_dates=receipt_dates,
    )
    assert decision.status == AssortmentStatus.WORKING
    assert str(WORKING_RECEIPT_WINDOW_DAYS) in decision.reason_text
    assert str(len(receipt_dates)) in decision.reason_text
    assert len(receipt_dates) >= WORKING_MIN_RECEIPTS


def test_dead_born_candidate_after_twelve_silent_months() -> None:
    # Порог 12 месяцев выбран на данных 2026-08-02: 95.2% дисплеев дают первое
    # движение в первые 365 дней. Формула только помечает кандидата — статус
    # остаётся "Плод", присваивает РМ человек.
    decision = _decision(
        created_at=date(2025, 1, 10),
        as_of=date(2026, 8, 2),
    )

    assert decision.status == AssortmentStatus.FRUIT
    assert "dead_born_candidate" in decision.reason_codes
    assert decision.recommended_status == AssortmentStatus.DO_NOT_ORDER
    assert decision.manual_review_required
    assert decision.requires_human_approval
    assert not decision.auto_order_allowed


def test_young_silent_card_is_not_dead_born() -> None:
    decision = _decision(
        created_at=date(2026, 3, 1),
        as_of=date(2026, 8, 2),
    )

    assert decision.status == AssortmentStatus.FRUIT
    assert "dead_born_candidate" not in decision.reason_codes
    assert not decision.manual_review_required


def test_any_movement_cancels_dead_born_candidate() -> None:
    common = {"created_at": date(2024, 1, 10), "as_of": date(2026, 8, 2)}

    # Заказ поставщику уводит карточку в Новорожденный — молчания больше нет.
    with_order = _decision(first_supplier_order_at=date(2024, 6, 1), **common)
    assert "dead_born_candidate" not in with_order.reason_codes

    # Сигнал спроса тоже отменяет: карточку ждут, а не забыли.
    with_signal = _decision(has_need_signal=True, **common)
    assert "dead_born_candidate" not in with_signal.reason_codes


def test_dead_born_needs_as_of_date() -> None:
    # Без даты расчёта правило молчит: формула не смотрит на системные часы,
    # иначе один и тот же снимок давал бы разный результат в разные дни.
    decision = _decision(created_at=date(2020, 1, 1))

    assert decision.status == AssortmentStatus.FRUIT
    assert "dead_born_candidate" not in decision.reason_codes


def test_pension_candidate_after_eighteen_months_without_sales() -> None:
    # Решение 2026-08-02: товар продавался и заглох — кандидат в «Пенсию».
    # Статус НЕ меняется: присваивает и снимает менеджер, автовозврата нет.
    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2023, 1, 20), date(2023, 3, 20)),
        receipt_dates=(date(2023, 2, 1),),
        first_sale_at=date(2023, 2, 10),
        last_sale_at=date(2024, 6, 1),
        as_of=date(2026, 8, 2),
    )

    assert "pension_candidate" in decision.reason_codes
    assert decision.recommended_status == AssortmentStatus.PENSION
    assert decision.manual_review_required
    assert decision.requires_human_approval
    assert not decision.auto_order_allowed


def test_recent_sale_is_not_pension_candidate() -> None:
    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2026, 1, 20),),
        receipt_dates=(date(2026, 2, 1),),
        first_sale_at=date(2026, 2, 10),
        last_sale_at=date(2026, 6, 1),
        as_of=date(2026, 8, 2),
    )

    assert "pension_candidate" not in decision.reason_codes


def test_fresh_receipt_cancels_pension_candidate() -> None:
    # Товар давно не продавался, но его только что завезли — значит закупка
    # считает его живым, и выводить рано.
    decision = _decision(
        supplier_order_cargo_handoff_dates=(date(2023, 1, 20),),
        receipt_dates=(date(2023, 2, 1), date(2026, 5, 1)),
        first_sale_at=date(2023, 2, 10),
        last_sale_at=date(2024, 6, 1),
        as_of=date(2026, 8, 2),
    )

    assert "pension_candidate" not in decision.reason_codes


def test_never_sold_card_is_not_pension() -> None:
    # Без единой продажи это «Родился мёртвым», а не «Пенсия».
    decision = _decision(created_at=date(2024, 1, 1), as_of=date(2026, 8, 2))

    assert "pension_candidate" not in decision.reason_codes
    assert "dead_born_candidate" in decision.reason_codes


def test_pension_manual_status_blocks_auto_order() -> None:
    decision = _decision(
        manual_status="pension",
        manual_reason="Продавался, заглох на два года. Остаток допродаём.",
        manual_approved_by="Омар",
        manual_changed_at=date(2026, 8, 2),
    )

    assert decision.status == AssortmentStatus.PENSION
    assert decision.status_label == "Допродаём"
    assert not decision.auto_order_allowed
    assert decision.blockers == ()


def test_old_sales_history_is_not_sales_start() -> None:
    # РБ000016562 (дисплей iPhone 4): 15703 продажи с 2014 года, вся история
    # заказов и поступлений вне окна наблюдения. Формула видела только продажи
    # и ставила ветерану «Старт продаж».
    veteran = _decision(
        first_sale_at=date(2014, 1, 21),
        last_sale_at=date(2026, 7, 27),
        supplier_order_cargo_handoff_dates=(date(2016, 4, 13),),
        as_of=date(2026, 8, 2),
    )

    assert veteran.status == AssortmentStatus.SALE
    assert "sales_history_beyond_start" in veteran.reason_codes


def test_old_sales_history_without_recent_supplier_window_is_not_fruit() -> None:
    veteran = _decision(
        first_sale_at=date(2014, 1, 21),
        last_sale_at=date(2026, 1, 10),
        as_of=date(2026, 8, 2),
        sales_qty_short=Decimal("0"),
        sales_qty_medium=Decimal("0"),
        sales_qty_long=Decimal("0"),
    )

    assert veteran.status == AssortmentStatus.WORKING
    assert "sales_history_beyond_start" in veteran.reason_codes


def test_historical_supplier_activity_without_sales_requires_review() -> None:
    veteran = _decision(
        first_supplier_order_at=date(2016, 3, 20),
        historical_first_cargo_handoff_at=date(2016, 4, 1),
        as_of=date(2026, 8, 2),
        sales_qty_short=Decimal("0"),
        sales_qty_medium=Decimal("0"),
        sales_qty_long=Decimal("0"),
    )

    assert veteran.status == AssortmentStatus.WORKING
    assert veteran.reason_codes == ("historical_supplier_activity_without_sales",)
    assert veteran.manual_review_required
    assert not veteran.auto_order_allowed


def test_recent_first_sale_still_gives_sales_start() -> None:
    fresh = _decision(
        first_sale_at=date(2026, 5, 1),
        last_sale_at=date(2026, 7, 27),
        supplier_order_cargo_handoff_dates=(date(2026, 3, 1),),
        as_of=date(2026, 8, 2),
    )

    assert fresh.status == AssortmentStatus.SALES_START
    assert "first_sale_registered" in fresh.reason_codes


def test_onec_receives_management_marks_only() -> None:
    # Решение 2026-08-18: в 1С уходят только управленческие метки. Стадии
    # лестницы считаются на сервере, их выгрузка требует отдельного решения.
    # Значения заводятся в действующих названиях: в справочнике 1С у свойства
    # не было ни одного значения, переносить отменённые названия незачем.
    from app.services.assortment_lifecycle import ONEC_STATUS_VALUE_NAMES

    assert set(ASSORTMENT_STATUS_LABELS) == set(AssortmentStatus)
    assert set(ONEC_STATUS_VALUE_NAMES) == MANUAL_ASSORTMENT_STATUSES
    assert ONEC_STATUS_VALUE_NAMES[AssortmentStatus.PENSION] == "Допродаём"
    assert ONEC_STATUS_VALUE_NAMES[AssortmentStatus.DO_NOT_ORDER] == "Не закупаем"
    for stage in (
        AssortmentStatus.FRUIT,
        AssortmentStatus.NEW_ITEM,
        AssortmentStatus.SALE,
        AssortmentStatus.WORKING,
    ):
        assert stage not in ONEC_STATUS_VALUE_NAMES


# --- Переходы по динамике спроса (решение пользователя 2026-08-02) ------------
# «Растим» и «Поддерживаем» описывают товар, а не работу закупщика, поэтому
# определяются продажами, а не числом поступлений.

_CARGO = (date(2026, 1, 20), date(2026, 2, 20))


def _demand_decision(**kwargs) -> AssortmentLifecycleDecision:
    """Решение с полным набором цифр спроса, чтобы не сработал откат на поставки."""
    demand = {
        "supplier_order_cargo_handoff_dates": _CARGO,
        "first_sale_at": date(2026, 2, 25),
        "as_of": date(2026, 8, 1),
        "sales_qty_short": Decimal("0"),
        "sales_qty_medium": Decimal("0"),
        "sales_qty_long": Decimal("0"),
    }
    demand.update(kwargs)
    return _decision(**demand)


def test_sale_entered_by_twelve_sales_not_by_receipts() -> None:
    # Порог 12 продаж за 180 дней = две продажи в месяц.
    below = _demand_decision(
        sales_qty_short=Decimal("2"),
        sales_qty_medium=Decimal("6"),
        sales_qty_long=Decimal("11"),
    )
    assert below.status == AssortmentStatus.SALES_START

    reached = _demand_decision(
        sales_qty_short=Decimal("2"),
        sales_qty_medium=Decimal("6"),
        sales_qty_long=Decimal("12"),
    )
    assert reached.status == AssortmentStatus.SALE
    assert reached.auto_order_allowed


def test_five_receipts_no_longer_grant_working_when_demand_is_known() -> None:
    # Прежнее правило (5 поступлений за 180 дней) снято: поставки больше не
    # решают, растёт товар или угасает.
    receipts = (
        date(2026, 1, 25),
        date(2026, 2, 25),
        date(2026, 3, 25),
        date(2026, 4, 25),
        date(2026, 5, 25),
    )
    decision = _demand_decision(
        receipt_dates=receipts,
        sales_qty_short=Decimal("8"),
        sales_qty_medium=Decimal("20"),
        sales_qty_long=Decimal("40"),
    )
    assert decision.status == AssortmentStatus.SALE
    assert "working_receipts_reached" not in decision.reason_codes


def test_declining_demand_moves_to_working_only_when_item_was_on_shelf() -> None:
    # Продажи падают три окна подряд: 0.067 / 0.2 / 0.333 шт в день.
    declining = {
        "sales_qty_short": Decimal("2"),
        "sales_qty_medium": Decimal("18"),
        "sales_qty_long": Decimal("60"),
        "previous_status": AssortmentStatus.SALE,
    }

    on_shelf = _demand_decision(
        **declining,
        days_in_sale_short=Decimal("30"),
        days_in_sale_medium=Decimal("90"),
        days_in_sale_long=Decimal("180"),
    )
    assert on_shelf.status == AssortmentStatus.WORKING
    assert on_shelf.reason_codes == ("demand_declining",)

    # Тот же спад, но товар почти не лежал на полке (10% дней) — это дефицит,
    # а не угасание спроса, статус понижать нельзя.
    starved = _demand_decision(
        **declining,
        days_in_sale_short=Decimal("3"),
        days_in_sale_medium=Decimal("9"),
        days_in_sale_long=Decimal("18"),
    )
    assert starved.status == AssortmentStatus.SALE
    assert starved.reason_codes == ("demand_declining_without_shelf_presence",)

    # Дней наличия нет в данных вообще — присутствие на полке не доказано,
    # понижать статус нельзя (fail-safe из docs/specs/assortment-lifecycle-
    # policy.md; до 2026-08-09 код в этом случае понижал до «Поддерживаем»).
    unknown = _demand_decision(**declining)
    assert unknown.status == AssortmentStatus.SALE
    assert unknown.reason_codes == ("availability_data_missing",)


def test_growing_demand_returns_from_working_to_sale() -> None:
    # Рост определяется двумя окнами: месяц заметно быстрее квартала.
    decision = _demand_decision(
        sales_qty_short=Decimal("20"),
        sales_qty_medium=Decimal("30"),
        sales_qty_long=Decimal("40"),
        previous_status=AssortmentStatus.WORKING,
    )
    assert decision.status == AssortmentStatus.SALE
    assert decision.reason_codes == ("demand_growing",)


def test_flat_demand_keeps_previous_status() -> None:
    # Ни рост, ни спад: карточка не должна мигать между статусами от прогона
    # к прогону.
    flat = {
        "sales_qty_short": Decimal("5"),
        "sales_qty_medium": Decimal("15"),
        "sales_qty_long": Decimal("30"),
        "days_in_sale_medium": Decimal("60"),
    }
    assert _demand_decision(**flat, previous_status=AssortmentStatus.SALE).status == (
        AssortmentStatus.SALE
    )
    assert _demand_decision(**flat, previous_status=AssortmentStatus.WORKING).status == (
        AssortmentStatus.WORKING
    )


def test_growth_ignores_card_without_any_sales() -> None:
    # 0 >= 0 не должно читаться как рост.
    decision = _demand_decision(previous_status=AssortmentStatus.SALE)
    assert decision.status != AssortmentStatus.SALE or decision.reason_codes != ("demand_growing",)
    assert "demand_growing" not in decision.reason_codes


def test_unknown_previous_status_does_not_break_calculation() -> None:
    # В таблице классификации могут лежать статусы прежних версий формулы.
    decision = _demand_decision(
        sales_qty_short=Decimal("2"),
        sales_qty_medium=Decimal("6"),
        sales_qty_long=Decimal("20"),
        previous_status="exclusive",
    )
    assert decision.status == AssortmentStatus.SALE


def test_days_without_stock_are_restored_softly_not_by_exclusion() -> None:
    # Мягкая формула (утверждена 2026-07-20): дни без товара достраиваются
    # виртуальными продажами, а не выбрасываются из знаменателя. Жёсткий
    # вариант (продажи / дни наличия) дал бы совсем другую скорость.
    from app.services.assortment_lifecycle import _soft_availability_rate

    soft = _soft_availability_rate(Decimal("30"), 90, Decimal("30"))
    hard = Decimal("30") / Decimal("30")
    calendar = Decimal("30") / Decimal("90")
    assert calendar < soft < hard
