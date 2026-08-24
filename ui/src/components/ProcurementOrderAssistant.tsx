import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import {
  assembleProcurementOrderProjects,
  approveProcurementClassification,
  confirmProcurementMatchingReview,
  fetchProcurementOrderAssistant,
  rejectProcurementClassification,
  updateProcurementSupplierProfile,
  type ProcurementOrderAssistant,
  type ProcurementOrderFormation,
  type ProcurementOrderFormationLine,
  type ProcurementSupplierProfile,
} from "../api/procurementAssortment";
import { procurementErrorText } from "../utils/procurementErrorMessages";
import { procurementRiskLabel } from "../utils/procurementRiskLabels";
import "../orderAssistant.css";

interface Props {
  onOpenOrder?: (orderId: number, lineId?: number) => void;
}

type QuickFilter =
  | "all"
  | "ready"
  | "supplier-missing"
  | "price-changed"
  | "low-profitability"
  | "high-defect"
  | "photo-missing";

interface AssistantRow {
  key: string;
  order: ProcurementOrderFormation;
  line: ProcurementOrderFormationLine;
}

// Подсказка объясняет, что именно отбирает счётчик: без неё «Можно собрать 31»
// читалось как непонятная кнопка.
const QUICK_FILTERS: Array<{ key: QuickFilter; label: string; hint: string }> = [
  { key: "all", label: "Все", hint: "Все активные строки очереди; исключённые строки скрыты" },
  {
    key: "ready",
    label: "Можно собрать",
    hint: "Строки без блокеров: есть поставщик, карточка товара и фото. Отметьте их галочками и нажмите «Собрать проекты» — помощник соберёт из них проекты заказов поставщикам.",
  },
  { key: "supplier-missing", label: "Без поставщика", hint: "У заказа не заполнен поставщик — собрать проект нельзя" },
  { key: "price-changed", label: "Цена изменилась", hint: "Закупочная цена отличается от прошлой закупки больше чем на 10%" },
  { key: "low-profitability", label: "Рентабельность ниже нормы", hint: "Рентабельность по фактам 1С ниже 20%" },
  { key: "high-defect", label: "Подтверждённый брак >10%", hint: "Подтверждённый брак поставщика выше 10% на достаточной истории" },
  { key: "photo-missing", label: "Без фото", hint: "Нет фото или карточки товара — строка не идёт в сборку" },
];

const SOURCE_LEVEL_LABELS: Record<string, string> = {
  sku: "Карточка товара",
  supplier: "Поставщик",
  route: "Маршрут",
  category: "Категория",
};

const CONFIDENCE_LABELS: Record<string, string> = {
  reliable: "Надёжная",
  high: "Высокая",
  medium: "Средняя",
  low: "Низкая",
};

const QUALITY_SEGMENT_LABELS: Record<string, string> = {
  new: "Новый",
  original: "Оригинал",
  service: "Сервисный",
};

const CONSTRUCTION_SEGMENT_LABELS: Record<string, string> = {
  with_frame: "С рамкой",
  without_frame: "Без рамки",
  no_frame: "Без рамки",
};

function sourceLevelLabel(value?: string | null) {
  return value ? SOURCE_LEVEL_LABELS[value] || "Другой источник" : "Источник не определён";
}

function confidenceLabel(value?: string | null) {
  return value ? CONFIDENCE_LABELS[value] || "Не оценена" : "Не оценена";
}

function qualitySegmentLabel(value?: string | null) {
  return value ? QUALITY_SEGMENT_LABELS[value] || "Не определён" : "Не определён";
}

function constructionSegmentLabel(value?: string | null) {
  return value ? CONSTRUCTION_SEGMENT_LABELS[value] || "Не определён" : "Не определён";
}

function errorText(error: unknown) {
  return procurementErrorText(error);
}

function numeric(value?: string | number | null) {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value?: string | number | null, currency = "RUB") {
  const parsed = numeric(value);
  if (parsed === null) return "Нет данных";
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(parsed);
}

function percent(value?: string | number | null) {
  const parsed = numeric(value);
  return parsed === null ? "Нет данных" : `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function countLabel(count: number, one: string, few: string, many: string) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return `${count} ${many}`;
  if (last === 1) return `${count} ${one}`;
  if (last >= 2 && last <= 4) return `${count} ${few}`;
  return `${count} ${many}`;
}

function returnLabel(value: unknown) {
  const count = numeric(value as string | number | null);
  return count === null
    ? "Количество возвратов не определено"
    : countLabel(count, "возврат", "возврата", "возвратов");
}

function batchEvidence(line: ProcurementOrderFormationLine) {
  return line.blocker_details?.find((detail) => detail.code === "batch_error_suspected")?.evidence;
}

function quantity(value: string) {
  const parsed = numeric(value);
  return parsed === null
    ? value
    : parsed.toLocaleString("ru-RU", { maximumFractionDigits: 3 });
}

function dateLabel(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("ru-RU").format(parsed);
}

function supplierMissing(order: ProcurementOrderFormation) {
  return !order.supplier_ref && !order.supplier_code;
}

function rowReady(row: AssistantRow) {
  return Boolean(
    !row.line.removed &&
      row.order.status !== "approved" &&
      !supplierMissing(row.order) &&
      row.order.blockers.length === 0 &&
      row.line.blockers.length === 0 &&
      row.line.product_card_url &&
      row.line.photo_original_url
  );
}

function activeOrderRows(order: ProcurementOrderFormation): AssistantRow[] {
  return order.lines
    .filter((line) => !line.removed)
    .map((line) => ({ key: `${order.id}:${line.id}`, order, line }));
}

function orderReady(order: ProcurementOrderFormation) {
  const rows = activeOrderRows(order);
  return rows.length > 0 && rows.every(rowReady);
}

function rowSelectable(row: AssistantRow) {
  return orderReady(row.order);
}

function rowUnavailableReason(row: AssistantRow) {
  if (row.line.removed) return "Потребность исчезла в новом расчёте";
  if (supplierMissing(row.order)) return "Недоступно: не определён поставщик";
  if (row.order.blockers.length > 0) return projectBlockerSummary(row.order);
  if (!row.line.product_card_url || !row.line.photo_original_url) {
    if (!row.line.product_card_url) return "Недоступно: не найдена точная карточка товара";
    return "Недоступно: в галерее карточки пока нет оригинального WebP-фото";
  }
  const blockers = [...row.order.blockers, ...row.line.blockers];
  if (blockers.some((value) => value.includes("purchase_price_change_over_10_pct"))) {
    return "Недоступно: изменение закупочной цены больше 10% требует проверки";
  }
  if (blockers.some((value) => value.includes("supplier_defect_over_10_pct_reliable"))) {
    return "Недоступно: подтверждённый брак поставщика выше 10% на надёжной базе";
  }
  if (blockers.some((value) => value.includes("classification_approval_pending"))) {
    return "Недоступно: ожидается решение по классификации";
  }
  if (row.line.blockers.length) return row.line.blocker_details?.[0]?.message || "Строка заблокирована";
  if (!orderReady(row.order)) return "Недоступно: другая строка проекта ещё не готова";
  return "";
}

function blockingLineNumbers(order: ProcurementOrderFormation) {
  const fromDetails = (order.blocker_details || [])
    .map((detail) => detail.line_number)
    .filter((value): value is number => typeof value === "number");
  if (fromDetails.length > 0) return [...new Set(fromDetails)].sort((a, b) => a - b);
  return order.lines.filter((line) => line.blockers.length > 0).map((line) => line.line_number);
}

function blockerShortLabel(code?: string) {
  if (code === "batch_error_suspected") return "подозрение на партийную ошибку";
  if (code === "defect_rate_suspected") return "подтверждённый высокий процент брака";
  if (code === "supplier_defect_over_10_pct_reliable") return "брак поставщика выше порога";
  if (code === "purchase_price_change_over_10_pct") return "изменение закупочной цены требует проверки";
  return code ? procurementRiskLabel(code).toLocaleLowerCase("ru") : "есть блокирующие условия";
}

function projectBlockerSummary(order: ProcurementOrderFormation) {
  const lineNumbers = blockingLineNumbers(order);
  const first = (order.blocker_details || []).find((detail) => detail.line_number != null);
  const reason = blockerShortLabel(first?.code);
  const lines = lineNumbers.length > 0 ? ` — строки ${lineNumbers.join(", ")}` : "";
  return `Проект №${order.id} заблокирован: ${reason}${lines}`;
}

function projectLabel(count: number) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return "проектов заказов";
  if (last === 1) return "проект заказа";
  if (last >= 2 && last <= 4) return "проекта заказов";
  return "проектов заказов";
}

function reasonLabel(count: number) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return `${count} причин`;
  if (last === 1) return `${count} причина`;
  if (last >= 2 && last <= 4) return `${count} причины`;
  return `${count} причин`;
}

function problemLineLabel(count: number) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return `${count} проблемных строк`;
  if (last === 1) return `${count} проблемная строка`;
  if (last >= 2 && last <= 4) return `${count} проблемные строки`;
  return `${count} проблемных строк`;
}

function blockerReasonCount(order: ProcurementOrderFormation) {
  const detailCodes = (order.blocker_details || []).map((detail) => detail.code).filter(Boolean);
  const rawCodes = order.blockers.map((value) => value.split(":").at(-1) || value);
  return new Set(detailCodes.length > 0 ? detailCodes : rawCodes).size;
}

function firstBlockingLineId(order: ProcurementOrderFormation) {
  const detailLineId = (order.blocker_details || []).find((detail) => detail.line_id != null)?.line_id;
  if (detailLineId != null) return detailLineId;
  const numbers = blockingLineNumbers(order);
  return order.lines.find((line) => numbers.includes(line.line_number))?.id;
}

function currencyLabel(value?: string | null) {
  const normalized = value?.trim().toUpperCase() || "";
  const aliases: Record<string, string> = {
    "156": "CNY",
    "643": "RUB",
    "784": "AED",
    "840": "USD",
    "978": "EUR",
  };
  return aliases[normalized] || normalized;
}

function priceHistoryLabel(line: ProcurementOrderFormationLine) {
  const change = numeric(line.price_change_pct);
  if (change !== null) {
    const history = line.price_history_count ? ` · ${line.price_history_count} заказ.` : "";
    return `${change > 0 ? "+" : ""}${percent(change)}${history}`;
  }
  const currency = currencyLabel(line.price_history_expected_currency || line.currency);
  if (line.price_change_status === "currency_mismatch") {
    const available = [...new Set(
      line.price_history_available_currencies?.map(currencyLabel).filter(Boolean) || []
    )].join(", ");
    return available
      ? `Нет истории в ${currency}; есть ${available}`
      : `Нет истории в ${currency}`;
  }
  if (line.price_history_count === 1) return `Только 1 заказ в ${currency}`;
  return `Нет двух заказов в ${currency}`;
}

function supplierSelectionLabel(line: ProcurementOrderFormationLine) {
  const price = numeric(line.supplier_selected_purchase_price);
  const currency = line.supplier_selected_price_currency || line.currency;
  const selectedPrice = price === null ? "" : ` — ${quantity(String(price))} ${currency}`;
  if (line.supplier_selection_reason === "price_guard_over_3pct_then_speed") {
    return `Разница больше 3%: выбран более дешёвый${selectedPrice}`;
  }
  if (line.supplier_selection_reason === "price_tie_within_3pct_speed") {
    return `Цены в пределах 3%: выбран более быстрый${selectedPrice}`;
  }
  if (line.supplier_selection_reason === "main_supplier_from_onec_card") {
    return "Основной поставщик из карточки 1С";
  }
  if (line.supplier_selection_reason === "only_historical_supplier_candidate") {
    return "Единственный поставщик в истории товара";
  }
  if (line.supplier_selection_rule === "historical_evidence_fallback") {
    return "Выбор по истории: сопоставимой цены нет";
  }
  return "Правило выбора не записано";
}

function filterMatches(row: AssistantRow, filter: QuickFilter) {
  const profitability = numeric(row.line.profitability_pct);
  const defect = numeric(row.line.supplier_defect_pct);
  const priceChange = numeric(row.line.price_change_pct);
  // Фильтр показывает ровно то, что уйдёт в сборку: строки готового заказа без
  // исчезнувшей потребности. Иначе счётчик «Можно собрать» не сходился с числом
  // строк в таблице — в неё попадали и снятые строки того же заказа.
  if (filter === "ready") return rowSelectable(row) && !row.line.removed;
  if (filter === "supplier-missing") return supplierMissing(row.order);
  if (filter === "price-changed") return priceChange !== null && priceChange !== 0;
  if (filter === "low-profitability") return profitability !== null && profitability < 20;
  if (filter === "high-defect") {
    return row.line.supplier_defect_attribution === "supplier_exact"
      && (row.line.supplier_defect_history_units || 0) >= 100
      && defect !== null
      && defect > 10;
  }
  if (filter === "photo-missing") return !row.line.photo_original_url;
  return true;
}

function csvCell(value: unknown) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
}

function safeFilePart(value: string) {
  return value.replace(/[^a-zA-Zа-яА-Я0-9_-]+/g, "-").replace(/^-|-$/g, "") || "supplier";
}

function saveTextFile(name: string, content: string, type: string) {
  const blob = new Blob(["\ufeff", content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function downloadSupplierPackage(rows: AssistantRow[], mode: "list" | "photos") {
  if (rows.length === 0) return;
  const supplier = safeFilePart(rows[0].order.supplier_name);
  if (mode === "photos") {
    const header = ["Имя файла", "Код товара", "Карточка товара", "Оригинал фото"].map(csvCell).join(";");
    const body = rows.map(({ line }, index) => [
      `${safeFilePart(line.nomenclature_code || `item-${index + 1}`)}.original`,
      line.nomenclature_code || "",
      line.product_card_url || "",
      line.photo_original_url || "",
    ].map(csvCell).join(";"));
    saveTextFile(`${supplier}-photos.csv`, [header, ...body].join("\r\n"), "text/csv;charset=utf-8");
    toast.success("Файл со ссылками на фотографии подготовлен");
    return;
  }
  const header = [
    "Поставщик",
    "Код товара",
    "Товар",
    "Количество",
    "Цена",
    "Валюта",
    "Карточка товара",
    "Оригинал фото",
  ].map(csvCell).join(";");
  const body = rows.map(({ order, line }) => [
    order.supplier_name,
    line.nomenclature_code || "",
    line.nomenclature_name,
    line.final_quantity,
    line.purchase_price,
    line.currency,
    line.product_card_url || "",
    line.photo_original_url || "",
  ].map(csvCell).join(";"));
  saveTextFile(`${supplier}-order-with-photos.csv`, [header, ...body].join("\r\n"), "text/csv;charset=utf-8");
  toast.success("Файл заказа поставщику подготовлен");
}

function ProductPhoto({ line }: { line: ProcurementOrderFormationLine }) {
  const [failed, setFailed] = useState(false);
  const [source, setSource] = useState(line.photo_thumbnail_url || line.photo_original_url || "");
  if (!line.photo_original_url || failed) {
    return <span className="order-assistant__photo-missing">Нет фото</span>;
  }
  return (
    <a
      aria-label={`Открыть исходное фото: ${line.nomenclature_name}`}
      className="order-assistant__photo-link"
      href={line.photo_original_url}
      rel="noreferrer"
      target="_blank"
      title="Открыть оригинал без сжатия"
    >
      <img
        alt=""
        loading="lazy"
        onError={() => {
          if (source !== line.photo_original_url) setSource(line.photo_original_url || "");
          else setFailed(true);
        }}
        src={source}
      />
      <span>Оригинал</span>
    </a>
  );
}

function supplierClassMeta(profile: ProcurementSupplierProfile) {
  const supplierClass = (profile.qualification_class || "").toUpperCase();
  const token = ["A", "B", "C"].includes(supplierClass) ? supplierClass.toLowerCase() : "unknown";
  const fallback = {
    A: "Лучшие условия и высокая надёжность.",
    B: "Стандартные рабочие условия.",
    C: "Предоплата или повышенный риск.",
  }[supplierClass];
  return {
    label: supplierClass ? `Класс ${supplierClass}` : "Не назначен",
    token,
    description: profile.class_description || profile.qualification_label || fallback || "Поставщик ещё не прошёл ручную оценку.",
  };
}

function averageLineMetric(rows: AssistantRow[], field: "profitability_pct") {
  const values = rows
    .map(({ line }) => numeric(line[field]))
    .filter((value): value is number => value !== null);
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function ClassificationDecision({ row, onRefresh }: { row: AssistantRow; onRefresh: () => Promise<void> }) {
  const proposal = row.line.latest_classification;
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [reasonMissing, setReasonMissing] = useState(false);
  if (!proposal || proposal.status !== "proposed") return null;

  const decide = async (decision: "approve" | "reject") => {
    if (decision === "reject" && !reason.trim()) {
      setReasonMissing(true);
      return;
    }
    setBusy(true);
    try {
      if (decision === "approve") {
        await approveProcurementClassification(row.order.id, row.line.id, proposal.id);
        toast.success("Предложение классификации принято");
      } else {
        await rejectProcurementClassification(row.order.id, row.line.id, proposal.id, {
          expected_order_version: row.order.version,
          expected_line_version: row.line.version,
          reason: reason.trim(),
        });
        toast.success("Предложение классификации отклонено");
      }
      await onRefresh();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="order-assistant__panel-section order-assistant__classification" aria-label="Предложение классификации">
      <h3>Предложение классификации</h3>
      <dl className="order-assistant__classification-facts">
        <div><dt>Автор</dt><dd>{proposal.requested_by_name || proposal.requested_by_bitrix_user_id}</dd></div>
        <div><dt>Причина</dt><dd>{proposal.reason}</dd></div>
        <div><dt>Изменение</dt><dd className="is-good">{proposal.previous_status || "Не задан"} → {proposal.proposed_status_label}</dd></div>
      </dl>
      {(proposal.can_approve || proposal.can_reject) ? (
        <>
          <div className="order-assistant__classification-actions">
            <button className="is-primary" disabled={busy || !proposal.can_approve} onClick={() => void decide("approve")} type="button">Принять</button>
            <button disabled={busy || !proposal.can_reject} onClick={() => void decide("reject")} type="button">Отклонить</button>
          </div>
          <p className="order-assistant__permission-note">Вы согласующий. Автор не может согласовать собственное предложение.</p>
          <label className="order-assistant__rejection-field">Причина отклонения <span>*</span>
            <select
              aria-invalid={reasonMissing}
              onChange={(event) => { setReason(event.target.value); setReasonMissing(false); }}
              value={reason}
            >
              <option value="">Выберите причину</option>
              <option value="Недостаточно подтверждённых данных">Недостаточно подтверждённых данных</option>
              <option value="Условия не подтверждены">Условия не подтверждены</option>
              <option value="Нужна повторная оценка">Нужна повторная оценка</option>
            </select>
          </label>
          {reasonMissing && <p className="order-assistant__field-error">Выберите причину, чтобы отклонить предложение.</p>}
        </>
      ) : (
        <p className="order-assistant__permission-note">Решение доступно согласующему, который не является автором предложения.</p>
      )}
    </section>
  );
}

function SupplierSummaryCard({ rows, onOpen }: { rows: AssistantRow[]; onOpen: () => void }) {
  const order = rows[0].order;
  const profile: ProcurementSupplierProfile = order.supplier_profile || { advantages: [], data_status: "missing" };
  const classMeta = supplierClassMeta(profile);
  const profitability = profile.profitability_pct ?? averageLineMetric(rows, "profitability_pct");
  const total = rows.reduce((sum, { line }) => sum + (numeric(line.amount) || 0), 0);
  const confirmedDefect = profile.defect_attribution === "supplier_exact";
  return (
    <article className="order-assistant__supplier-card">
      <div className="order-assistant__supplier-heading">
        <div><h3>{order.supplier_name || "Без поставщика"}</h3><p>{rows.length} поз. · {money(total, order.currency)}</p></div>
        <button className={`supplier-class supplier-class--${classMeta.token}`} onClick={onOpen} type="button">{classMeta.label}</button>
      </div>
      <p className="order-assistant__qualification">{profile.qualification_label || classMeta.description}</p>
      <dl className="order-assistant__supplier-metrics">
        <div><dt>Рентабельность</dt><dd className={profitability == null ? "is-missing" : ""}>{percent(profitability)}</dd><dd className="order-assistant__metric-note">{profile.profitability_pct != null ? "по истории" : "по подбору"}</dd></div>
        <div><dt>Брак поставщика</dt><dd className={confirmedDefect ? "" : "is-missing"}>{confirmedDefect ? percent(profile.defect_pct) : "Связь с поставкой не подтверждена"}</dd><dd className="order-assistant__metric-note">{profile.defect_history_units ? `${profile.defect_history_units.toLocaleString("ru-RU")} шт. · ${confidenceLabel(profile.defect_confidence)}` : "нет подтверждённой базы"}</dd></div>
        <div><dt>История заказов</dt><dd className={profile.history_order_count == null ? "is-missing" : ""}>{profile.history_order_count == null ? "Нет данных" : `${profile.history_order_count} заказов`}</dd><dd className="order-assistant__metric-note">ценовых наблюдений: {profile.price_history_count ?? "нет"}</dd></div>
      </dl>
      <dl className="order-assistant__terms">
        <div><dt>Оплата по договору 1С</dt><dd>{profile.terms_status === "missing" ? "Не заполнено в 1С" : profile.payment_terms || "Не заполнено в 1С"}</dd></div>
        <div><dt>Отсрочка</dt><dd>{profile.credit_days == null ? "Не заполнено в 1С" : `${profile.credit_days} дней`}{profile.credit_limit ? ` · лимит ${money(profile.credit_limit, order.currency)}` : ""}</dd></div>
        <div><dt>Сборка у поставщика</dt><dd>{profile.supplier_prepare_days == null ? "Нет данных" : `${profile.supplier_prepare_days} дн.`}</dd></div>
        <div><dt>Логистика</dt><dd>{profile.logistics_days == null ? "Нет данных" : `${profile.logistics_days} дн.`}</dd></div>
      </dl>
      <div className="order-assistant__supplier-footer">
        <small>Факты обновлены {dateLabel(profile.facts_updated_at || profile.updated_at)}</small>
        <details>
          <summary>Пакет поставщику</summary>
          <div><button onClick={() => downloadSupplierPackage(rows, "list")} type="button">Список + фото</button><button onClick={() => downloadSupplierPackage(rows, "photos")} type="button">Фото отдельно</button></div>
        </details>
      </div>
    </article>
  );
}

function SupplierPanel({ rows, onClose, onOpenOrder, onRefresh }: { rows: AssistantRow[]; onClose: () => void; onOpenOrder?: (orderId: number) => void; onRefresh: () => Promise<void> }) {
  const order = rows[0].order;
  const profile: ProcurementSupplierProfile = order.supplier_profile || { advantages: [], data_status: "missing" };
  const classMeta = supplierClassMeta(profile);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [profileClass, setProfileClass] = useState(profile.qualification_class || "");
  const [qualificationLabel, setQualificationLabel] = useState(profile.qualification_label || "");
  const [advantages, setAdvantages] = useState(profile.advantages.join("\n"));
  const [internalNote, setInternalNote] = useState(profile.internal_note || "");
  const firstLine = rows[0].line;
  const pendingDecision = rows.find(({ line }) => line.latest_classification?.status === "proposed");
  const ready = orderReady(order);
  const unavailable = rows.find((row) => !rowReady(row));
  const supplierPrepareDays = profile.supplier_prepare_days ?? firstLine.supplier_prepare_days;
  const logisticsDays = profile.logistics_days ?? firstLine.logistics_days;
  const leadTimeDays = profile.lead_time_days ?? firstLine.lead_time_days;
  const confirmedDefect = profile.defect_attribution === "supplier_exact";
  const productDefect = numeric(firstLine.product_defect_pct);
  const productDefectBasis = firstLine.product_defect_history_units;
  const paymentTerms = profile.terms_status === "missing" ? "Не заполнено в 1С" : profile.payment_terms || "Не заполнено в 1С";

  const saveProfile = async () => {
    if (!order.supplier_ref) return;
    setSaving(true);
    try {
      await updateProcurementSupplierProfile(order.supplier_ref, {
        expected_version: profile.version || 0,
        qualification_class: profileClass || null,
        qualification_label: qualificationLabel.trim() || null,
        advantages: advantages.split(/\n|;/).map((value) => value.trim()).filter(Boolean),
        internal_note: internalNote.trim() || null,
      });
      toast.success("Профиль поставщика обновлён");
      setEditing(false);
      await onRefresh();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <aside aria-label={`Профиль поставщика ${order.supplier_name || "без названия"}`} className="order-assistant__supplier-panel">
      <div className="order-assistant__panel-scroll">
        <header className="order-assistant__panel-header">
          <div>
            <div className="order-assistant__supplier-title-row">
              <h2>{order.supplier_name || "Без поставщика"}</h2>
              {onOpenOrder && <button onClick={() => onOpenOrder(order.id)} type="button">Открыть проект <ArrowTopRightOnSquareIcon aria-hidden="true" /></button>}
            </div>
            <div className="order-assistant__class-row"><span className={`supplier-class supplier-class--${classMeta.token}`}>{classMeta.label}</span><span>{classMeta.description}</span></div>
          </div>
          <button aria-label="Закрыть панель поставщика" className="order-assistant__icon-button" onClick={onClose} type="button"><XMarkIcon aria-hidden="true" /></button>
        </header>

        {profile.can_edit && order.supplier_ref && <button className="order-assistant__profile-edit" onClick={() => setEditing((value) => !value)} type="button">{editing ? "Закрыть редактирование" : "Изменить профиль"}</button>}
        {editing && (
          <div className="order-assistant__profile-form">
            <label>Класс<select onChange={(event) => setProfileClass(event.target.value)} value={profileClass}><option value="">Не назначен</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select></label>
            <label>Расшифровка<input onChange={(event) => setQualificationLabel(event.target.value)} value={qualificationLabel} /></label>
            <label>Преимущества<textarea onChange={(event) => setAdvantages(event.target.value)} placeholder="По одному на строку" value={advantages} /></label>
            <label>Внутренний комментарий<textarea onChange={(event) => setInternalNote(event.target.value)} value={internalNote} /></label>
            <button disabled={saving} onClick={() => void saveProfile()} type="button">{saving ? "Сохраняем..." : "Сохранить профиль"}</button>
          </div>
        )}

        <div className={`order-assistant__work-status ${ready ? "is-ready" : "is-attention"}`}>
          {ready ? <CheckCircleIcon aria-hidden="true" /> : <ExclamationTriangleIcon aria-hidden="true" />}
          <div><strong>{ready ? "Можно работать" : "Требует решения"}</strong>{!ready && unavailable && <small>{rowUnavailableReason(unavailable)}</small>}</div>
        </div>

        <dl className="order-assistant__metric-strip">
          <div><dd>{paymentTerms}</dd><dt>оплата</dt></div>
          <div><dd>{profile.credit_days == null ? "Нет данных" : `${profile.credit_days} дней`}</dd><dt>отсрочка</dt></div>
          <div><dd>{leadTimeDays == null ? "Нет данных" : `${leadTimeDays} дней`}</dd><dt>до поступления</dt></div>
          <div><dd>{profile.history_order_count == null ? "Нет данных" : `${profile.history_order_count} заказов`}</dd><dt>история</dt></div>
        </dl>

        {pendingDecision ? <ClassificationDecision onRefresh={onRefresh} row={pendingDecision} /> : (
          <section className="order-assistant__panel-section order-assistant__classification-empty"><h3>Предложение классификации</h3><p>Нет предложений, ожидающих решения.</p></section>
        )}

        <section className="order-assistant__panel-section">
          <h3>Подробности</h3>
          <div className="order-assistant__lead-time-equation">
            <dl><dt>Сборка</dt><dd>{supplierPrepareDays == null ? "Нет данных" : `${supplierPrepareDays} дней`}</dd></dl><span aria-hidden="true">+</span>
            <dl><dt>Логистика</dt><dd>{logisticsDays == null ? "Нет данных" : `${logisticsDays} дней`}</dd></dl><span aria-hidden="true">=</span>
            <dl><dt>Всего до поступления</dt><dd>{leadTimeDays == null ? "Нет данных" : `${leadTimeDays} дней`}</dd></dl>
          </div>
          <dl className="order-assistant__source-confidence">
            <div><dt>Источник</dt><dd>{sourceLevelLabel(firstLine.lead_time_source_level)}</dd></div>
            <div><dt>Уверенность</dt><dd className={firstLine.lead_time_confidence === "high" || firstLine.lead_time_confidence === "reliable" ? "is-good" : ""}>{confidenceLabel(firstLine.lead_time_confidence || profile.lead_time_confidence)}</dd></div>
            <div><dt>Почему выбран поставщик</dt><dd>{supplierSelectionLabel(firstLine)}</dd></div>
          </dl>
        </section>

        <section className="order-assistant__panel-section">
          <h3>Договор и условия оплаты</h3>
          <dl className="order-assistant__panel-terms">
            <div><dt>Официальный договор</dt><dd>{order.contract_name || "Не заполнено в 1С"}</dd></div>
            <div><dt>Отсрочка из 1С</dt><dd>{profile.credit_days == null ? "Не заполнено в 1С" : `${profile.credit_days} дней`}{profile.credit_limit ? ` · лимит ${money(profile.credit_limit, order.currency)}` : ""}</dd></div>
            <div><dt>Оплата по договору 1С</dt><dd>{paymentTerms}</dd></div>
          </dl>
        </section>

        <section className="order-assistant__panel-section">
          <h3>Брак и качество</h3>
          <div className="order-assistant__quality-grid">
            <div><span>Связь с поставкой</span><strong>{confirmedDefect ? percent(profile.defect_pct) : "Не подтверждена"}</strong><small>{profile.defect_history_units ? `${profile.defect_history_units.toLocaleString("ru-RU")} шт. · ${confidenceLabel(profile.defect_confidence)}` : "Нет подтверждённой базы"}</small></div>
            <div><span>Брак по товару</span><strong>{percent(productDefect)}</strong><small>{productDefectBasis ? `база ${productDefectBasis.toLocaleString("ru-RU")} шт.` : "Нет истории"}</small></div>
          </div>
        </section>

        <p className="order-assistant__updated-at">Обновлено {dateLabel(profile.facts_updated_at || profile.updated_at)}</p>
        {(profile.advantages.length > 0 || profile.internal_note) && <section className="order-assistant__panel-section"><h3>Профиль поставщика</h3>{profile.advantages.length > 0 && <ul className="order-assistant__panel-advantages">{profile.advantages.map((item) => <li key={item}>{item}</li>)}</ul>}{profile.internal_note && <p className="order-assistant__internal-note"><strong>Внутренний комментарий:</strong> {profile.internal_note}</p>}</section>}
      </div>
    </aside>
  );
}

export function ProcurementOrderAssistant({ onOpenOrder }: Props) {
  const [data, setData] = useState<ProcurementOrderAssistant | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [matchingBusyKey, setMatchingBusyKey] = useState("");
  const [filter, setFilter] = useState<QuickFilter>("all");
  const [showRemoved, setShowRemoved] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [supplier, setSupplier] = useState("");
  const [supplierClass, setSupplierClass] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [panelOrderId, setPanelOrderId] = useState<number | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchProcurementOrderAssistant();
      setData(response);
      const readyKeys = response.orders
        .filter(orderReady)
        .flatMap((order) => activeOrderRows(order).map((row) => row.key));
      setSelected(new Set(readyKeys));
      setPanelOrderId((current) => response.orders.some((order) => order.id === current)
        ? current
        : response.orders.find(orderReady)?.id ?? response.orders[0]?.id ?? null);
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const rows = useMemo<AssistantRow[]>(() => (data?.orders.flatMap((order) =>
    order.lines.map((line) => ({ key: `${order.id}:${line.id}`, order, line }))
  ) || []).sort((left, right) => {
    const leftRank = left.line.removed ? 2 : left.line.blockers.length > 0 ? 0 : 1;
    const rightRank = right.line.removed ? 2 : right.line.blockers.length > 0 ? 0 : 1;
    return leftRank - rightRank
      || left.order.id - right.order.id
      || left.line.line_number - right.line.line_number;
  }), [data]);

  const confirmMatching = async (row: AssistantRow) => {
    const recommendation = row.line.display_family_recommendation;
    if (!recommendation?.registry_version_number || !recommendation.registry_inventory_checksum) {
      toast.error("Версия сопоставления не определена. Обновите расчёт.");
      return;
    }
    setMatchingBusyKey(row.key);
    try {
      const result = await confirmProcurementMatchingReview(row.order.id, row.line.id, {
        expected_registry_version_number: recommendation.registry_version_number,
        expected_registry_inventory_checksum: recommendation.registry_inventory_checksum,
      });
      toast.success(result.idempotent ? "Сопоставление уже было проверено" : "Проверка сопоставления сохранена");
      await load();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setMatchingBusyKey("");
    }
  };

  const suppliers = useMemo(() => Array.from(new Set(rows.map((row) => row.order.supplier_name))).sort(), [rows]);
  const classes = useMemo(() => Array.from(new Set(rows.map((row) => row.order.supplier_profile?.qualification_class).filter(Boolean) as string[])).sort(), [rows]);
  const removedRowsCount = useMemo(() => rows.filter((row) => row.line.removed).length, [rows]);
  const visibleRows = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ru");
    return rows.filter((row) => {
      if (row.line.removed && !showRemoved) return false;
      if (!filterMatches(row, filter)) return false;
      if (supplier && row.order.supplier_name !== supplier) return false;
      if (supplierClass && row.order.supplier_profile?.qualification_class !== supplierClass) return false;
      if (!needle) return true;
      return `${row.line.nomenclature_name} ${row.line.nomenclature_code || ""} ${row.order.supplier_name}`
        .toLocaleLowerCase("ru")
        .includes(needle);
    });
  }, [filter, rows, search, showRemoved, supplier, supplierClass]);
  const projectAlertKeys = useMemo(() => {
    const result = new Map<number, string>();
    visibleRows.forEach((row) => {
      if (row.order.blockers.length > 0 && !result.has(row.order.id)) {
        result.set(row.order.id, row.key);
      }
    });
    return result;
  }, [visibleRows]);

  const selectedRows = useMemo(() => rows.filter((row) => selected.has(row.key)), [rows, selected]);
  const hasResettableFilters = filter !== "all" || Boolean(search || supplier || supplierClass || showRemoved);
  const selectedOrders = useMemo(() => (data?.orders || []).filter((order) => {
    const orderRows = activeOrderRows(order);
    return orderReady(order) && orderRows.every((row) => selected.has(row.key));
  }), [data, selected]);
  const partialOrderCount = useMemo(() => new Set(selectedRows
    .map((row) => row.order.id)
    .filter((orderId) => !selectedOrders.some((order) => order.id === orderId))).size, [selectedOrders, selectedRows]);
  const groupedRows = useMemo(() => {
    const groups = new Map<number, AssistantRow[]>();
    selectedRows.forEach((row) => groups.set(row.order.id, [...(groups.get(row.order.id) || []), row]));
    return Array.from(groups.values());
  }, [selectedRows]);
  const panelRows = useMemo(() => panelOrderId == null
    ? []
    : rows.filter((row) => row.order.id === panelOrderId), [panelOrderId, rows]);

  const countFor = (key: QuickFilter) => {
    if (!data) return 0;
    const summary = data.summary;
    return {
      all: rows.length - removedRowsCount,
      ready: summary.ready_lines,
      "supplier-missing": summary.supplier_missing_lines,
      "price-changed": summary.price_changed_lines,
      "low-profitability": summary.low_profitability_lines,
      "high-defect": summary.high_defect_lines,
      "photo-missing": summary.photo_missing_lines,
    }[key];
  };

  const toggleRow = (row: AssistantRow) => setSelected((current) => {
    if (!rowSelectable(row)) return current;
    const next = new Set(current);
    if (next.has(row.key)) next.delete(row.key); else next.add(row.key);
    return next;
  });

  const toggleVisible = () => setSelected((current) => {
    const next = new Set(current);
    const selectableRows = visibleRows.filter(rowSelectable);
    const allVisibleSelected = selectableRows.length > 0 && selectableRows.every((row) => next.has(row.key));
    selectableRows.forEach((row) => allVisibleSelected ? next.delete(row.key) : next.add(row.key));
    return next;
  });

  const selectableVisibleRows = visibleRows.filter(rowSelectable);
  const assemblyHint = busy
    ? "Идёт сборка выбранных проектов."
    : selectedRows.length === 0
      ? "Выберите хотя бы один полностью готовый проект заказа."
      : partialOrderCount > 0
        ? "Для сборки включите все строки каждого выбранного проекта."
        : selectedOrders.length === 0
          ? "Выбранные строки пока нельзя собрать в готовый проект."
          : `Готово к сборке: ${selectedOrders.length} ${projectLabel(selectedOrders.length)}.`;

  const assemble = async () => {
    if (selectedOrders.length === 0) return;
    setBusy(true);
    try {
      const result = await assembleProcurementOrderProjects(selectedOrders);
      if (result.approved) toast.success(`Собрано проектов заказов: ${result.approved}`);
      if (result.blocked || result.stale) {
        toast.error(`Не собрано: ${result.blocked + result.stale}. Обновите подбор и проверьте блокеры.`);
      }
      await load();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return (
    <div aria-busy="true" aria-label="Загрузка помощника заказов" className="order-assistant__skeleton">
      <span />
      <span />
      <span />
    </div>
  );
  if (error) return (
    <div className="order-workspace__state order-workspace__state--error">
      <strong>Не удалось загрузить помощник</strong><span>{error}</span>
      <button className="btn btn--ghost" onClick={() => void load()} type="button">Повторить</button>
    </div>
  );
  if (!data) return null;

  return (
    <main className={`order-assistant ${panelOpen && panelRows.length ? "has-panel" : ""}`}>
      <div className="order-assistant__canvas">
        <section className="order-assistant__heading">
          <div><h2>Помощник заказов</h2><p>Очередь решений перед формированием проектов заказов поставщикам</p></div>
          <span>Обновлено {dateLabel(data.updated_at)}</span>
        </section>
        <section aria-label="Быстрые фильтры" className="order-assistant__quick-filters">
          {QUICK_FILTERS.map((item) => (
            <button className={filter === item.key ? "is-active" : ""} key={item.key} onClick={() => setFilter(item.key)} title={item.hint} type="button"><span>{item.label}</span><strong>{countFor(item.key)}</strong></button>
          ))}
        </section>
        <div className="order-assistant__table-card">
          <div className="order-assistant__toolbar">
            <button className={filtersOpen ? "is-active" : ""} onClick={() => setFiltersOpen((value) => !value)} type="button">Все фильтры</button>
            <span>{countLabel(visibleRows.length, "строка", "строки", "строк")} в текущем фильтре</span>
            <span className="order-assistant__filter-hint">{QUICK_FILTERS.find((item) => item.key === filter)?.hint}</span>
            {removedRowsCount > 0 && (
              <button
                aria-pressed={showRemoved}
                className="order-assistant__removed-toggle"
                onClick={() => setShowRemoved((value) => !value)}
                type="button"
              >
                Исключённые: {removedRowsCount} · {showRemoved ? "Скрыть" : "Показать"}
              </button>
            )}
            {hasResettableFilters && (
              <button className="order-assistant__reset" onClick={() => { setFilter("all"); setSearch(""); setSupplier(""); setSupplierClass(""); setShowRemoved(false); }} type="button">Сбросить</button>
            )}
          </div>
          {filtersOpen && (
            <div className="order-assistant__advanced-filters">
              <label>Поиск<input onChange={(event) => setSearch(event.target.value)} placeholder="Товар, код или поставщик" type="search" value={search} /></label>
              <label>Поставщик<select onChange={(event) => setSupplier(event.target.value)} value={supplier}><option value="">Все поставщики</option>{suppliers.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
              <label>Класс<select onChange={(event) => setSupplierClass(event.target.value)} value={supplierClass}><option value="">Все классы</option>{classes.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
            </div>
          )}
          <div className="order-assistant__table-scroll">
            <table className="order-assistant__table">
              <thead><tr><th><input aria-label="Выбрать все готовые проекты в фильтре" checked={selectableVisibleRows.length > 0 && selectableVisibleRows.every((row) => selected.has(row.key))} disabled={selectableVisibleRows.length === 0} onChange={toggleVisible} type="checkbox" /></th><th>Фото / товар</th><th>Потребность</th><th>Поставщик</th><th>Цена / изменение</th><th title="Доля прибыли в обороте, 180 дней">Рентабельность</th><th>Брак</th><th>Срок</th><th>Решение</th></tr></thead>
              <tbody>
                {visibleRows.map((row) => {
                  const profitability = numeric(row.line.profitability_pct);
                  const supplierDefectConfirmed = row.line.supplier_defect_attribution === "supplier_exact";
                  const supplierDefect = numeric(row.line.supplier_defect_pct);
                  const productDefect = numeric(row.line.product_defect_pct);
                  const defect = numeric(supplierDefectConfirmed ? row.line.supplier_defect_pct : row.line.product_defect_pct);
                  const defectBasis = supplierDefectConfirmed ? row.line.supplier_defect_history_units : row.line.product_defect_history_units;
                  const defectConfidence = supplierDefectConfirmed ? row.line.supplier_defect_confidence : row.line.product_defect_confidence;
                  const batch = batchEvidence(row.line);
                  const hasBatchBlocker = Boolean(batch || row.line.blockers.includes("batch_error_suspected"));
                  const batchShare = hasBatchBlocker
                    ? numeric((batch?.share_pct ?? row.line.payload?.batch_error_share_pct) as string | number | null)
                    : null;
                  const batchReturns = batch?.return_qty ?? row.line.payload?.batch_error_return_qty;
                  const batchMinimumShare = numeric(batch?.minimum_share_pct as string | number | null);
                  const batchMinimumReturns = numeric(batch?.minimum_return_qty as string | number | null);
                  const priceChange = numeric(row.line.price_change_pct);
                  const familyRecommendation = row.line.display_family_recommendation;
                  const isSelected = selected.has(row.key);
                  const selectable = rowSelectable(row);
                  const unavailableReason = rowUnavailableReason(row);
                  const blockerLines = blockingLineNumbers(row.order);
                  const showProjectAlert = projectAlertKeys.get(row.order.id) === row.key;
                  const matchingReview = familyRecommendation?.conflict_codes.some((code) =>
                    code === "accepted_matching_review" || code === "manual_accepted_matching_review"
                  );
                  const remainingFamilyWarnings = familyRecommendation?.conflict_codes.filter((code) =>
                    code !== "accepted_matching_review" && code !== "manual_accepted_matching_review"
                  ) || [];
                  return (
                    <Fragment key={row.key}>
                    {showProjectAlert && (
                      <tr className="order-assistant__project-alert-row">
                        <td colSpan={9}>
                          <section aria-label={`Блокировка проекта №${row.order.id}`} className="order-assistant__project-alert">
                            <div>
                              <strong>{projectBlockerSummary(row.order)}</strong>
                              <span>
                                {reasonLabel(blockerReasonCount(row.order))} · {problemLineLabel(blockerLines.length)}
                              </span>
                            </div>
                            {onOpenOrder && (
                              <button
                                onClick={() => onOpenOrder(row.order.id, firstBlockingLineId(row.order))}
                                type="button"
                              >
                                Разобрать {problemLineLabel(blockerLines.length)}
                              </button>
                            )}
                          </section>
                        </td>
                      </tr>
                    )}
                    <tr className={row.line.blockers.length > 0 ? "is-blocked" : selectable ? "" : "is-unavailable"}>
                      <td><input aria-label={`Выбрать ${row.line.nomenclature_name}`} checked={isSelected} disabled={!selectable} onChange={() => toggleRow(row)} type="checkbox" /></td>
                      <td><div className="order-assistant__product"><ProductPhoto line={row.line} /><div><strong>{row.line.nomenclature_name}</strong><small>{row.line.nomenclature_code || "Код не указан"}</small>{row.line.product_card_url ? <a className="order-assistant__product-card-link" href={row.line.product_card_url} rel="noreferrer" target="_blank">Карточка товара</a> : <small>Карточка не найдена</small>}</div></div></td>
                      <td>
                        <strong>{quantity(row.line.final_quantity)} шт.</strong>
                        <small>к {dateLabel(row.order.order_date)}</small>
                        {familyRecommendation && (
                          <details className="order-assistant__family-recommendation">
                            <summary>Семья · только вручную</summary>
                            <span>{familyRecommendation.family_label || familyRecommendation.family_id || "членство не найдено"}</span>
                            <span>SKU: {quantity(familyRecommendation.baseline_order_qty)} → {quantity(familyRecommendation.allocated_order_qty)} шт.</span>
                            <span>Пул семьи: {quantity(familyRecommendation.family_pool_order_qty)} шт. · сегмент: {qualitySegmentLabel(familyRecommendation.quality_segment)} / {constructionSegmentLabel(familyRecommendation.construction_segment)}</span>
                            <span>Уверенность: {confidenceLabel(familyRecommendation.confidence)} · версия реестра {familyRecommendation.registry_version_number ?? "—"}</span>
                            {familyRecommendation.reason_ru && <span>{familyRecommendation.reason_ru}</span>}
                            {matchingReview && (
                              <span className="is-warning">
                                Не блокирует заказ. Сопоставление принято автоматически; проверьте рамку и качество.
                              </span>
                            )}
                            {matchingReview && (
                              <button
                                disabled={matchingBusyKey === row.key}
                                onClick={() => void confirmMatching(row)}
                                type="button"
                              >
                                {matchingBusyKey === row.key ? "Сохраняем..." : "Сопоставление проверено"}
                              </button>
                            )}
                            {familyRecommendation.matching_review_confirmed && (
                              <span className="is-good">
                                Проверено {familyRecommendation.matching_review_confirmed_by || "закупщиком"}
                                {familyRecommendation.matching_review_confirmed_at
                                  ? ` · ${dateLabel(familyRecommendation.matching_review_confirmed_at)}`
                                  : ""}
                              </span>
                            )}
                            {remainingFamilyWarnings.length > 0 && (
                              <span className="is-warning">
                                Предупреждения: {remainingFamilyWarnings.map(procurementRiskLabel).join(", ")}
                              </span>
                            )}
                          </details>
                        )}
                        {row.line.removed && <small className="is-warning">Потребность исчезла</small>}
                        {row.line.payload?.recommendation_discrepancy?.final_quantity && <small className="is-warning">Новый расчёт: {quantity(row.line.payload.recommendation_discrepancy.final_quantity.recommended)} шт.</small>}
                      </td>
                      <td><button className="order-assistant__link-button" onClick={() => { setPanelOrderId(row.order.id); setPanelOpen(true); }} title={`${row.order.supplier_name || "Нет поставщика"} — открыть карточку строки справа`} type="button">{row.order.supplier_name || "Нет поставщика"}</button><small>{row.order.contract_ref || row.order.contract_code ? "Контракт" : "Без контракта"}</small></td>
                      <td><strong>{money(row.line.purchase_price, row.line.currency)}</strong><small className={priceChange !== null && Math.abs(priceChange) > 10 ? "is-danger" : priceChange !== null && priceChange < 0 ? "is-good" : ""}>{priceHistoryLabel(row.line)}</small>{row.line.payload?.recommendation_discrepancy?.purchase_price && <small className="is-warning">Новая цена: {money(row.line.payload.recommendation_discrepancy.purchase_price.recommended, row.line.currency)}</small>}</td>
                      <td><strong className={profitability !== null && profitability < 20 ? "is-warning" : profitability !== null ? "is-good" : ""}>{profitability !== null ? percent(profitability) : row.line.profitability_status && row.line.profitability_status !== "ready" ? "не рассчитана" : "нет данных"}</strong><small>{profitability === null && row.line.profitability_explanation ? row.line.profitability_explanation : `доля прибыли в обороте, ${row.line.metrics_window_days || 180} дней`}</small></td>
                      <td>
                        {batchShare !== null ? (
                          <>
                            <strong className="is-danger">Возвраты партии: {percent(batchShare)}</strong>
                            <small>{returnLabel(batchReturns)}</small>
                            <small>
                              Порог: {batchMinimumReturns === null ? "—" : returnLabel(batchMinimumReturns)} · {percent(batchMinimumShare)}
                            </small>
                            <small>
                              Подтверждённый брак поставщика: {supplierDefectConfirmed && supplierDefect !== null ? percent(supplierDefect) : "данных нет"}
                            </small>
                          </>
                        ) : (
                          <>
                            <strong className={supplierDefectConfirmed && defect !== null && defect > 10 && (defectBasis || 0) >= 100 ? "is-danger" : defect !== null ? "is-good" : ""}>
                              {defect === null ? "Данных о браке нет" : percent(defect)}
                            </strong>
                            <small>{supplierDefectConfirmed ? "Подтверждённый брак поставщика" : productDefect !== null ? "Брак товара — поставщик не подтверждён" : "Атрибуция поставщика отсутствует"}</small>
                            <small>{defectBasis ? `${defectBasis.toLocaleString("ru-RU")} шт. · ${confidenceLabel(defectConfidence)}` : "Нет истории"}</small>
                          </>
                        )}
                      </td>
                      <td><strong>{row.line.lead_time_days != null ? `${row.line.lead_time_days} дн. всего` : "Нет данных"}</strong><small>сборка: {row.line.supplier_prepare_days ?? "—"} · логистика: {row.line.logistics_days ?? "—"}</small><small>{sourceLevelLabel(row.line.lead_time_source_level)} · {confidenceLabel(row.line.lead_time_confidence)}</small><small>{supplierSelectionLabel(row.line)}</small></td>
                      <td>
                        <div className="order-assistant__decision">
                          {row.order.blockers.length > 0 ? (
                            <span className="order-assistant__project-action-note">
                              {row.line.blockers.length > 0
                                ? "Разбор этой строки доступен выше"
                                : "Проект заблокирован другой строкой"}
                            </span>
                          ) : (
                            <button aria-pressed={isSelected} className={isSelected ? "is-accepted" : ""} disabled={!selectable} onClick={() => toggleRow(row)} type="button">{isSelected ? "Включено" : "Включить"}</button>
                          )}
                          {unavailableReason && row.order.blockers.length === 0 && <small>{unavailableReason}</small>}
                          {row.line.blocker_details?.[0] && (
                            <small className={row.line.blocker_details[0].severity === "technical" ? "is-warning" : "is-danger"}>
                              Причина: {blockerShortLabel(row.line.blocker_details[0].code)}
                            </small>
                          )}
                        </div>
                      </td>
                    </tr>
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
            {visibleRows.length === 0 && <div className="order-assistant__empty">По выбранным фильтрам строк нет.</div>}
          </div>
          <footer className="order-assistant__table-footer"><span>Выбрано: {countLabel(selectedRows.length, "строка", "строки", "строк")}</span><button onClick={() => setSelected(new Set())} type="button">Снять выбор</button></footer>
        </div>

        <section className="order-assistant__selection">
          <div className="order-assistant__selection-heading"><div><h2>Выбрано: {countLabel(selectedRows.length, "строка", "строки", "строк")}</h2><p>Будут сгруппированы в проекты заказов поставщикам</p></div><button aria-label="Снять выбор" onClick={() => setSelected(new Set())} type="button">Снять выбор</button></div>
          <div className="order-assistant__supplier-list">
            {groupedRows.length ? groupedRows.map((group) => <SupplierSummaryCard key={group[0].order.id} onOpen={() => { setPanelOrderId(group[0].order.id); setPanelOpen(true); }} rows={group} />) : <div className="order-assistant__empty">Выберите строки для формирования проектов.</div>}
          </div>
          {partialOrderCount > 0 && <p className="order-assistant__partial-note">Неполных групп: {partialOrderCount}. Чтобы собрать проект, выберите все строки этого заказа.</p>}
        </section>
        <p className="order-assistant__photo-note">В пакет попадают ссылки на исходные фото без сжатия. Миниатюры используются только на экране.</p>
        <button aria-describedby="order-assistant-assembly-hint" className="order-assistant__assemble" disabled={busy || selectedOrders.length === 0 || partialOrderCount > 0} onClick={() => void assemble()} type="button">{busy ? "Собираем проекты..." : `Собрать ${selectedOrders.length} ${projectLabel(selectedOrders.length)}`}</button>
        <p className="order-assistant__assembly-hint" id="order-assistant-assembly-hint">{assemblyHint}</p>
        <p className="order-assistant__onec-note">Проекты не будут отправлены в 1С автоматически. Передача остаётся отдельным действием в разделе «Заказы».</p>
      </div>
      {panelOpen && panelRows.length > 0 && <SupplierPanel key={panelRows[0].order.id} onClose={() => setPanelOpen(false)} onOpenOrder={onOpenOrder} onRefresh={load} rows={panelRows} />}
    </main>
  );
}
