import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  assembleProcurementOrderProjects,
  fetchProcurementOrderAssistant,
  type ProcurementOrderAssistant,
  type ProcurementOrderFormation,
  type ProcurementOrderFormationLine,
} from "../api/procurementAssortment";

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
  { key: "high-defect", label: "Брак выше 2%" },
  { key: "photo-missing", label: "Без фото" },
];

function errorText(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail || (error instanceof Error ? error.message : "Операция не выполнена");
}

function numeric(value?: string | number | null) {
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
      row.line.photo_original_url
  );
}

function filterMatches(row: AssistantRow, filter: QuickFilter) {
  const profitability = numeric(row.line.profitability_pct);
  const defect = numeric(row.line.supplier_defect_pct);
  const priceChange = numeric(row.line.price_change_pct);
  if (filter === "ready") return rowReady(row);
  if (filter === "supplier-missing") return supplierMissing(row.order);
  if (filter === "price-changed") return priceChange !== null && priceChange !== 0;
  if (filter === "low-profitability") return profitability !== null && profitability < 20;
  if (filter === "high-defect") return defect !== null && defect > 2;
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
    const header = ["Имя файла", "Код товара", "Оригинал фото"].map(csvCell).join(";");
    const body = rows.map(({ line }, index) => [
      `${safeFilePart(line.nomenclature_code || `item-${index + 1}`)}.original`,
      line.nomenclature_code || "",
      line.photo_original_url || "",
    ].map(csvCell).join(";"));
    saveTextFile(`${supplier}-photos.csv`, [header, ...body].join("\r\n"), "text/csv;charset=utf-8");
    return;
  }
  const header = [
    "Поставщик",
    "Код товара",
    "Товар",
    "Количество",
    "Цена",
    "Валюта",
    "Оригинал фото",
  ].map(csvCell).join(";");
  const body = rows.map(({ order, line }) => [
    order.supplier_name,
    line.nomenclature_code || "",
    line.nomenclature_name,
    line.final_quantity,
    line.purchase_price,
    line.currency,
    line.photo_original_url || "",
  ].map(csvCell).join(";"));
  saveTextFile(`${supplier}-order-with-photos.csv`, [header, ...body].join("\r\n"), "text/csv;charset=utf-8");
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

function SupplierCard({ rows, defaultExpanded = false }: { rows: AssistantRow[]; defaultExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const order = rows[0].order;
  const profile = order.supplier_profile;
  const supplierClass = (profile.qualification_class || "").toUpperCase();
  const classToken = ["A", "B", "C"].includes(supplierClass) ? supplierClass.toLowerCase() : "unknown";
  const lineProfitability = rows
    .map(({ line }) => numeric(line.profitability_pct))
    .filter((value): value is number => value !== null);
  const currentProfitability = lineProfitability.length
    ? lineProfitability.reduce((sum, value) => sum + value, 0) / lineProfitability.length
    : null;
  const total = rows.reduce((sum, { line }) => sum + (numeric(line.amount) || 0), 0);
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
        </div>
      </div>
      {profile.qualification_label && (
        <p className="order-assistant__qualification">{profile.qualification_label}</p>
      )}

      {expanded ? (
        <>
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
              <dt>Брак</dt>
              <dd className={profile.defect_pct == null ? "is-missing" : (numeric(profile.defect_pct) || 0) > 2 ? "is-danger" : ""}>{percent(profile.defect_pct)}</dd>
              <dd className="order-assistant__metric-note">{profile.defect_history_units ? `${profile.defect_history_units.toLocaleString("ru-RU")} шт. в истории` : "нет базы истории"}</dd>
            </div>
            <div>
              <dt>В срок</dt>
              <dd className={profile.on_time_pct == null ? "is-missing" : ""}>{percent(profile.on_time_pct)}</dd>
              <dd className="order-assistant__metric-note">{profile.history_order_count ? `${profile.history_order_count} заказов` : "нет базы истории"}</dd>
            </div>
          </dl>

          <dl className="order-assistant__terms">
            <div><dt>Оплата</dt><dd>{profile.payment_terms || "Нет данных"}</dd></div>
            <div><dt>Отсрочка / кредит</dt><dd>{profile.credit_days ? `${profile.credit_days} дней` : "Нет данных"}{profile.credit_limit ? ` · лимит ${money(profile.credit_limit, order.currency)}` : ""}</dd></div>
          </dl>

          <div className="order-assistant__advantages">
            <strong>Преимущества</strong>
            {profile.advantages.length ? (
              <ul>{profile.advantages.map((item) => <li key={item}>{item}</li>)}</ul>
            ) : (
              <span>Не заполнены в карточке поставщика</span>
            )}
          </div>

          <div className="order-assistant__supplier-footer">
            <small>Обновлено {dateLabel(profile.updated_at)}</small>
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
          <div><dt>Брак</dt><dd>{percent(profile.defect_pct)}</dd></div>
          <div><dt>Отсрочка</dt><dd>{profile.credit_days ? `${profile.credit_days} дней` : "Нет данных"}</dd></div>
          <div><dt>В срок</dt><dd>{percent(profile.on_time_pct)}</dd></div>
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
      const readyKeys = response.orders.flatMap((order) =>
        order.lines
          .filter((line) => !line.removed)
          .map((line) => ({ key: `${order.id}:${line.id}`, order, line }))
          .filter(rowReady)
          .map((row) => row.key)
      );
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
  const classes = useMemo(() => Array.from(new Set(rows.map((row) => row.order.supplier_profile.qualification_class).filter(Boolean) as string[])).sort(), [rows]);
  const visibleRows = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ru");
    return rows.filter((row) => {
      if (!filterMatches(row, filter)) return false;
      if (supplier && row.order.supplier_name !== supplier) return false;
      if (supplierClass && row.order.supplier_profile.qualification_class !== supplierClass) return false;
      if (!needle) return true;
      return `${row.line.nomenclature_name} ${row.line.nomenclature_code || ""} ${row.order.supplier_name}`
        .toLocaleLowerCase("ru")
        .includes(needle);
    });
  }, [filter, rows, search, supplier, supplierClass]);

  const selectedRows = useMemo(() => rows.filter((row) => selected.has(row.key)), [rows, selected]);
  const selectedOrders = useMemo(() => (data?.orders || []).filter((order) => {
    const orderRows = rows.filter((row) => row.order.id === order.id);
    return order.status !== "approved" && orderRows.length > 0 && orderRows.every((row) => selected.has(row.key) && rowReady(row));
  }), [data, rows, selected]);
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

  const toggleRow = (key: string) => setSelected((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const toggleVisible = () => setSelected((current) => {
    const next = new Set(current);
    const allVisibleSelected = visibleRows.length > 0 && visibleRows.every((row) => next.has(row.key));
    visibleRows.forEach((row) => allVisibleSelected ? next.delete(row.key) : next.add(row.key));
    return next;
  });

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
                <th><input aria-label="Выбрать все строки в фильтре" checked={visibleRows.length > 0 && visibleRows.every((row) => selected.has(row.key))} onChange={toggleVisible} type="checkbox" /></th>
                <th>Фото / товар</th><th>Потребность</th><th>Поставщик</th><th>Цена / изменение</th><th>Рентабельность</th><th>Брак</th><th>Срок</th><th>Решение</th>
              </tr></thead>
              <tbody>
                {visibleRows.map((row) => {
                  const profitability = numeric(row.line.profitability_pct);
                  const defect = numeric(row.line.supplier_defect_pct);
                  const priceChange = numeric(row.line.price_change_pct);
                  const isSelected = selected.has(row.key);
                  return (
                    <tr className={!row.line.photo_original_url ? "is-photo-missing" : row.line.blockers.length || row.order.blockers.length ? "is-blocked" : ""} key={row.key}>
                      <td><input aria-label={`Выбрать ${row.line.nomenclature_name}`} checked={isSelected} onChange={() => toggleRow(row.key)} type="checkbox" /></td>
                      <td><div className="order-assistant__product"><ProductPhoto line={row.line} /><div><strong>{row.line.nomenclature_name}</strong><small>{row.line.nomenclature_code || "Код не указан"}</small></div></div></td>
                      <td><strong>{quantity(row.line.final_quantity)} шт.</strong><small>к {dateLabel(row.order.order_date)}</small></td>
                      <td><button className="order-assistant__link-button" onClick={() => onOpenOrder?.(row.order.id)} type="button">{row.order.supplier_name || "Нет поставщика"}</button><small>{row.order.contract_ref || row.order.contract_code ? "Контракт" : "Без контракта"}</small></td>
                      <td><strong>{money(row.line.purchase_price, row.line.currency)}</strong><small className={priceChange !== null && priceChange > 0 ? "is-danger" : priceChange !== null && priceChange < 0 ? "is-good" : ""}>{priceChange === null ? "Нет истории" : `${priceChange > 0 ? "+" : ""}${percent(priceChange)}`}</small></td>
                      <td><strong className={profitability !== null && profitability < 20 ? "is-warning" : profitability !== null ? "is-good" : ""}>{percent(profitability)}</strong></td>
                      <td><strong className={defect !== null && defect > 2 ? "is-danger" : defect !== null ? "is-good" : ""}>{percent(defect)}</strong><small>{row.line.supplier_defect_history_units ? `${row.line.supplier_defect_history_units.toLocaleString("ru-RU")} шт. в истории` : "Нет истории"}</small></td>
                      <td><strong>{row.line.delivery_days != null ? `${row.line.delivery_days} дн.` : "Нет данных"}</strong></td>
                      <td><div className="order-assistant__decision"><button className={isSelected ? "is-accepted" : ""} onClick={() => { if (!isSelected) toggleRow(row.key); }} type="button">{isSelected ? "Принято" : "Принять"}</button><button onClick={() => { if (isSelected) toggleRow(row.key); }} type="button">Убрать</button></div></td>
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
            {groupedRows.length ? groupedRows.map((group, index) => <SupplierCard defaultExpanded={index === 0} key={group[0].order.id} rows={group} />) : <div className="order-assistant__empty">Выберите строки для формирования проектов.</div>}
          </div>
          {partialOrderCount > 0 && <p className="order-assistant__partial-note">Неполных групп: {partialOrderCount}. Чтобы собрать проект, выберите все строки этого заказа.</p>}
          <p className="order-assistant__photo-note">В пакет попадают ссылки на исходные фото без сжатия. Миниатюры используются только на экране.</p>
          <button className="order-assistant__assemble" disabled={busy || selectedOrders.length === 0} onClick={() => void assemble()} type="button">{busy ? "Собираем проекты..." : `Собрать ${selectedOrders.length} ${selectedOrders.length === 1 ? "проект заказа" : "проекта заказов"}`}</button>
          <p className="order-assistant__onec-note">Проекты не будут отправлены в 1С автоматически. Передача остаётся отдельным действием в разделе «Заказы».</p>
        </aside>
      </section>
    </main>
  );
}
