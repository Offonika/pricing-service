import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  assembleProcurementOrderProjects,
  approveProcurementClassification,
  fetchProcurementOrderAssistant,
  rejectProcurementClassification,
  updateProcurementSupplierProfile,
  type ProcurementOrderAssistant,
  type ProcurementOrderFormation,
  type ProcurementOrderFormationLine,
  type ProcurementSupplierProfile,
} from "../api/procurementAssortment";
import "../orderAssistant.css";

interface Props {
  onOpenOrder?: (orderId: number) => void;
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

const QUICK_FILTERS: Array<{ key: QuickFilter; label: string }> = [
  { key: "all", label: "Все" },
  { key: "ready", label: "Можно собрать" },
  { key: "supplier-missing", label: "Без поставщика" },
  { key: "price-changed", label: "Цена изменилась" },
  { key: "low-profitability", label: "Рентабельность ниже нормы" },
  { key: "high-defect", label: "Подтверждённый брак >10%" },
  { key: "photo-missing", label: "Без фото" },
];

function errorText(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail || (error instanceof Error ? error.message : "Операция не выполнена");
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
  if (supplierMissing(row.order)) return "Недоступно: не определён поставщик";
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
  if (row.order.blockers.length || row.line.blockers.length) {
    return "Недоступно: проект содержит блокирующие условия";
  }
  if (!orderReady(row.order)) return "Недоступно: другая строка проекта ещё не готова";
  return "";
}

function projectLabel(count: number) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return "проектов заказов";
  if (last === 1) return "проект заказа";
  if (last >= 2 && last <= 4) return "проекта заказов";
  return "проектов заказов";
}

function priceHistoryLabel(line: ProcurementOrderFormationLine) {
  const change = numeric(line.price_change_pct);
  if (change !== null) {
    const history = line.price_history_count ? ` · ${line.price_history_count} заказ.` : "";
    return `${change > 0 ? "+" : ""}${percent(change)}${history}`;
  }
  const currency = line.price_history_expected_currency || line.currency;
  if (line.price_change_status === "currency_mismatch") {
    const available = line.price_history_available_currencies?.join(", ");
    return available
      ? `Нет истории в ${currency}; есть ${available}`
      : `Нет истории в ${currency}`;
  }
  if (line.price_history_count === 1) return `Только 1 заказ в ${currency}`;
  return `Нет двух заказов в ${currency}`;
}

function filterMatches(row: AssistantRow, filter: QuickFilter) {
  const profitability = numeric(row.line.profitability_pct);
  const defect = numeric(row.line.supplier_defect_pct);
  const priceChange = numeric(row.line.price_change_pct);
  if (filter === "ready") return rowSelectable(row);
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

function ClassificationDecision({ row, onRefresh }: { row: AssistantRow; onRefresh: () => Promise<void> }) {
  const proposal = row.line.latest_classification;
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  if (!proposal || proposal.status !== "proposed") return null;
  const decide = async (decision: "approve" | "reject") => {
    if (decision === "reject" && !reason.trim()) return;
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
    <section className="order-assistant__classification" aria-label="Предложение классификации">
      <strong>{proposal.previous_status || "Не задан"} → {proposal.proposed_status_label}</strong>
      <small>Автор: {proposal.requested_by_name || proposal.requested_by_bitrix_user_id}</small>
      <p>{proposal.reason}</p>
      {(proposal.can_approve || proposal.can_reject) ? (
        <div className="order-assistant__classification-actions">
          <button disabled={busy} onClick={() => void decide("approve")} type="button">Принять</button>
          <button disabled={busy} onClick={() => setRejecting((value) => !value)} type="button">Отклонить</button>
          {rejecting && (
            <label>Причина отклонения
              <textarea onChange={(event) => setReason(event.target.value)} value={reason} />
              <button disabled={busy || !reason.trim()} onClick={() => void decide("reject")} type="button">Подтвердить отклонение</button>
            </label>
          )}
        </div>
      ) : (
        <small>Решение доступно согласующему, который не является автором.</small>
      )}
    </section>
  );
}

function SupplierCard({ rows, onRefresh, defaultExpanded = false }: { rows: AssistantRow[]; onRefresh: () => Promise<void>; defaultExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const order = rows[0].order;
  const profile: ProcurementSupplierProfile = order.supplier_profile || {
    advantages: [],
    data_status: "missing",
  };
  const [profileClass, setProfileClass] = useState(profile.qualification_class || "");
  const [qualificationLabel, setQualificationLabel] = useState(profile.qualification_label || "");
  const [advantages, setAdvantages] = useState(profile.advantages.join("\n"));
  const [internalNote, setInternalNote] = useState(profile.internal_note || "");
  const supplierClass = (profile.qualification_class || "").toUpperCase();
  const classToken = ["A", "B", "C"].includes(supplierClass) ? supplierClass.toLowerCase() : "unknown";
  const lineProfitability = rows
    .map(({ line }) => numeric(line.profitability_pct))
    .filter((value): value is number => value !== null);
  const currentProfitability = lineProfitability.length
    ? lineProfitability.reduce((sum, value) => sum + value, 0) / lineProfitability.length
    : null;
  const total = rows.reduce((sum, { line }) => sum + (numeric(line.amount) || 0), 0);
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
    <article className="order-assistant__supplier-card">
      <div className="order-assistant__supplier-heading">
        <div>
          <h3>{order.supplier_name || "Без поставщика"}</h3>
          <p>{rows.length} поз. · {money(total, order.currency)}</p>
        </div>
        <div className="order-assistant__supplier-actions">
          {supplierClass ? (
            <span className={`supplier-class supplier-class--${classToken}`}>Класс {supplierClass}</span>
          ) : (
            <span className="supplier-class supplier-class--unknown">Класс не задан</span>
          )}
          <button aria-expanded={expanded} onClick={() => setExpanded((value) => !value)} type="button">
            {expanded ? "Свернуть" : "Подробнее"}
          </button>
          {profile.can_edit && order.supplier_ref && (
            <button onClick={() => setEditing((value) => !value)} type="button">{editing ? "Отмена" : "Изменить профиль"}</button>
          )}
        </div>
      </div>
      {profile.qualification_label && (
        <p className="order-assistant__qualification">{profile.qualification_label}</p>
      )}
      {profile.class_description && (
        <p className="order-assistant__qualification">{profile.class_description}</p>
      )}
      {expanded ? (
        <>
          {editing && (
            <div className="order-assistant__profile-form">
              <label>Класс<select onChange={(event) => setProfileClass(event.target.value)} value={profileClass}><option value="">Не назначен</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select></label>
              <label>Расшифровка<input onChange={(event) => setQualificationLabel(event.target.value)} value={qualificationLabel} /></label>
              <label>Преимущества<textarea onChange={(event) => setAdvantages(event.target.value)} placeholder="По одному на строку" value={advantages} /></label>
              <label>Внутренний комментарий<textarea onChange={(event) => setInternalNote(event.target.value)} value={internalNote} /></label>
              <button disabled={saving} onClick={() => void saveProfile()} type="button">{saving ? "Сохраняем..." : "Сохранить профиль"}</button>
            </div>
          )}
          {profile.data_status === "missing" ? (
            <p className="order-assistant__history-note">История по поставщику пока не заполнена.</p>
          ) : profile.data_status === "partial" ? (
            <p className="order-assistant__history-note">История заполнена частично — пустые показатели не оцениваются.</p>
          ) : null}
          <dl className="order-assistant__supplier-metrics">
            <div>
              <dt>Рентабельность</dt>
              <dd className={profile.profitability_pct == null && currentProfitability == null ? "is-missing" : ""}>{percent(profile.profitability_pct ?? currentProfitability)}</dd>
              <dd className="order-assistant__metric-note">{profile.profitability_pct != null ? "по истории" : "по подбору"}</dd>
            </div>
            <div>
              <dt>Брак поставщика</dt>
              <dd className={profile.defect_attribution !== "supplier_exact" ? "is-missing" : (numeric(profile.defect_pct) || 0) > 10 ? "is-danger" : ""}>{profile.defect_attribution === "supplier_exact" ? percent(profile.defect_pct) : "Связь с поставкой не подтверждена"}</dd>
              <dd className="order-assistant__metric-note">{profile.defect_history_units ? `${profile.defect_history_units.toLocaleString("ru-RU")} шт. · ${profile.defect_confidence || "без оценки"}` : "нет подтверждённой базы"}</dd>
            </div>
            <div>
              <dt>История заказов</dt>
              <dd className={profile.history_order_count == null ? "is-missing" : ""}>{profile.history_order_count == null ? "Нет данных" : `${profile.history_order_count} заказов`}</dd>
              <dd className="order-assistant__metric-note">ценовых наблюдений: {profile.price_history_count ?? "нет"}</dd>
            </div>
          </dl>
          <dl className="order-assistant__terms">
            <div><dt>Оплата по договору 1С</dt><dd>{profile.terms_status === "missing" ? "Не заполнено в 1С" : profile.payment_terms || "Не заполнено в 1С"}</dd></div>
            <div><dt>Отсрочка</dt><dd>{profile.credit_days == null ? "Не заполнено в 1С" : `${profile.credit_days} дней`}{profile.credit_limit ? ` · лимит ${money(profile.credit_limit, order.currency)}` : ""}</dd></div>
            <div><dt>Сборка у поставщика</dt><dd>{profile.supplier_prepare_days == null ? "Нет данных" : `${profile.supplier_prepare_days} дн.`}</dd></div>
            <div><dt>Логистика</dt><dd>{profile.logistics_days == null ? "Нет данных" : `${profile.logistics_days} дн.`}</dd></div>
            <div><dt>Всего до поступления</dt><dd>{profile.lead_time_days == null ? "Нет данных" : `${profile.lead_time_days} дн. · ${profile.lead_time_confidence || "уверенность не оценена"}`}</dd></div>
          </dl>
          <div className="order-assistant__advantages">
            <strong>Преимущества</strong>
            {profile.advantages.length ? (
              <ul>{profile.advantages.map((item) => <li key={item}>{item}</li>)}</ul>
            ) : (
              <span>Не заполнены в карточке поставщика</span>
            )}
          </div>
          {profile.internal_note && <p className="order-assistant__internal-note"><strong>Внутренний комментарий:</strong> {profile.internal_note}</p>}
          <div className="order-assistant__supplier-footer">
            <small>Факты обновлены {dateLabel(profile.facts_updated_at || profile.updated_at)}{profile.manual_updated_by_name ? ` · профиль: ${profile.manual_updated_by_name}` : ""}</small>
            <details>
              <summary>Пакет поставщику</summary>
              <div>
                <button onClick={() => downloadSupplierPackage(rows, "list")} type="button">Список + фото</button>
                <button onClick={() => downloadSupplierPackage(rows, "photos")} type="button">Фото отдельно</button>
              </div>
            </details>
          </div>
        </>
      ) : (
        <dl className="order-assistant__supplier-compact">
          <div><dt>Брак поставщика</dt><dd>{profile.defect_attribution === "supplier_exact" ? percent(profile.defect_pct) : "Не подтверждён"}</dd></div>
          <div><dt>Отсрочка</dt><dd>{profile.credit_days == null ? "Не заполнено в 1С" : `${profile.credit_days} дней`}</dd></div>
          <div><dt>Общий срок</dt><dd>{profile.lead_time_days == null ? "Нет данных" : `${profile.lead_time_days} дн.`}</dd></div>
        </dl>
      )}
    </article>
  );
}

export function ProcurementOrderAssistant({ onOpenOrder }: Props) {
  const [data, setData] = useState<ProcurementOrderAssistant | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<QuickFilter>("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [supplier, setSupplier] = useState("");
  const [supplierClass, setSupplierClass] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

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
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const rows = useMemo<AssistantRow[]>(() => (data?.orders.flatMap((order) =>
    order.lines
      .filter((line) => !line.removed)
      .map((line) => ({ key: `${order.id}:${line.id}`, order, line }))
  ) || []).sort((left, right) =>
    left.line.line_number - right.line.line_number || left.order.id - right.order.id
  ), [data]);

  const suppliers = useMemo(() => Array.from(new Set(rows.map((row) => row.order.supplier_name))).sort(), [rows]);
  const classes = useMemo(() => Array.from(new Set(rows.map((row) => row.order.supplier_profile?.qualification_class).filter(Boolean) as string[])).sort(), [rows]);
  const visibleRows = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ru");
    return rows.filter((row) => {
      if (!filterMatches(row, filter)) return false;
      if (supplier && row.order.supplier_name !== supplier) return false;
      if (supplierClass && row.order.supplier_profile?.qualification_class !== supplierClass) return false;
      if (!needle) return true;
      return `${row.line.nomenclature_name} ${row.line.nomenclature_code || ""} ${row.order.supplier_name}`
        .toLocaleLowerCase("ru")
        .includes(needle);
    });
  }, [filter, rows, search, supplier, supplierClass]);

  const selectedRows = useMemo(() => rows.filter((row) => selected.has(row.key)), [rows, selected]);
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

  const countFor = (key: QuickFilter) => {
    if (!data) return 0;
    const summary = data.summary;
    return {
      all: summary.lines,
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

  if (loading) return <div className="order-workspace__state">Загрузка помощника заказов...</div>;
  if (error) return (
    <div className="order-workspace__state order-workspace__state--error">
      <strong>Не удалось загрузить помощник</strong><span>{error}</span>
      <button className="btn btn--ghost" onClick={() => void load()} type="button">Повторить</button>
    </div>
  );
  if (!data) return null;

  return (
    <main className="order-assistant">
      <section className="order-assistant__heading">
        <div><h2>Помощник заказов</h2><p>Очередь решений перед формированием проектов заказов поставщикам</p></div>
        <span>Обновлено {dateLabel(data.updated_at)}</span>
      </section>
      <section aria-label="Быстрые фильтры" className="order-assistant__quick-filters">
        {QUICK_FILTERS.map((item) => (
          <button className={filter === item.key ? "is-active" : ""} key={item.key} onClick={() => setFilter(item.key)} type="button">
            <span>{item.label}</span><strong>{countFor(item.key)}</strong>
          </button>
        ))}
      </section>
      <section className="order-assistant__layout">
        <div className="order-assistant__table-card">
          <div className="order-assistant__toolbar">
            <button className={filtersOpen ? "is-active" : ""} onClick={() => setFiltersOpen((value) => !value)} type="button">Все фильтры</button>
            <span>{visibleRows.length} строк в текущем фильтре</span>
            <button className="order-assistant__reset" onClick={() => { setFilter("all"); setSearch(""); setSupplier(""); setSupplierClass(""); }} type="button">Сбросить</button>
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
              <thead><tr>
                <th><input aria-label="Выбрать все готовые проекты в фильтре" checked={selectableVisibleRows.length > 0 && selectableVisibleRows.every((row) => selected.has(row.key))} disabled={selectableVisibleRows.length === 0} onChange={toggleVisible} type="checkbox" /></th>
                <th>Фото / товар</th><th>Потребность</th><th>Поставщик</th><th>Цена / изменение</th><th>Рентабельность</th><th>Брак</th><th>Срок</th><th>Решение</th>
              </tr></thead>
              <tbody>
                {visibleRows.map((row) => {
                  const profitability = numeric(row.line.profitability_pct);
                  const supplierDefectConfirmed = row.line.supplier_defect_attribution === "supplier_exact";
                  const defect = numeric(supplierDefectConfirmed ? row.line.supplier_defect_pct : row.line.product_defect_pct);
                  const defectBasis = supplierDefectConfirmed ? row.line.supplier_defect_history_units : row.line.product_defect_history_units;
                  const defectConfidence = supplierDefectConfirmed ? row.line.supplier_defect_confidence : row.line.product_defect_confidence;
                  const priceChange = numeric(row.line.price_change_pct);
                  const isSelected = selected.has(row.key);
                  const selectable = rowSelectable(row);
                  const unavailableReason = rowUnavailableReason(row);
                  return (
                    <tr className={selectable ? "" : "is-unavailable"} key={row.key}>
                      <td><input aria-label={`Выбрать ${row.line.nomenclature_name}`} checked={isSelected} disabled={!selectable} onChange={() => toggleRow(row)} type="checkbox" /></td>
                      <td><div className="order-assistant__product"><ProductPhoto line={row.line} /><div><strong>{row.line.nomenclature_name}</strong><small>{row.line.nomenclature_code || "Код не указан"}</small>{row.line.product_card_url ? <a className="order-assistant__product-card-link" href={row.line.product_card_url} rel="noreferrer" target="_blank">Карточка товара</a> : <small>Карточка не найдена</small>}</div></div></td>
                      <td><strong>{quantity(row.line.final_quantity)} шт.</strong><small>к {dateLabel(row.order.order_date)}</small></td>
                      <td><button className="order-assistant__link-button" onClick={() => onOpenOrder?.(row.order.id)} type="button">{row.order.supplier_name || "Нет поставщика"}</button><small>{row.order.contract_ref || row.order.contract_code ? "Контракт" : "Без контракта"}</small></td>
                      <td><strong>{money(row.line.purchase_price, row.line.currency)}</strong><small className={priceChange !== null && Math.abs(priceChange) > 10 ? "is-danger" : priceChange !== null && priceChange < 0 ? "is-good" : ""}>{priceHistoryLabel(row.line)}</small></td>
                      <td><strong className={profitability !== null && profitability < 20 ? "is-warning" : profitability !== null ? "is-good" : ""}>{percent(profitability)}</strong><small>{row.line.profitability_explanation || (row.line.metrics_window_days ? `${row.line.metrics_window_days} дней · 1С` : "Нет истории")}</small></td>
                      <td><strong className={supplierDefectConfirmed && defect !== null && defect > 10 && (defectBasis || 0) >= 100 ? "is-danger" : defect !== null ? "is-good" : ""}>{percent(defect)}</strong><small>{supplierDefectConfirmed ? "Брак поставщика подтверждён" : "Брак по товару — поставщик не подтверждён"}</small><small>{defectBasis ? `${defectBasis.toLocaleString("ru-RU")} шт. · ${defectConfidence || "без оценки"}` : "Нет истории"}</small></td>
                      <td><strong>{row.line.lead_time_days != null ? `${row.line.lead_time_days} дн. всего` : "Нет данных"}</strong><small>сборка: {row.line.supplier_prepare_days ?? "—"} · логистика: {row.line.logistics_days ?? "—"}</small><small>{row.line.lead_time_source_level || "источник не определён"} · {row.line.lead_time_confidence || "без оценки"}</small></td>
                      <td><div className="order-assistant__decision"><button aria-pressed={isSelected} className={isSelected ? "is-accepted" : ""} disabled={!selectable} onClick={() => toggleRow(row)} type="button">{isSelected ? "Включено" : "Включить"}</button>{unavailableReason && <small>{unavailableReason}</small>}<ClassificationDecision onRefresh={load} row={row} /></div></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {visibleRows.length === 0 && <div className="order-assistant__empty">По выбранным фильтрам строк нет.</div>}
          </div>
          <footer className="order-assistant__table-footer"><span>Выбрано {selectedRows.length} строк</span><button onClick={() => setSelected(new Set())} type="button">Снять выбор</button></footer>
        </div>
        <aside className="order-assistant__selection">
          <div className="order-assistant__selection-heading"><div><h2>Выбрано {selectedRows.length} строк</h2><p>Будут сгруппированы в проекты заказов поставщикам</p></div><button aria-label="Снять выбор" onClick={() => setSelected(new Set())} type="button">Закрыть</button></div>
          <div className="order-assistant__supplier-list">
            {groupedRows.length ? groupedRows.map((group, index) => <SupplierCard defaultExpanded={index === 0} key={group[0].order.id} onRefresh={load} rows={group} />) : <div className="order-assistant__empty">Выберите строки для формирования проектов.</div>}
          </div>
          {partialOrderCount > 0 && <p className="order-assistant__partial-note">Неполных групп: {partialOrderCount}. Чтобы собрать проект, выберите все строки этого заказа.</p>}
          <p className="order-assistant__photo-note">В пакет попадают ссылки на исходные фото без сжатия. Миниатюры используются только на экране.</p>
          <button aria-describedby="order-assistant-assembly-hint" className="order-assistant__assemble" disabled={busy || selectedOrders.length === 0 || partialOrderCount > 0} onClick={() => void assemble()} type="button">{busy ? "Собираем проекты..." : `Собрать ${selectedOrders.length} ${projectLabel(selectedOrders.length)}`}</button>
          <p className="order-assistant__assembly-hint" id="order-assistant-assembly-hint">{assemblyHint}</p>
          <p className="order-assistant__onec-note">Проекты не будут отправлены в 1С автоматически. Передача остаётся отдельным действием в разделе «Заказы».</p>
        </aside>
      </section>
    </main>
  );
}
