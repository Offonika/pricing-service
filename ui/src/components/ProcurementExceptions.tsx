import "./ProcurementExceptions.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { fetchProcurementOrder, type ProcurementOrderFormation } from "../api/procurementAssortment";
import { procurementErrorText } from "../utils/procurementErrorMessages";

interface ExceptionItem {
  id: number; order_id: number; line_id: number | null; title: string; reason_code: string;
  status: string; version: number; facts_hash: string; overdue: boolean;
  response_due_at: string; first_seen_at: string; next_action: string | null;
  next_action_due_at: string | null; assigned_user_id: string | null;
  facts: { all_open_quantity?: string; dated_only_quantity?: string; days_remaining?: string;
    required_days?: string; expected_at?: string; open_quantity?: string;
    schedule?: { order_ref: string; quantity: string; expected_at: string | null; category: string }[] };
}
interface Queue { total: number; items: ExceptionItem[]; overdue_count: number }
interface Summary {
  open_orders: number; without_eta: number; past_eta: number;
  unconfirmed_incoming_quantity: string; unknown_incoming_order_count: number;
  synchronization_errors: number; exceptions_open: number; exceptions_overdue: number;
  stockout_risks: number; unknown_freshness_count: number; last_onec_sync_at: string | null;
  oldest_onec_sync_at?: string | null; stale_receipt_sources?: number;
  recommendation_change_reasons?: Record<string, number>;
  recommendation_decisions: { denominator: number; confirmed: number; changed: number; unreviewed: number };
}
const statusNames: Record<string, string> = { new: "Новое", in_progress: "В работе", waiting: "Ожидание ответа", resolved: "Решено" };
const categoryNames: Record<string, string> = { dated: "В пределах горизонта", undated: "Без даты", overdue: "Срок прошёл", later: "За пределами горизонта" };
const manualResolution = new Set(["supply_confirmation_required", "stockout_risk", "aged_order", "price_history"]);
const dateTime = (value: string | null) => value ? new Date(value).toLocaleString("ru-RU", { timeZone: "Europe/Moscow" }) : "Неизвестно";

export function ProcurementControlOverview({ onOpen }: { onOpen: () => void }) {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void api.get<Summary>("/procurement-order-formation/control-summary", { signal: controller.signal })
      .then(({ data: value }) => setData(value)).catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(procurementErrorText(reason));
      });
    return () => controller.abort();
  }, []);
  if (error) return <p role="alert">Контроль исполнения: {error}</p>;
  if (!data) return <p role="status">Загрузка контроля исполнения…</p>;
  const decisions = data.recommendation_decisions;
  return <section aria-label="Контроль исполнения закупок" className="order-workspace__content">
    <h2>Контроль исполнения</h2>
    <div className="dashboard-overview">
      <button type="button" onClick={onOpen}><span>Без срока поступления</span><strong>{data.without_eta} / {data.open_orders}</strong></button>
      <button type="button" onClick={onOpen}><span>Срок поступления прошёл</span><strong>{data.past_eta}</strong></button>
      <button type="button" onClick={onOpen}><span>Открытых исключений / просрочено</span><strong>{data.exceptions_open} / {data.exceptions_overdue}</strong></button>
      <button type="button" onClick={onOpen}><span>Риски дефицита</span><strong>{data.stockout_risks}</strong></button>
    </div>
    <p>Неподтверждённые сроки: {data.unconfirmed_incoming_quantity} шт.; неизвестное количество — у {data.unknown_incoming_order_count} заказов.</p>
    <p>Решения по {decisions.denominator} активным рекомендациям: подтверждено {decisions.confirmed}, изменено {decisions.changed}, без решения {decisions.unreviewed}.</p>
    {Object.entries(data.recommendation_change_reasons || {}).map(([reason, count]) => <p key={reason}>Изменение количества: {reason} — {count}.</p>)}
    <p>Ошибки синхронизации: {data.synchronization_errors}. Последнее чтение 1С: {dateTime(data.last_onec_sync_at)} МСК. Свежесть неизвестна у {data.unknown_freshness_count} заказов.</p>
    <p>Самое давнее чтение открытого заказа: {dateTime(data.oldest_onec_sync_at ?? null)} МСК. Устаревшие доказательства приёмки: {data.stale_receipt_sources ?? "Неизвестно"}.</p>
  </section>;
}

export function ProcurementExceptions({ onOpenOrder }: { onOpenOrder: (id: number) => void }) {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [offset, setOffset] = useState(0);
  const [overdue, setOverdue] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ExceptionItem | null>(null);
  const [order, setOrder] = useState<ProcurementOrderFormation | null>(null);
  const [status, setStatus] = useState("in_progress");
  const [action, setAction] = useState("");
  const [due, setDue] = useState("");
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [quantity, setQuantity] = useState("");
  const [busy, setBusy] = useState(false);
  const selectionRequest = useRef(0);
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selected) panel.current?.focus();
  }, [selected]);
  const load = useCallback(async () => {
    setError("");
    try {
      const { data } = await api.get<Queue>("/procurement-order-formation/exceptions", { params: { offset, limit: 50, overdue_only: overdue, status: showResolved ? "all" : "open" } });
      setQueue(data);
    } catch (reason: unknown) { setError(procurementErrorText(reason)); }
  }, [offset, overdue, showResolved]);
  useEffect(() => { void load(); }, [load]);
  const open = async (item: ExceptionItem) => {
    const request = ++selectionRequest.current;
    setSelected(item); setOrder(null); setStatus("in_progress"); setReason(""); setEvidence("");
    setAction(item.next_action || ""); setDue(""); setQuantity(""); setError("");
    try {
      const value = await fetchProcurementOrder(item.order_id);
      if (request !== selectionRequest.current) return;
      setOrder(value);
      setQuantity(String(value.lines.find((line) => line.id === item.line_id)?.final_quantity ?? ""));
    } catch (reason: unknown) {
      if (request === selectionRequest.current) setError(procurementErrorText(reason));
    }
  };
  const save = async () => {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const line = order?.lines.find((line) => line.id === selected.line_id);
      await api.post(`/procurement-order-formation/exceptions/${selected.id}/decision`, {
        expected_version: selected.version, facts_hash: selected.facts_hash, status,
        next_action: action, next_action_due_at: due ? new Date(`${due}:00+03:00`).toISOString() : null,
        reason, evidence, expected_order_version: order?.version,
        expected_line_version: line?.version, final_quantity: quantity || null,
      });
      setSelected(null); await load();
    } catch (reason: unknown) { setError(procurementErrorText(reason)); }
    finally { setBusy(false); }
  };
  return <main className="order-workspace__content procurement-exceptions">
    <h2>Исключения закупки</h2>
    <p>Взять в работу и назначить следующее действие — до 18:00 следующего рабочего дня по Москве. Просрочки контролирует Омар.</p>
    <div className="order-workspace__filters">
      <label><input type="checkbox" checked={overdue} onChange={(event) => { setOverdue(event.target.checked); setOffset(0); }} /> Только просроченные</label>
      <label><input type="checkbox" checked={showResolved} onChange={(event) => { setShowResolved(event.target.checked); setOffset(0); }} /> Включая решённые</label>
      <button type="button" className="btn" onClick={() => void load()}>Обновить</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {!queue ? <p role="status">Загрузка очереди…</p> : <>
      <p>Найдено {queue.total}; просрочено {queue.overdue_count}.</p>
      {queue.items.length === 0 && <p>Исключений по выбранному фильтру нет.</p>}
      {queue.items.map((item) => <article key={item.id} className="order-workspace__section-heading">
        <div><h3>{item.title}</h3><p>{statusNames[item.status]}{item.overdue ? " · Просрочено" : ""}</p>
          <p>Обнаружено: {dateTime(item.first_seen_at)}. Срок реакции: {dateTime(item.response_due_at)} МСК.</p>
          {item.next_action && <p>Следующее действие: {item.next_action}; до {dateTime(item.next_action_due_at)} МСК.</p>}
          {item.facts.all_open_quantity !== undefined && <p>С учётом всех поставок: {item.facts.all_open_quantity} шт. Только датированные: {item.facts.dated_only_quantity} шт.</p>}
          {item.facts.days_remaining !== undefined && <p>Остатка хватит на {item.facts.days_remaining} дней; требуется {item.facts.required_days} дней.</p>}
          <button type="button" className="btn btn--small" onClick={() => onOpenOrder(item.order_id)}>Открыть заказ</button>{" "}
          {item.status !== "resolved" && <button type="button" className="btn btn--small" onClick={() => void open(item)}>Обработать</button>}
        </div>
      </article>)}
      <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Назад</button>{" "}
      <button type="button" disabled={offset + 50 >= queue.total} onClick={() => setOffset(offset + 50)}>Далее</button>
    </>}
    {selected && <section ref={panel} tabIndex={-1} aria-label="Обработка исключения" className="procurement-exceptions__decision">
      <h3>{selected.title}</h3>
      <p>Решение будет записано от вашего имени.</p>
      {selected.facts.schedule && <table><caption>Состав ожидаемых поставок</caption><thead><tr><th>Количество</th><th>Ожидаемая дата</th><th>Состояние</th></tr></thead>
        <tbody>{selected.facts.schedule.map((item, index) => <tr key={`${item.order_ref}-${index}`}><td>{item.quantity}</td><td>{item.expected_at || "Не указана"}</td><td>{categoryNames[item.category]}</td></tr>)}</tbody></table>}
      <label>Результат <select value={status} onChange={(event) => setStatus(event.target.value)}>
        <option value="in_progress">В работе</option><option value="waiting">Ожидание ответа</option>
        {manualResolution.has(selected.reason_code) && <option value="resolved">Решено, проверка выполнена</option>}
      </select></label>
      {status !== "resolved" ? <>
        <label>Следующее действие <textarea value={action} onChange={(event) => setAction(event.target.value)} /></label>
        <label>Срок действия, МСК <input type="datetime-local" value={due} onChange={(event) => setDue(event.target.value)} /></label>
      </> : <>
        <label>Основание решения <textarea value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        <label>Чем подтверждён результат <textarea value={evidence} onChange={(event) => setEvidence(event.target.value)} /></label>
        {selected.reason_code === "supply_confirmation_required" && <label>Итоговое количество <input type="number" min="0.001" step="0.001" value={quantity} onChange={(event) => setQuantity(event.target.value)} /></label>}
      </>}
      {!manualResolution.has(selected.reason_code) && <p>Закроется после исправления данных и подтверждения повторным чтением источника.</p>}
      <button type="button" className="btn" disabled={busy || !order} onClick={() => void save()}>{busy ? "Сохранение…" : "Сохранить решение"}</button>{" "}
      <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => { selectionRequest.current++; setSelected(null); }}>Отмена</button>
    </section>}
  </main>;
}
