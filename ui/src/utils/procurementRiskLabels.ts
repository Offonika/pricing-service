const PROCUREMENT_RISK_LABELS: Record<string, string> = {
  working_confirmation_required: "Переход в «Поддерживаем (Рабочий)» должен подтвердить Омар",
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
  // Блокеры самого заказа: без них заказ нельзя передать в 1С.
  supplier_1c_reference_missing: "У поставщика нет ссылки или кода 1С",
  contract_1c_reference_missing: "У договора нет ссылки или кода 1С",
  warehouse_1c_reference_missing: "У склада нет ссылки или кода 1С",
  order_has_no_active_lines: "В заказе не осталось ни одной строки",
  quantity_must_be_positive: "Количество должно быть больше нуля",
  purchase_price_must_be_positive: "Закупочная цена должна быть больше нуля",
  purchase_price_change_over_10_pct: "Закупочная цена изменилась больше чем на 10%",
  supplier_defect_over_10_pct_reliable: "У поставщика подтверждённый брак выше 10%",
  classification_approval_pending: "Изменение классификации ждёт второго согласования",
  bitrix_readback_unrecognized_status: "Из Bitrix вернулся незнакомый статус",
  source_error: "Источник данных вернул ошибку — расчёт неполный",
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
  lead_time_low_confidence_fallback: "Мало фактов по срокам — взят срок по умолчанию",
  lead_time_missing_fallback: "Фактов по срокам нет — взят срок по умолчанию",
  recent_seasonality_adjustment_applied: "Учтена текущая сезонность сборки и доставки",
  availability_history_too_short: "Истории наличия мало — расчёт спроса приблизительный",
  display_family_manual_approval_required:
    "Распределение внутри семейства дисплеев подтверждает закупщик",
  accepted_matching_review: "Сопоставление товара принято, нужна проверка",
  manual_accepted_matching_review: "Сопоставление товара принято вручную",
  // Заказы покупателей и клиенты 3/4/5.
  active_customer_orders_added_to_need: "В потребность добавлены активные заказы покупателей",
  active_customer_orders_exceed_sellable_stock: "Заказов покупателей больше свободного остатка",
  b2b_customer_demand_advisory: "Расчёт по клиентам 3/4/5 показан справочно",
  b2b_client_only_sku: "Товар берут только клиенты 3/4/5",
  b2b_passive_reactivation_not_calibrated: "Возврат уснувших клиентов ещё не откалиброван",
  b2b_customer_demand_profile_stale: "Профиль спроса клиентов устарел",
  b2b_sales_window_not_supported: "Окно продаж не поддержано расчётом по клиентам",
  b2b_customer_demand_source_error: "Источник данных по клиентам 3/4/5 вернул ошибку",
  supplier_order_without_cargo: "Заказ поставщику есть, передачи в груз нет",
  // Доля маркетплейса в продажах.
  critical_marketplace_refusal_nonliquid_risk:
    "Критично: продажи держит маркетплейс — риск неликвида, автозаказ остановлен",
  high_marketplace_refusal_risk:
    "Высокий риск: маркетплейс 50-70% продаж, автозаказ остановлен",
  medium_channel_split_required: "Маркетплейс 30-50% продаж — каналы показаны раздельно",
  watch_order_impact: "Маркетплейс заметно влияет на размер заказа",
};

// Статусы приходят кодом и в блокере вида `classification_blocks_order:pension`.
const BLOCKING_STATUS_LABELS: Record<string, string> = {
  pension: "Допродаём (Пенсия)",
  replace_candidate: "Меняем на аналог (Кандидат на замену)",
  nonliquid: "Выводим (Кандидат на неликвид)",
  do_not_order: "Не закупаем (Не закупать)",
  on_demand: "Только под заказ (Под заказ)",
};

const LINE_PREFIX = /^line_(\d+):(.+)$/;

export function procurementRiskLabel(code: string) {
  const known = PROCUREMENT_RISK_LABELS[code];
  if (known) return known;
  // Блокер строки приезжает в заказ с префиксом номера строки, а решение по
  // статусу — с самим статусом после двоеточия. Без разбора префикса весь
  // список схлопывался в одинаковые «Требуется дополнительная проверка».
  const [prefix, ...rest] = code.split(":");
  if (prefix === "classification_blocks_order" && rest.length) {
    const status = rest.join(":");
    return `Статус «${BLOCKING_STATUS_LABELS[status] || status}» запрещает закупку`;
  }
  const lineMatch = LINE_PREFIX.exec(code);
  if (lineMatch) return procurementRiskLabel(lineMatch[2]);
  return "Требуется дополнительная проверка";
}

export interface ProcurementBlockerGroup {
  text: string;
  lines: number[];
  codes: string[];
}

/**
 * Схлопывает блокеры заказа в понятный список: одинаковая причина по разным
 * строкам показывается один раз с перечнем номеров строк. До этого экран
 * выводил четыре одинаковые фразы подряд, по которым нельзя было понять
 * ни причину, ни где искать проблему.
 */
export function groupProcurementBlockers(blockers: string[]): ProcurementBlockerGroup[] {
  const groups = new Map<string, ProcurementBlockerGroup>();
  for (const blocker of blockers) {
    const lineMatch = LINE_PREFIX.exec(blocker);
    const text = procurementRiskLabel(blocker);
    const group = groups.get(text) || { text, lines: [], codes: [] };
    if (lineMatch) {
      const lineNumber = Number(lineMatch[1]);
      if (Number.isFinite(lineNumber) && !group.lines.includes(lineNumber)) {
        group.lines.push(lineNumber);
      }
    }
    if (!group.codes.includes(blocker)) group.codes.push(blocker);
    groups.set(text, group);
  }
  return [...groups.values()].map((group) => ({
    ...group,
    lines: [...group.lines].sort((left, right) => left - right),
  }));
}

export function procurementBlockerText(group: ProcurementBlockerGroup) {
  if (!group.lines.length) return group.text;
  const numbers = group.lines.join(", ");
  return group.lines.length === 1
    ? `${group.text} — строка ${numbers}`
    : `${group.text} — строки ${numbers}`;
}

/**
 * Схлопывает сигналы строки по подписи: разные коды с одинаковым текстом (в том
 * числе несколько неизвестных) печатались подряд одной и той же фразой.
 * Коды остаются в подсказке, чтобы разработчик видел исходные значения.
 */
export function groupProcurementRiskCodes(codes: string[]): ProcurementBlockerGroup[] {
  const groups = new Map<string, ProcurementBlockerGroup>();
  for (const code of codes) {
    const text = procurementRiskLabel(code);
    const group = groups.get(text) || { text, lines: [], codes: [] };
    if (!group.codes.includes(code)) group.codes.push(code);
    groups.set(text, group);
  }
  return [...groups.values()];
}
