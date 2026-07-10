import { useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  approveProcurementClassification,
  createProcurementClassification,
  submitProcurementOrder,
  updateProcurementOrderLine,
  type ProcurementOrderFormation,
  type ProcurementOrderFormationLine,
} from "../api/procurementAssortment";

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
  draft: "Черновик сформирован",
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

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "Операция не выполнена";
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
  const locked = order.status === "transmitting" || order.status === "transmitted";
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

  const saveLine = async (line: ProcurementOrderFormationLine) => {
    const edit = lineEdit(line);
    setLoadingKey(`line-${line.id}`);
    try {
      const updated = await updateProcurementOrderLine(order.id, line.id, {
        expected_order_version: order.version,
        expected_line_version: line.version,
        final_quantity: edit.quantity,
        purchase_price: edit.price,
      });
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
      const updated = await createProcurementClassification(order.id, line.id, {
        expected_order_version: order.version,
        expected_line_version: line.version,
        proposed_status: edit.status,
        reason: edit.reason,
        manual_minimum: edit.manualMinimum || null,
        review_date: edit.reviewDate || null,
      });
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
          <ul>{order.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
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
              {activeLines.map((line) => {
                const edit = lineEdit(line);
                const classification = classificationEdit(line);
                const proposal = line.latest_classification;
                return (
                  <tr key={line.id} className={line.blockers.length ? "order-formation__row--blocked" : ""}>
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
                        disabled={locked}
                        onClick={() => setOpenedClassification(openedClassification === line.id ? null : line.id)}
                        type="button"
                      >
                        Изменить классификацию
                      </button>
                      {proposal?.status === "proposed" && (
                        <button
                          className="btn btn--small"
                          disabled={Boolean(loadingKey)}
                          onClick={() => approveClassification(line)}
                          type="button"
                        >
                          Согласовать отдельно
                        </button>
                      )}
                      {openedClassification === line.id && (
                        <div className="order-formation__classification">
                          <select
                            disabled={locked}
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
                            disabled={locked}
                            placeholder="Обязательная причина"
                            value={classification.reason}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, reason: event.target.value },
                            }))}
                          />
                          <input
                            disabled={locked}
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
                            disabled={locked}
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
                      {line.recommendation_reason && <small>{line.recommendation_reason}</small>}
                      {line.risk_codes.map((risk) => <small key={risk}>Риск: {risk}</small>)}
                    </td>
                    <td>
                      <input
                        min="0"
                        disabled={locked}
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
                        disabled={locked}
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
                        disabled={Boolean(loadingKey) || locked}
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
