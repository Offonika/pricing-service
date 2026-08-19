import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  fetchProcurementAssortmentDecision,
  saveProcurementAssortmentDecision,
  syncProcurementAssortmentDecision,
  type ProcurementAssortmentDecision,
  type ProcurementAssortmentDecisionUpdate,
} from "../api/procurementAssortment";
import { procurementRiskLabel } from "../utils/procurementRiskLabels";

interface ProcurementAssortmentDecisionAppProps {
  bitrixUserName?: string | null;
  itemId: string;
}

// value — это xml_id значения в смарт-процессе Bitrix, его менять нельзя.
// Подпись показываем действующую, прежнюю оставляем в скобках.
const STATUS_OPTIONS = [
  { value: "no_change", label: "Без изменения" },
  { value: "matrix", label: "Держим всегда (Матричный)" },
  { value: "working", label: "Поддерживаем (Рабочий)" },
  { value: "on_demand", label: "Только под заказ (Под заказ)" },
  { value: "replace_candidate", label: "Меняем на аналог (Кандидат на замену)" },
  { value: "nonliquid", label: "Выводим (Неликвид)" },
  { value: "do_not_order", label: "Не закупаем (Не закупать)" },
];

interface DecisionForm {
  status_decision: string;
  status_reason: string;
  status_approved_by: string;
  status_changed_at: string;
  commercial_marks_text: string;
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function splitMarks(value: string) {
  return value
    .split(/[,\n;]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function statusLabel(value: string) {
  return STATUS_OPTIONS.find((option) => option.value === value)?.label || value || "Без изменения";
}

function formFromDecision(
  decision: ProcurementAssortmentDecision,
  bitrixUserName?: string | null
): DecisionForm {
  return {
    status_decision: decision.status_decision || "no_change",
    status_reason: decision.status_reason || "",
    status_approved_by: decision.status_approved_by || bitrixUserName || "",
    status_changed_at: decision.status_changed_at || todayIso(),
    commercial_marks_text: decision.commercial_marks.join(", "),
  };
}

function syncPreviewText(decision: ProcurementAssortmentDecision | null) {
  const preview = decision?.manual_override_preview;
  if (!preview) return "Решение пока не готово к применению";
  if (preview.manual_status) return `Статус: ${statusLabel(String(preview.manual_status))}`;
  if (preview.working_confirmed_by_folder_responsible) return "Подтверждение: рабочий товар";
  return "Решение будет сохранено как ручное правило";
}

export function ProcurementAssortmentDecisionApp({
  bitrixUserName,
  itemId,
}: ProcurementAssortmentDecisionAppProps) {
  const [decision, setDecision] = useState<ProcurementAssortmentDecision | null>(null);
  const [form, setForm] = useState<DecisionForm>({
    status_decision: "no_change",
    status_reason: "",
    status_approved_by: bitrixUserName || "",
    status_changed_at: todayIso(),
    commercial_marks_text: "",
  });
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<"save" | "sync" | null>(null);
  const [message, setMessage] = useState("");

  const blockers = useMemo(() => decision?.sync_blockers || [], [decision]);
  const canSync = Boolean(decision?.manual_override_preview && blockers.length === 0 && !actionLoading);

  const refresh = useCallback(async () => {
    if (!itemId) return;
    setLoading(true);
    setMessage("");
    try {
      const data = await fetchProcurementAssortmentDecision(itemId);
      setDecision(data);
      setForm(formFromDecision(data, bitrixUserName));
      setMessage("Данные карточки загружены.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Не удалось загрузить решение");
    } finally {
      setLoading(false);
    }
  }, [bitrixUserName, itemId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const payload = (): ProcurementAssortmentDecisionUpdate => ({
    status_decision: form.status_decision,
    status_reason: form.status_reason,
    status_approved_by: form.status_approved_by,
    status_changed_at: form.status_changed_at,
    commercial_marks: splitMarks(form.commercial_marks_text),
  });

  const save = async () => {
    if (!itemId) return;
    setActionLoading("save");
    setMessage("");
    try {
      const result = await saveProcurementAssortmentDecision(itemId, payload());
      setDecision(result.decision);
      setForm(formFromDecision(result.decision, bitrixUserName));
      toast.success("Решение сохранено в Bitrix");
      setMessage("Сохранено в карточке Bitrix.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Не удалось сохранить решение");
    } finally {
      setActionLoading(null);
    }
  };

  const saveAndSync = async () => {
    if (!itemId) return;
    setActionLoading("sync");
    setMessage("");
    try {
      const saved = await saveProcurementAssortmentDecision(itemId, payload());
      setDecision(saved.decision);
      const synced = await syncProcurementAssortmentDecision(itemId);
      setDecision(synced.decision);
      setForm(formFromDecision(synced.decision, bitrixUserName));
      if (synced.synced) {
        toast.success("Решение применено в правила");
        setMessage(
          synced.merge_action === "updated"
            ? "Правило обновлено в ручных решениях."
            : "Правило добавлено в ручные решения."
        );
      } else {
        setMessage(`Не применено: ${synced.blockers.map(procurementRiskLabel).join("; ")}`);
      }
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Не удалось применить решение");
    } finally {
      setActionLoading(null);
    }
  };

  if (!itemId) {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Ассортиментный статус</h1>
          <p>Bitrix24 не передал ID карточки закупки.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="app assortment-decision">
      <header className="app__header assortment-decision__header">
        <div>
          <h1>Ассортиментный статус</h1>
          <span>{decision?.title || `Карточка ${itemId}`}</span>
        </div>
        {bitrixUserName && <span className="app__user">{bitrixUserName}</span>}
        <button className="btn btn--ghost" disabled={loading || Boolean(actionLoading)} onClick={refresh} type="button">
          Проверить
        </button>
        <button className="btn btn--ghost" disabled={Boolean(actionLoading)} onClick={save} type="button">
          {actionLoading === "save" ? "Сохраняем..." : "Сохранить"}
        </button>
        <button className="btn" disabled={Boolean(actionLoading)} onClick={saveAndSync} type="button">
          {actionLoading === "sync" ? "Применяем..." : "Сохранить и применить"}
        </button>
      </header>

      <section className="assortment-decision__summary">
        <div>
          <span>Код 1С</span>
          <strong>{decision?.sku_code || "..."}</strong>
        </div>
        <div>
          <span>Товар</span>
          <strong>{decision?.sku_name || "..."}</strong>
        </div>
        <div>
          <span>Текущее решение</span>
          <strong>{statusLabel(decision?.status_decision || form.status_decision)}</strong>
        </div>
      </section>

      {message && <div className="assortment-decision__message">{message}</div>}

      <main className="assortment-decision__body">
        <section className="assortment-decision__panel">
          <label>
            <span>Решение</span>
            <select
              className="assortment-decision__control"
              disabled={loading || Boolean(actionLoading)}
              onChange={(event) => setForm((current) => ({ ...current, status_decision: event.target.value }))}
              value={form.status_decision}
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <label>
            <span>Причина</span>
            <textarea
              className="assortment-decision__control assortment-decision__textarea"
              disabled={loading || Boolean(actionLoading)}
              onChange={(event) => setForm((current) => ({ ...current, status_reason: event.target.value }))}
              placeholder="Например: собственный бренд F5, заказано 1000 шт., держим в матрице."
              value={form.status_reason}
            />
          </label>

          <div className="assortment-decision__grid">
            <label>
              <span>Утвердил</span>
              <input
                className="assortment-decision__control"
                disabled={loading || Boolean(actionLoading)}
                onChange={(event) => setForm((current) => ({ ...current, status_approved_by: event.target.value }))}
                value={form.status_approved_by}
              />
            </label>
            <label>
              <span>Дата</span>
              <input
                className="assortment-decision__control"
                disabled={loading || Boolean(actionLoading)}
                onChange={(event) => setForm((current) => ({ ...current, status_changed_at: event.target.value }))}
                type="date"
                value={form.status_changed_at}
              />
            </label>
          </div>

          <label>
            <span>Коммерческие признаки</span>
            <input
              className="assortment-decision__control"
              disabled={loading || Boolean(actionLoading)}
              onChange={(event) => setForm((current) => ({ ...current, commercial_marks_text: event.target.value }))}
              placeholder="own_brand, exclusive, rare_market_item"
              value={form.commercial_marks_text}
            />
          </label>
        </section>

        <aside className="assortment-decision__panel assortment-decision__panel--side">
          <div className={canSync ? "assortment-decision__sync assortment-decision__sync--ready" : "assortment-decision__sync"}>
            <span>Готовность</span>
            <strong>{canSync ? "Можно применить" : "Нужна проверка"}</strong>
          </div>
          <div className="assortment-decision__rule-preview">
            <span>В правилах автозаказа</span>
            <strong>{syncPreviewText(decision)}</strong>
          </div>
          {blockers.length > 0 && (
            <ul className="assortment-decision__blockers">
              {blockers.map((blocker) => (
                <li key={blocker} title={blocker}>{procurementRiskLabel(blocker)}</li>
              ))}
            </ul>
          )}
          {!blockers.length && decision?.manual_override_preview && (
            <p className="assortment-decision__note">
              После применения это решение попадет в ручные правила дисплеев.
            </p>
          )}
        </aside>
      </main>
    </div>
  );
}
