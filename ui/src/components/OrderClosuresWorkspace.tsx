import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  confirmClosureBatch,
  createExcelClosureBatch,
  createFilterClosureBatch,
  readClosureBatch,
  readClosureReasons,
  repeatClosureDiagnosis,
  type OrderClosureBatch,
  type OrderClosureReason,
  type OrderClosureReasonCode,
} from "../api/orderClosures";
import "../orderClosures.css";

interface Props {
  canConfirm: boolean;
  userName?: string | null;
}

type Choice = { selected: boolean; reason: OrderClosureReasonCode | "" };

const TERMINAL = new Set(["diagnosed", "applied", "stale", "failed", "canceled"]);

function factsText(facts: Record<string, unknown>) {
  const labels: Array<[string, string]> = [
    ["closure_count", "закрытий"],
    ["rtu_count", "РТУ"],
    ["payment_count", "оплат"],
    ["remaining", "остаток"],
    ["reserve", "резерв"],
    ["placement", "размещение"],
  ];
  return labels
    .filter(([key]) => facts[key] !== undefined)
    .map(([key, label]) => `${label}: ${String(facts[key])}`)
    .join(" · ");
}

function formatTimestamp(value: string | null) {
  return value ? new Date(value).toLocaleString("ru-RU") : "ещё не было";
}

export function OrderClosuresWorkspace({ canConfirm, userName }: Props) {
  const [source, setSource] = useState<"excel" | "filter">("excel");
  const [pastedText, setPastedText] = useState("");
  const [year, setYear] = useState(new Date().getFullYear() - 1);
  const [departmentRef, setDepartmentRef] = useState("");
  const [category, setCategory] = useState<"all" | "web" | "onec">("all");
  const [state, setState] = useState<"all" | "eligible" | "blocked" | "closed">("all");
  const [batch, setBatch] = useState<OrderClosureBatch | null>(null);
  const [reasons, setReasons] = useState<OrderClosureReason[]>([]);
  const [choices, setChoices] = useState<Record<number, Choice>>({});
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async (batchId: string) => {
    const next = await readClosureBatch(batchId);
    setBatch(next);
    if (next.status === "diagnosed") {
      const reasonRows = await readClosureReasons(next.id);
      setReasons(reasonRows);
      setChoices((current) => {
        const copy = { ...current };
        for (const item of next.items) {
          if (item.eligible && !copy[item.id]) copy[item.id] = { selected: false, reason: "" };
        }
        return copy;
      });
    }
    return next;
  }, []);

  useEffect(() => {
    if (!batch || TERMINAL.has(batch.status)) return;
    const timer = window.setInterval(() => {
      refresh(batch.id).catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [batch, refresh]);

  const selectedCount = useMemo(
    () => Object.values(choices).filter((choice) => choice.selected).length,
    [choices]
  );
  const eligibleCount = useMemo(
    () => batch?.items.filter((item) => item.eligible).length ?? 0,
    [batch]
  );
  const selectedRowsReady = useMemo(
    () =>
      selectedCount > 0 &&
      batch?.items.every(
        (item) => !choices[item.id]?.selected || (item.eligible && Boolean(choices[item.id]?.reason))
      ),
    [batch, choices, selectedCount]
  );
  const leaseExpired = Boolean(
    batch?.status === "leased" &&
      batch.lease_until &&
      new Date(batch.lease_until).getTime() < Date.now()
  );

  const startDryRun = async () => {
    setBusy(true);
    try {
      const created =
        source === "excel"
          ? await createExcelClosureBatch(pastedText)
          : await createFilterClosureBatch({
              year,
              department_ref: departmentRef || undefined,
              category,
              state,
            });
      setBatch(created);
      setChoices({});
      setReasons([]);
      toast.success("Dry-run поставлен в очередь УТ 10.3");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Не удалось создать dry-run");
    } finally {
      setBusy(false);
    }
  };

  const assignSelected = (reason: OrderClosureReasonCode) => {
    setChoices((current) =>
      Object.fromEntries(
        Object.entries(current).map(([id, choice]) => [
          id,
          choice.selected ? { ...choice, reason } : choice,
        ])
      )
    );
  };

  const confirm = async () => {
    if (!batch?.diagnosis_hash) return;
    const assignments = batch.items.filter((item) => choices[item.id]?.selected).map((item) => {
        const choice = choices[item.id];
        if (!choice?.reason) throw new Error("Для каждой выбранной строки требуется причина");
        const reason = reasons.find((row) => row.code === choice.reason);
        if (!reason?.ref) throw new Error("1С не вернула точную ссылку выбранной причины");
        return {
          item_id: item.id,
          reason_code: reason.code,
          reason_ref: reason.ref,
          reason_name: reason.name,
        };
      });
    if (!selectedRowsReady || assignments.length !== selectedCount) {
      toast.error("Выберите допустимые строки и назначьте каждой причину");
      return;
    }
    setBusy(true);
    try {
      setBatch(await confirmClosureBatch(batch, assignments));
      toast.success("Пакет подтверждён и передан в УТ 10.3");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Пакет не подтверждён");
    } finally {
      setBusy(false);
    }
  };

  const repeat = async () => {
    if (!batch) return;
    setBusy(true);
    try {
      setBatch(await repeatClosureDiagnosis(batch.id));
      setChoices({});
      toast.success("Повторная диагностика поставлена в очередь");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="closure-page">
      <header className="closure-hero">
        <div>
          <p className="closure-kicker">УТ 10.3 · безопасная операция</p>
          <h1>Закрытие заказов</h1>
          <p>Сначала 1С выполняет read-only проверку. Без подтверждения документы не создаются.</p>
        </div>
        <div className="closure-user">
          <span>{userName || "Пользователь Bitrix24"}</span>
          <strong>{canConfirm ? "Оператор закрытия" : "Только просмотр"}</strong>
        </div>
      </header>

      <section className="closure-card">
        <div className="closure-tabs" role="tablist" aria-label="Источник списка">
          <button className={source === "excel" ? "active" : ""} onClick={() => setSource("excel")}>Вставить из Excel</button>
          <button className={source === "filter" ? "active" : ""} onClick={() => setSource("filter")}>Сформировать по фильтрам</button>
        </div>
        {source === "excel" ? (
          <label className="closure-field closure-field--wide">
            <span>Номер и дата либо год — по одной строке на заказ</span>
            <textarea
              value={pastedText}
              onChange={(event) => setPastedText(event.target.value)}
              placeholder={"223210\t2026\n223211\t04.09.2026"}
              rows={6}
            />
          </label>
        ) : (
          <div className="closure-filters">
            <label className="closure-field"><span>Год</span><input type="number" min="2000" max="2100" value={year} onChange={(event) => setYear(Number(event.target.value))} /></label>
            <label className="closure-field"><span>Подразделение (ссылка 1С)</span><input value={departmentRef} onChange={(event) => setDepartmentRef(event.target.value)} /></label>
            <label className="closure-field"><span>Категория</span><select value={category} onChange={(event) => setCategory(event.target.value as typeof category)}><option value="all">Все</option><option value="web">WEB</option><option value="onec">Только 1С</option></select></label>
            <label className="closure-field"><span>Состояние</span><select value={state} onChange={(event) => setState(event.target.value as typeof state)}><option value="all">Все</option><option value="eligible">Допустимые</option><option value="blocked">С блокировкой</option><option value="closed">Уже закрытые</option></select></label>
          </div>
        )}
        <button className="closure-primary" disabled={busy || (source === "excel" && !pastedText.trim())} onClick={startDryRun}>Выполнить dry-run</button>
      </section>

      {batch ? (
        <section className="closure-card closure-results">
          <div className="closure-results__header">
            <div><p className="closure-kicker">Пакет {batch.id.slice(0, 8)}</p><h2>{batch.status === "diagnosed" ? "Результат проверки" : `Состояние: ${batch.status}`}</h2></div>
            {TERMINAL.has(batch.status) && batch.status !== "applied" ? <button className="closure-secondary" disabled={busy} onClick={repeat}>Повторить проверку</button> : null}
          </div>
          <p className={leaseExpired ? "closure-status stop" : "closure-kicker"}>
            Последний опрос 1С: {formatTimestamp(batch.last_polled_at)} · попыток: {batch.attempt_count}
            {batch.lease_until ? ` · аренда до ${formatTimestamp(batch.lease_until)}` : ""}
            {batch.last_error_code ? ` · ошибка: ${batch.last_error_code}` : ""}
          </p>
          {batch.status === "diagnosed" ? (
            <>
              <div className="closure-bulk">
                <span>Выбрано для массового назначения: {selectedCount} · допустимо строк: {eligibleCount}</span>
                <button onClick={() => assignSelected("execution")}>Назначить «Исполнение»</button>
                <button onClick={() => assignSelected("cancellation")}>Назначить «Отмена»</button>
              </div>
              <div className="closure-table-wrap">
                <table className="closure-table">
                  <thead><tr><th>Выбор</th><th>Заказ</th><th>Дата</th><th>Диагностика 1С</th><th>Причина</th><th>Результат</th></tr></thead>
                  <tbody>
                    {batch.items.map((item) => {
                      const choice = choices[item.id] || { selected: false, reason: "" };
                      return <tr key={item.id} className={item.eligible ? "" : "blocked"}>
                        <td><input aria-label={`Выбрать заказ ${item.onec_order_number || item.input_number}`} type="checkbox" disabled={!item.eligible || !canConfirm} checked={choice.selected} onChange={(event) => setChoices((current) => ({ ...current, [item.id]: { ...choice, selected: event.target.checked } }))} /></td>
                        <td><strong>{item.onec_order_number || item.input_number}</strong><small>{item.site_order_number ? `WEB ${item.site_order_number}` : item.department_name || ""}</small></td>
                        <td>{item.onec_order_date || item.input_period || "—"}</td>
                        <td><span className={`closure-status ${item.eligible ? "ok" : "stop"}`}>{item.eligible ? "Можно выбрать" : item.blocker_text || "Заблокирован"}</span><small>{factsText(item.facts)}</small></td>
                        <td><select disabled={!choice.selected || !canConfirm} value={choice.reason} onChange={(event) => setChoices((current) => ({ ...current, [item.id]: { ...choice, reason: event.target.value as OrderClosureReasonCode } }))}><option value="">Выберите</option>{reasons.map((reason) => <option key={reason.code} value={reason.code} disabled={!reason.ref}>{reason.name}</option>)}</select></td>
                        <td>{item.result_document_number || "—"}</td>
                      </tr>;
                    })}
                  </tbody>
                </table>
              </div>
              <div className="closure-confirm"><p>Перед выполнением УТ 10.3 повторит все выбранные строки. Одно расхождение остановит весь пакет.</p><button className="closure-danger" disabled={!canConfirm || busy || !selectedRowsReady} onClick={confirm}>Подтвердить и отправить в 1С</button></div>
            </>
          ) : batch.status === "applied" ? (
            <div className="closure-table-wrap">
              <table className="closure-table">
                <thead><tr><th>Заказ</th><th>Дата</th><th>Документ закрытия</th><th>Ссылка 1С</th></tr></thead>
                <tbody>{batch.items.map((item) => <tr key={item.id}>
                  <td>{item.onec_order_number || item.input_number}</td>
                  <td>{item.onec_order_date || item.input_period || "—"}</td>
                  <td>{item.result_document_number || "—"}</td>
                  <td><code>{item.result_document_ref || "—"}</code></td>
                </tr>)}</tbody>
              </table>
            </div>
          ) : <div className="closure-wait">{leaseExpired ? "Аренда зависла; команда будет выдана повторно после истечения lease." : "Ожидаем ответ УТ 10.3…"}</div>}
        </section>
      ) : null}
    </main>
  );
}
