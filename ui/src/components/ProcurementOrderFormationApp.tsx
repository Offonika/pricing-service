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
import { procurementErrorText } from "../utils/procurementErrorMessages";
import {
  groupProcurementBlockers,
  procurementBlockerText,
  procurementRiskLabel,
} from "../utils/procurementRiskLabels";

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
  replacementSkuCode: string;
  noReplacement: boolean;
}

// Статусы, снимающие карточку с ведения: для них нужен код карточки-победителя
// семьи либо явная отметка «замены нет» (решение 2026-08-18).
const REPLACEMENT_REQUIRED_STATUSES = new Set(["pension", "replace_candidate", "do_not_order"]);

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

function numeric(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function percent(value: unknown) {
  const parsed = numeric(value);
  return parsed === null
    ? "нет данных"
    : `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function payloadValue(line: ProcurementOrderFormationLine, key: string) {
  return line.payload?.[key];
}

function lineProblemTexts(line: ProcurementOrderFormationLine, batchId: string) {
  const values = line.blockers.map((code) => {
    if (code === "batch_error_suspected") {
      const returned = payloadValue(line, "batch_error_return_qty") || "?";
      const share = percent(payloadValue(line, "batch_error_share_pct"));
      return `Подозрение на пересорт: ${returned} возвратов (${share} продаж). Точная партия поставки в источнике не определена; расчёт ${batchId}.`;
    }
    if (code === "defect_rate_suspected") {
      const returned = payloadValue(line, "defect_return_qty") || "?";
      const share = percent(payloadValue(line, "defect_share_pct"));
      return `Высокий процент брака: ${share} (${returned} возвратов); автозаказ остановлен.`;
    }
    if (code === "supplier_defect_over_10_pct_reliable") {
      const basis = line.supplier_defect_history_units
        ? ` на базе ${line.supplier_defect_history_units.toLocaleString("ru-RU")} шт.`
        : "";
      return `У выбранного поставщика подтверждённый брак ${percent(line.supplier_defect_pct)}${basis}`;
    }
    if (code === "purchase_price_change_over_10_pct") {
      return `Закупочная цена изменилась на ${percent(line.price_change_pct)} — нужна проверка.`;
    }
    return procurementRiskLabel(code);
  });
  if (line.removed) values.unshift("Потребность исчезла в новом расчёте.");
  return [...new Set(values)];
}

function familyQuantityChanged(line: ProcurementOrderFormationLine) {
  const recommendation = line.display_family_recommendation;
  if (!recommendation) return false;
  const baseline = numeric(recommendation.baseline_order_qty);
  const allocated = numeric(recommendation.allocated_order_qty);
  return baseline !== null && allocated !== null && baseline !== allocated;
}

function visibleRecommendationReason(line: ProcurementOrderFormationLine) {
  if (!line.recommendation_reason) return null;
  const family = line.display_family_recommendation;
  if (family && line.recommendation_reason === family.reason_ru && !familyQuantityChanged(line)) {
    return null;
  }
  return line.recommendation_reason;
}

function roundingExplanation(line: ProcurementOrderFormationLine) {
  const family = line.display_family_recommendation;
  if (family && familyQuantityChanged(line)) {
    return `Семейное перераспределение: базово ${family.baseline_order_qty} шт., итог ${family.allocated_order_qty} шт.; после распределения применяется целое количество, а не кратность SKU.`;
  }
  const raw = payloadValue(line, "recommended_order_qty_raw");
  const multiple = payloadValue(line, "order_rounding_multiple");
  const gate = String(payloadValue(line, "order_rounding_price_gate") || "");
  const gateText = String(payloadValue(line, "order_rounding_price_gate_ru") || "");
  const median = payloadValue(line, "order_rounding_group_median_price");
  if (gate === "above_median") {
    return `Округление не применено: цена карточки выше медианы группы${median ? ` (${median})` : ""}.`;
  }
  if (gate === "no_purchase_price") {
    return "Округление не применено: нет подтверждённой закупочной цены.";
  }
  if (raw && multiple && numeric(raw) !== numeric(line.recommended_quantity)) {
    return `Округление: ${raw} → ${line.recommended_quantity} шт., кратность ${multiple}.`;
  }
  if (gateText) return `Округление: ${gateText}.`;
  return null;
}


const LINE_CHANGED_MESSAGE =
  "Строку уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.";

function errorStatus(error: unknown) {
  return (error as { response?: { status?: number } })?.response?.status;
}

function errorText(error: unknown) {
  return procurementErrorText(error);
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
    () => [...order.lines].sort((left, right) => {
      const leftProblem = left.blockers.length > 0 || left.removed ? 1 : 0;
      const rightProblem = right.blockers.length > 0 || right.removed ? 1 : 0;
      return rightProblem - leftProblem || left.line_number - right.line_number;
    }),
    [order.lines]
  );
  // Один и тот же блокер приходит по каждой проблемной строке отдельно, поэтому
  // без группировки экран показывал несколько одинаковых фраз подряд.
  const blockerGroups = useMemo(
    () => groupProcurementBlockers(order.blockers),
    [order.blockers]
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
      replacementSkuCode: "",
      noReplacement: false,
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
          replacement_sku_code: edit.replacementSkuCode.trim() || null,
          no_replacement: edit.noReplacement,
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

      {blockerGroups.length > 0 && (
        <section className="order-formation__alert">
          <strong>Передача заблокирована</strong>
          <ul>
            {blockerGroups.map((group) => (
              <li key={group.text} title={group.codes.join(", ")}>
                {procurementBlockerText(group)}
              </li>
            ))}
          </ul>
        </section>
      )}

      <main className="order-formation__body">
        <div className="order-formation__table-wrap">
          <table className="order-formation__table">
            <thead>
              <tr>
                <th>№</th>
                <th>Товар</th>
                <th>Классификация</th>
                <th>Проблема / рекомендация</th>
                <th>Кол-во</th>
                <th>Цена закупки</th>
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
                const problems = lineProblemTexts(line, order.batch_id);
                const recommendationReason = visibleRecommendationReason(line);
                const rounding = roundingExplanation(line);
                const defectValue = payloadValue(line, "defect_share_pct")
                  ?? line.supplier_defect_pct
                  ?? line.product_defect_pct;
                return (
                  <tr
                    key={line.id}
                    className={line.blockers.length || line.removed ? "order-formation__row--blocked" : ""}
                  >
                    <td className="order-formation__line-number">{line.line_number}</td>
                    <td>
                      <strong>{line.nomenclature_name}</strong>
                      <small>1С: {line.nomenclature_code || line.nomenclature_ref}</small>
                      <small>Bitrix product: {line.bitrix_product_id || "не найден"}</small>
                      {line.quality && <small>Качество: {line.quality}</small>}
                    </td>
                    <td>
                      <strong>{line.effective_assortment_status_label || "Не задана"}</strong>
                      {line.lifecycle_status &&
                        line.lifecycle_status !== line.effective_assortment_status_label && (
                          <small>Жизненный статус: {line.lifecycle_status}</small>
                        )}
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
                          {REPLACEMENT_REQUIRED_STATUSES.has(classification.status) && (
                            <>
                              <input
                                disabled={locked || line.removed || classification.noReplacement}
                                placeholder="Взамен ведём: код 1С (РБ...)"
                                type="text"
                                value={classification.replacementSkuCode}
                                onChange={(event) => setClassificationEdits((current) => ({
                                  ...current,
                                  [line.id]: {
                                    ...classification,
                                    replacementSkuCode: event.target.value,
                                  },
                                }))}
                              />
                              <label className="order-formation__no-replacement">
                                <input
                                  checked={classification.noReplacement}
                                  disabled={locked || line.removed}
                                  type="checkbox"
                                  onChange={(event) => setClassificationEdits((current) => ({
                                    ...current,
                                    [line.id]: {
                                      ...classification,
                                      noReplacement: event.target.checked,
                                      replacementSkuCode: event.target.checked
                                        ? ""
                                        : classification.replacementSkuCode,
                                    },
                                  }))}
                                />
                                Замены нет: снято с производства
                              </label>
                            </>
                          )}
                          <button
                            className="btn btn--small"
                            disabled={
                              !classification.reason.trim() ||
                              (REPLACEMENT_REQUIRED_STATUSES.has(classification.status) &&
                                !classification.replacementSkuCode.trim() &&
                                !classification.noReplacement) ||
                              Boolean(loadingKey) ||
                              locked
                            }
                            onClick={() => saveClassification(line)}
                            type="button"
                          >
                            {classification.status === "pension" ? "Перевести в Допродаём" : "На согласование"}
                          </button>
                        </div>
                      )}
                    </td>
                    <td>
                      <strong>{line.recommended_quantity}</strong>
                      {problems.length > 0 && (
                        <strong className="is-warning">Проблема: {problems[0]}</strong>
                      )}
                      {problems.slice(1).map((problem) => (
                        <small className="is-warning" key={problem}>Также: {problem}</small>
                      ))}
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
                      {recommendationReason && <small>Рекомендация: {recommendationReason}</small>}
                      <small>Брак: {percent(defectValue)}</small>
                      <small>Рентабельность: {percent(line.profitability_pct)}</small>
                      {rounding && <small>{rounding}</small>}
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
                      {line.product_card_url && (
                        <a
                          className="order-formation__product-card-link"
                          href={line.product_card_url}
                          rel="noreferrer"
                          target="_blank"
                        >
                          Открыть карточку
                        </a>
                      )}
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
