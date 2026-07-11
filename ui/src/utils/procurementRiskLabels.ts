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
};

export function procurementRiskLabel(code: string) {
  return PROCUREMENT_RISK_LABELS[code] || "Требуется дополнительная проверка";
}
