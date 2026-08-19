const PROCUREMENT_RISK_LABELS: Record<string, string> = {
  working_confirmation_required: "Переход в «Рабочий» должен подтвердить Омар",
  fact_status_decision: "Рекомендация рассчитана по фактам 1С",
  fact_status_decision_requires_1c_approval: "Изменение свойства требует подтверждения в 1С",
  cargo_handoff_confirmed: "Подтверждена передача товара в груз",
  first_cargo_handoff_confirmed: "Подтверждена первая передача товара в груз",
  new_item_with_confirmed_demand: "По новому товару подтверждена потребность",
  live_item_with_remaining_stock: "Есть продажи и остаток товара",
  live_selling_item: "Товар продаётся",
  catalog_guid_missing: "В каталоге Bitrix нет GUID товара из 1С",
  catalog_product_missing: "Товар не найден в каталоге Bitrix по GUID 1С",
  catalog_xml_id_mismatch: "GUID товара не совпадает с XML_ID каталога Bitrix",
  currency_missing: "Не указана валюта заказа",
  supplier_missing: "Не указан поставщик",
  contract_missing: "Не указан договор поставщика",
  warehouse_missing: "Не указан склад",
  purchase_price_missing: "Не указана закупочная цена",
  manual_review_required: "Требуется ручная проверка",
  ut103_export_blocked: "Передача изменения в 1С заблокирована",
  explicit_demand_required: "Нужна подтверждённая клиентская или ручная потребность",
  status_decision_required: "Выберите решение по статусу",
  nomenclature_code_required: "В карточке нет кода номенклатуры 1С",
  manual_reason_required: "Заполните причину решения",
  manual_approved_by_required: "Укажите, кто утвердил решение",
  manual_changed_at_required: "Укажите дату решения",
  // Сигналы расчёта автозаказа. Без этих подписей интерфейс показывал
  // «Требуется дополнительная проверка» на каждый нераспознанный код —
  // менеджер видел два одинаковых бессмысленных предупреждения в строке.
  defect_rate_above_threshold: "Высокий процент возвратов по браку",
  defect_rate_suspected: "Автозаказ остановлен: высокий процент брака",
  batch_error_suspected: "Подозрение на партийную ошибку (пересорт)",
  structural_floor_starter_order: "Стартовый заказ: товара нет на полке",
  speed_horizon_rule_applied: "Горизонт заказа задан классом скорости",
  speed_tier_manual_review: "Медленная группа — решение за закупщиком",
  speed_tier_accelerating_override: "Медленная группа, но продажи ускоряются",
  pension_candidate_flat_despite_availability: "Товар лежал на полке, но не продавался",
  stockout_guard_triggered: "Остатка не хватит до прихода поставки",
  stockout_demand_uplift_applied: "Спрос поднят: товара не было в наличии",
  incoming_deducted_from_need: "Из потребности вычтен товар в пути",
  order_qty_capped: "Количество ограничено максимумом",
  order_qty_rounded_to_multiple: "Количество округлено до кратности",
  price_batch_minimum_applied: "Количество поднято до минимальной партии",
  price_batch_excess_manual_review: "Излишек партии — нужна проверка",
  no_recent_net_sales: "Продаж за расчётный период не было",
  reserve_more_than_sellable_stock: "Резерв больше свободного остатка",
  not_auto_order_allowed: "Автозаказ по карточке выключен",
  // Сигналы адаптивного расчёта и семейств дисплеев. Без подписей интерфейс
  // показывал «Требуется дополнительная проверка» по три раза в каждой строке.
  adaptive_lead_time_applied: "Срок поставки взят живой, по истории поставщика",
  adaptive_lead_time_sync_ready: "Строка пересчитана по живым срокам и готова к заказу",
  display_family_manual_approval_required:
    "Распределение внутри семейства дисплеев подтверждает закупщик",
  recent_seasonality_adjustment_applied: "Учтена текущая сезонность сборки и доставки",
  accepted_matching_review: "Сопоставление товара принято, нужна проверка",
  manual_accepted_matching_review: "Сопоставление товара принято вручную",
};

export function procurementRiskLabel(code: string) {
  return PROCUREMENT_RISK_LABELS[code] || "Требуется дополнительная проверка";
}
