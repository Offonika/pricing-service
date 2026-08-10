import { useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  approveProcurementClassification,
  createProcurementClassification,
  fetchProcurementOrder,
  submitProcurementOrder,
  updateProcurementOrderLine,
  type ProcurementOrderFormation,
  type ProcurementOrderFormationLine,
} from "../api/procurementAssortment";
import { procurementRiskLabel } from "../utils/procurementRiskLabels";

interface Props {
  bitrixUserName?: string | null;
  initialOrder: ProcurementOrderFormation;
  onBack?: () => void;
}

interface LineEdit {
  quantity: string;
  price: string;
}

interface ClassificationEdit {
  status: string;
  reason: string;
  manualMinimum: string;
  reviewDate: string;
}

const ORDER_STATUS_LABELS: Record<string, string> = {
  draft: "Заказ на подтверждении",
  review: "На проверке",
  approved: "Согласовано к 1С",
  transmitting: "Передача в 1С",
  transmitted: "Передано в 1С",
  deferred: "Отложено / отменено",
  error: "Ошибка передачи",
};

function money(value: string, currency: string) {
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(number);
}

const ERROR_MESSAGES: Record<string, string> = {
  "order version changed; refresh the order":
    "Заказ уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.",
  "order line version changed; refresh the order":
    "Строку уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.",
  "transmitted order is read-only; create a new version":
    "Заказ уже передан в 1С, его нельзя менять. Создайте новую версию заказа.",
  "approved order is read-only; create a new revision":
    "Подтверждённый заказ заморожен. Новый расчёт создаст отдельную ревизию.",
  "classification proposal cannot be self-approved":
    "Своё предложение согласовать нельзя — нужен второй сотрудник.",
  "user cannot approve product classification":
    "У вас нет прав согласовывать классификацию товара.",
  "classification reason is required": "Укажите причину изменения классификации.",
  "review date is required when manual minimum is set":
    "При ручном минимуме обязательно укажите дату пересмотра.",
  "manual minimum cannot be negative": "Ручной минимум не может быть отрицательным.",
};

const LINE_CHANGED_MESSAGE =
  "Строку уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.";

function errorStatus(error: unknown) {
  return (error as { response?: { status?: number } })?.response?.status;
}

function errorText(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  if (detail) return ERROR_MESSAGES[detail] || detail;
  if (error instanceof Error) return ERROR_MESSAGES[error.message] || error.message;
  return "Операция не выполнена";
}

export function ProcurementOrderFormationApp({ bitrixUserName, initialOrder, onBack }: Props) {
  const [order, setOrder] = useState(initialOrder);
  const [lineEdits, setLineEdits] = useState<Record<number, LineEdit>>({});
  const [classificationEdits, setClassificationEdits] = useState<
    Record<number, ClassificationEdit>
  >({});
  const [openedClassification, setOpenedClassification] = useState<number | null>(null);
  const [loadingKey, setLoadingKey] = useState("");

  const activeLines = useMemo(() => order.lines.filter((line) => !line.removed), [order.lines]);
  const visibleLines = useMemo(
    () => [...order.lines].sort((left, right) => left.line_number - right.line_number),
    [order.lines]
  );
  const locked = ["approved", "transmitting", "transmitted"].includes(order.status);
  const draftTotal = useMemo(
    () => activeLines.reduce((total, line) => {
      const edit = lineEdits[line.id];
      const quantity = Number(edit?.quantity ?? line.final_quantity);
      const price = Number(edit?.price ?? line.purchase_price);
      return total + (Number.isFinite(quantity * price) ? quantity * price : 0);
    }, 0),
    [activeLines, lineEdits]
  );

  const lineEdit = (line: ProcurementOrderFormationLine): LineEdit =>
    lineEdits[line.id] || { quantity: line.final_quantity, price: line.purchase_price };

  const classificationEdit = (line: ProcurementOrderFormationLine): ClassificationEdit =>
    classificationEdits[line.id] || {
      status: line.effective_assortment_status || "working",
      reason: "",
      manualMinimum: line.manual_minimum || "",
      reviewDate: "",
    };

  // Версия заказа растёт от любой правки, в том числе в соседней вкладке или у другого
  // закупщика. Поэтому на 409 перезагружаем карточку и повторяем действие, если сама
  // строка не менялась; иначе просим проверить обновлённые данные вручную.
  const runVersioned = async (
    line: ProcurementOrderFormationLine,
    action: (versions: {
      orderVersion: number;
      lineVersion: number;
    }) => Promise<ProcurementOrderFormation>
  ): Promise<ProcurementOrderFormation> => {
    try {
      return await action({ orderVersion: order.version, lineVersion: line.version });
    } catch (error: unknown) {
      if (errorStatus(error) !== 409) throw error;
      const fresh = await fetchProcurementOrder(order.id);
      setOrder(fresh);
      const freshLine = fresh.lines.find((item) => item.id === line.id);
      if (!freshLine || freshLine.version !== line.version) {
        throw new Error(LINE_CHANGED_MESSAGE);
      }
      return action({ orderVersion: fresh.version, lineVersion: freshLine.version });
    }
  };

  const saveLine = async (line: ProcurementOrderFormationLine) => {
    const edit = lineEdit(line);
    setLoadingKey(`line-${line.id}`);
    try {
      const updated = await runVersioned(line, ({ orderVersion, lineVersion }) =>
        updateProcurementOrderLine(order.id, line.id, {
          expected_order_version: orderVersion,
          expected_line_version: lineVersion,
          final_quantity: edit.quantity,
          purchase_price: edit.price,
        })
      );
      setOrder(updated);
      setLineEdits((current) => {
        const next = { ...current };
        delete next[line.id];
        return next;
      });
      toast.success("Строка сохранена, согласование версии снято");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const saveClassification = async (line: ProcurementOrderFormationLine) => {
    const edit = classificationEdit(line);
    setLoadingKey(`class-${line.id}`);
    try {
      const updated = await runVersioned(line, ({ orderVersion, lineVersion }) =>
        createProcurementClassification(order.id, line.id, {
          expected_order_version: orderVersion,
          expected_line_version: lineVersion,
          proposed_status: edit.status,
          reason: edit.reason,
          manual_minimum: edit.manualMinimum || null,
          review_date: edit.reviewDate || null,
        })
      );
      setOrder(updated);
      setOpenedClassification(null);
      toast.success("Классификация отправлена на отдельное согласование");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const approveClassification = async (line: ProcurementOrderFormationLine) => {
    const proposal = line.latest_classification;
    if (!proposal) return;
    setLoadingKey(`approve-class-${line.id}`);
    try {
      const result = await approveProcurementClassification(order.id, line.id, proposal.id);
      setOrder(result.order);
      toast.success(
        result.mode === "apply"
          ? "Изменение передано в 1С"
          : "Классификация согласована, сформирован dry-run XML"
      );
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const submitOrder = async () => {
    setLoadingKey("submit-order");
    try {
      const result = await submitProcurementOrder(order.id);
      setOrder(result.order);
      toast.success(
        result.mode === "apply"
          ? `Черновик передан в 1С: ${result.message_id}`
          : `Заказ проверен, dry-run без записи в 1С: ${result.message_id}`
      );
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  return (
    <div className="app order-formation">
      <header className="app__header order-formation__header">
        {onBack && <button className="btn btn--ghost" onClick={onBack} type="button">К заказам</button>}
        <div>
          <h1>Формирование заказа</h1>
          <span>
            {ORDER_STATUS_LABELS[order.status] || order.status} · версия {order.version}
          </span>
        </div>
        {bitrixUserName && <span className="app__user">{bitrixUserName}</span>}
      </header>

      <section className="order-formation__conditions">
        <div><span>Поставщик</span><strong>{order.supplier_name}</strong></div>
        <div><span>Договор</span><strong>{order.contract_name}</strong></div>
        <div><span>Склад</span><strong>{order.warehouse_name}</strong></div>
        <div><span>Маршрут</span><strong>{order.route}</strong></div>
        <div><span>Партия</span><strong>{order.batch_id}</strong></div>
        <div><span>Дата</span><strong>{order.order_date}</strong></div>
      </section>

      {order.blockers.length > 0 && (
        <section className="order-formation__alert">
          <strong>Передача заблокирована</strong>
          <ul>
            {order.blockers.map((blocker) => (
              <li key={blocker} title={blocker}>{procurementRiskLabel(blocker)}</li>
            ))}
          </ul>
        </section>
      )}

      <main className="order-formation__body">
        <div className="order-formation__table-wrap">
          <table className="order-formation__table">
            <thead>
              <tr>
                <th>Товар</th>
                <th>Классификация</th>
                <th>Рекомендация</th>
                <th>Количество</th>
                <th>Закупочная цена</th>
                <th>Сумма</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visibleLines.map((line) => {
                const edit = lineEdit(line);
                const classification = classificationEdit(line);
                const proposal = line.latest_classification;
                const b2bDemand = line.payload?.b2b_customer_demand;
                return (
                  <tr
                    key={line.id}
                    className={line.blockers.length || line.removed ? "order-formation__row--blocked" : ""}
                  >
                    <td>
                      <strong>{line.nomenclature_name}</strong>
                      <small>1С: {line.nomenclature_code || line.nomenclature_ref}</small>
                      <small>Bitrix product: {line.bitrix_product_id || "не найден"}</small>
                      {line.quality && <small>Качество: {line.quality}</small>}
                    </td>
                    <td>
                      <strong>{line.effective_assortment_status_label || "Не задана"}</strong>
                      {line.lifecycle_status && <small>Жизненный статус: {line.lifecycle_status}</small>}
                      {line.procurement_profile && <small>Профиль: {line.procurement_profile}</small>}
                      {proposal && <small>Предложение: {proposal.proposed_status_label} · {proposal.status}</small>}
                      <button
                        className="btn btn--ghost btn--small"
                        disabled={locked || line.removed}
                        onClick={() => setOpenedClassification(openedClassification === line.id ? null : line.id)}
                        type="button"
                      >
                        Изменить классификацию
                      </button>
                      {proposal?.status === "proposed" && (
                        <button
                          className="btn btn--small"
                          disabled={Boolean(loadingKey) || locked}
                          onClick={() => approveClassification(line)}
                          type="button"
                        >
                          Согласовать отдельно
                        </button>
                      )}
                      {openedClassification === line.id && (
                        <div className="order-formation__classification">
                          <select
                            disabled={locked || line.removed}
                            value={classification.status}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, status: event.target.value },
                            }))}
                          >
                            {Object.entries(order.manual_status_options).map(([value, label]) => (
                              <option key={value} value={value}>{label}</option>
                            ))}
                          </select>
                          <textarea
                            disabled={locked || line.removed}
                            placeholder="Обязательная причина"
                            value={classification.reason}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, reason: event.target.value },
                            }))}
                          />
                          <input
                            disabled={locked || line.removed}
                            min="0"
                            placeholder="Ручной минимум"
                            step="0.001"
                            type="number"
                            value={classification.manualMinimum}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, manualMinimum: event.target.value },
                            }))}
                          />
                          <input
                            disabled={locked || line.removed}
                            type="date"
                            value={classification.reviewDate}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, reviewDate: event.target.value },
                            }))}
                          />
                          <button
                            className="btn btn--small"
                            disabled={!classification.reason.trim() || Boolean(loadingKey) || locked}
                            onClick={() => saveClassification(line)}
                            type="button"
                          >
                            На согласование
                          </button>
                        </div>
                      )}
                    </td>
                    <td>
                      <strong>{line.recommended_quantity}</strong>
                      {line.removed && (
                        <small className="is-warning">Потребность исчезла в новом расчёте</small>
                      )}
                      {line.payload?.recommendation_discrepancy?.final_quantity && (
                        <small className="is-warning">
                          Решение человека: {line.payload.recommendation_discrepancy.final_quantity.manual} · новый расчёт: {line.payload.recommendation_discrepancy.final_quantity.recommended}
                        </small>
                      )}
                      {line.payload?.recommendation_discrepancy?.purchase_price && (
                        <small className="is-warning">
                          Цена человека: {line.payload.recommendation_discrepancy.purchase_price.manual} · новая цена: {line.payload.recommendation_discrepancy.purchase_price.recommended}
                        </small>
                      )}
                      {line.recommendation_reason && <small>{line.recommendation_reason}</small>}
                      {b2bDemand && (
                        <div className="order-formation__b2b-advisory">
                          <strong>
                            Клиенты 3/4/5: {b2bDemand.replacement_recommended_order_qty || "0"} шт.
                          </strong>
                          <small>
                            Альтернативный расчёт, основной заказ не изменён
                            {b2bDemand.order_delta_qty
                              ? ` · разница ${Number(b2bDemand.order_delta_qty) > 0 ? "+" : ""}${b2bDemand.order_delta_qty} шт.`
                              : ""}
                          </small>
                          <small>
                            Активных: {b2bDemand.active_customer_count ?? 0} · пассивных:{" "}
                            {b2bDemand.passive_customer_count ?? 0} · ожидаются к сроку:{" "}
                            {b2bDemand.due_customer_count ?? 0}
                          </small>
                          {b2bDemand.dependency_class && <small>{b2bDemand.dependency_class}</small>}
                          {b2bDemand.reason_ru && (
                            <details>
                              <summary>Почему так рассчитано</summary>
                              <small>{b2bDemand.reason_ru}</small>
                            </details>
                          )}
                        </div>
                      )}
                      {line.risk_codes.map((risk) => (
                        <small key={risk} title={risk}>
                          Сигнал: {procurementRiskLabel(risk)}
                        </small>
                      ))}
                    </td>
                    <td>
                      <input
                        min="0"
                        disabled={locked || line.removed}
                        step="0.001"
                        type="number"
                        value={edit.quantity}
                        onChange={(event) => setLineEdits((current) => ({
                          ...current,
                          [line.id]: { ...edit, quantity: event.target.value },
                        }))}
                      />
                    </td>
                    <td>
                      <input
                        min="0"
                        disabled={locked || line.removed}
                        step="0.01"
                        type="number"
                        value={edit.price}
                        onChange={(event) => setLineEdits((current) => ({
                          ...current,
                          [line.id]: { ...edit, price: event.target.value },
                        }))}
                      />
                    </td>
                    <td><strong>{money(String(Number(edit.quantity) * Number(edit.price)), line.currency)}</strong></td>
                    <td>
                      <button
                        className="btn btn--ghost btn--small"
                        disabled={Boolean(loadingKey) || locked || line.removed}
                        onClick={() => saveLine(line)}
                        type="button"
                      >
                        Сохранить
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </main>

      <footer className="order-formation__footer">
        <span>{activeLines.length} строк</span>
        <strong>Итого: {money(String(draftTotal), order.currency)}</strong>
        <span>1С: {order.onec_status}</span>
        {order.approved_by_name && <span>Согласовал: {order.approved_by_name}</span>}
        {!locked && (
          <button
            className="btn"
            disabled={Boolean(loadingKey) || order.blockers.length > 0}
            onClick={submitOrder}
            type="button"
          >
            {loadingKey === "submit-order" ? "Проверяем и передаём..." : "Проверил и создать черновик в 1С"}
          </button>
        )}
      </footer>
    </div>
  );
}
