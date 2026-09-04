import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowPathIcon,
  ArrowTopRightOnSquareIcon,
  CheckCircleIcon,
  CubeIcon,
  ExclamationTriangleIcon,
  ShoppingCartIcon,
  TruckIcon,
} from "@heroicons/react/24/outline";
import {
  fetchProcurementOrder,
  fetchProcurementProductCard,
  updateProcurementOrderLine,
  type ProcurementOrderFormation,
  type ProcurementOrderFormationLine,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { resolveBitrixPortalUrl } from "../api/bitrix";
import { procurementErrorText } from "../utils/procurementErrorMessages";
import { procurementRiskLabel } from "../utils/procurementRiskLabels";

interface Props {
  productId: string;
  orderId?: number;
  lineId?: number;
}

interface LineEdit {
  quantity: string;
  price: string;
}

const DEMAND_WINDOWS = [
  { key: "rate_180", label: "180 дней" },
  { key: "rate_90", label: "90 дней" },
  { key: "rate_30", label: "30 дней" },
] as const;

const CONFIDENCE_LABELS: Record<string, string> = {
  high: "Высокая",
  medium: "Средняя",
  low: "Низкая",
};

const ORDER_STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  review: "На проверке",
  approved: "Подтверждён",
  transmitting: "Передаётся",
  transmitted: "Передан",
};

const ONEC_STATUS_LABELS: Record<string, string> = {
  not_sent: "не передан",
  pending: "ожидается",
  created: "создан",
  accepted: "принят",
  error: "ошибка",
};

const LOCKED_ORDER_STATUSES = new Set(["approved", "transmitting", "transmitted"]);
const LINE_CHANGED_MESSAGE =
  "Строку уже изменили в другом окне. Данные обновлены — проверьте их и повторите.";

function text(value: unknown, fallback = "Нет данных") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function number(value: unknown, suffix = "") {
  if (value === null || value === undefined || value === "") return "Нет данных";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 3 })}${suffix}`;
}

function price(value: unknown, currency: unknown) {
  const amount = number(value);
  if (amount === "Нет данных") return amount;
  const currencyCode = text(currency, "");
  return currencyCode === "RUB" ? `${amount} ₽` : [amount, currencyCode].filter(Boolean).join(" ");
}

function finiteNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function dictionaryLabel(labels: Record<string, string>, value: unknown) {
  const raw = text(value);
  return labels[raw] || raw;
}

function errorStatus(error: unknown) {
  return (error as { response?: { status?: number } })?.response?.status;
}

function metricTone(value: unknown, goodBelow = false) {
  const numeric = finiteNumber(value);
  if (numeric === null) return "neutral";
  if (goodBelow) return numeric <= 1 ? "positive" : numeric <= 3 ? "warning" : "negative";
  return numeric >= 20 ? "positive" : numeric >= 10 ? "warning" : "negative";
}

function inventoryRows(data: ProcurementProductCard) {
  return [
    {
      label: "Текущий остаток",
      value: number(data.demand.sellable_stock, " шт."),
      hint: "Доступно к продаже",
      icon: CubeIcon,
      tone: "blue",
    },
    {
      label: "В пути",
      value: number(data.demand.incoming, " шт."),
      hint: data.supply.lead_time_days
        ? `Срок поставки ${number(data.supply.lead_time_days, " дн.")}`
        : "Ожидается поставка",
      icon: TruckIcon,
      tone: "green",
    },
    {
      label: "Под заказы",
      value: number(data.demand.customer_orders, " шт."),
      hint: "Активный спрос клиентов",
      icon: ShoppingCartIcon,
      tone: "violet",
    },
  ];
}

export function ProcurementProductInsights({ productId, orderId, lineId }: Props) {
  const [data, setData] = useState<ProcurementProductCard | null>(null);
  const [order, setOrder] = useState<ProcurementOrderFormation | null>(null);
  const [error, setError] = useState("");
  const [orderError, setOrderError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [edit, setEdit] = useState<LineEdit>({ quantity: "", price: "" });
  const hasOrderContext = Boolean(orderId && lineId);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setOrderError("");
    const [productResult, orderResult] = await Promise.allSettled([
      fetchProcurementProductCard(productId),
      hasOrderContext && orderId ? fetchProcurementOrder(orderId) : Promise.resolve(null),
    ]);
    if (productResult.status === "fulfilled") setData(productResult.value);
    else setError(procurementErrorText(productResult.reason));
    if (orderResult.status === "fulfilled") setOrder(orderResult.value);
    else setOrderError(procurementErrorText(orderResult.reason));
    setLoading(false);
  }, [hasOrderContext, orderId, productId]);

  useEffect(() => {
    if (!productId) {
      setLoading(false);
      return;
    }
    void load();
  }, [load, productId]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = data?.identity.name
      ? `${data.identity.name} — показатели товара`
      : "Показатели товара";
    return () => { document.title = previousTitle; };
  }, [data?.identity.name]);

  const orderLine = useMemo(
    () => order?.lines.find((line) => line.id === lineId) || null,
    [lineId, order]
  );

  useEffect(() => {
    if (orderLine) setEdit({ quantity: orderLine.final_quantity, price: orderLine.purchase_price });
  }, [orderLine]);

  const chart = useMemo(() => {
    if (!data) return [];
    const values = DEMAND_WINDOWS.map((item) => ({
      ...item,
      value: data.demand[item.key],
      numericValue: finiteNumber(data.demand[item.key]),
    }));
    const max = Math.max(...values.map((item) => item.numericValue ?? 0), 0);
    return values.map((item) => ({
      ...item,
      height: item.numericValue !== null && max > 0
        ? Math.max(18, (item.numericValue / max) * 100)
        : 0,
    }));
  }, [data]);

  const saveLine = async (line: ProcurementOrderFormationLine) => {
    if (!order) return;
    setSaving(true);
    setSaveMessage("");
    try {
      let updated: ProcurementOrderFormation;
      try {
        updated = await updateProcurementOrderLine(order.id, line.id, {
          expected_order_version: order.version,
          expected_line_version: line.version,
          final_quantity: edit.quantity,
          purchase_price: edit.price,
        });
      } catch (requestError: unknown) {
        if (errorStatus(requestError) !== 409) throw requestError;
        const freshOrder = await fetchProcurementOrder(order.id);
        setOrder(freshOrder);
        const freshLine = freshOrder.lines.find((item) => item.id === line.id);
        if (!freshLine || freshLine.version !== line.version) throw new Error(LINE_CHANGED_MESSAGE);
        updated = await updateProcurementOrderLine(freshOrder.id, freshLine.id, {
          expected_order_version: freshOrder.version,
          expected_line_version: freshLine.version,
          final_quantity: edit.quantity,
          purchase_price: edit.price,
        });
      }
      setOrder(updated);
      setSaveMessage("Строка подтверждена и сохранена");
    } catch (requestError: unknown) {
      setSaveMessage(procurementErrorText(requestError));
    } finally {
      setSaving(false);
    }
  };

  if (!productId) {
    return <div className="product-insights product-insights--state">Bitrix24 не передал ID товара.</div>;
  }
  if (loading) {
    return (
      <div className="product-insights product-insights--state">
        <ArrowPathIcon aria-hidden="true" className="product-insights__loading-icon" />
        Загрузка показателей товара…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="product-insights product-insights--state">
        <strong>Не удалось загрузить показатели</strong>
        <span>{error || "Данные товара не найдены"}</span>
        <button className="btn" onClick={() => void load()} type="button">Повторить</button>
      </div>
    );
  }

  const sourceState = text(data.source.state, "missing");
  const sourceClass = sourceState === "ready" ? "is-ready" : "is-warning";
  const stateClass = data.blockers.length ? "is-blocked" : sourceClass;
  const primaryBlocker = data.blockers[0];
  const relatedOrder = data.orders[0];
  const relatedOrderUrl = relatedOrder
    ? `${relatedOrder.app_url}${primaryBlocker?.line_id
      ? `${relatedOrder.app_url.includes("?") ? "&" : "?"}line=${primaryBlocker.line_id}`
      : ""}`
    : null;
  const recommendationText = text(data.recommendation, "Рекомендация пока не рассчитана");
  const locked = Boolean(order && LOCKED_ORDER_STATUSES.has(order.status));
  const editIsValid = finiteNumber(edit.quantity) !== null
    && Number(edit.quantity) >= 0
    && finiteNumber(edit.price) !== null
    && Number(edit.price) >= 0;

  return (
    <main className={`product-insights ${stateClass}`}>
      <header className="product-insights__hero">
        <div className="product-insights__photo">
          {data.identity.photo_url ? (
            <img alt={data.identity.name} src={data.identity.photo_url} />
          ) : <CubeIcon aria-label="Фото товара отсутствует" />}
        </div>
        <div className="product-insights__identity">
          <div className="product-insights__eyebrow">Операционный фокус</div>
          <h1>{data.identity.name}</h1>
          <div className="product-insights__identity-meta">
            <span>1С: {text(data.identity.nomenclature_code)}</span>
            <span>Bitrix24: {data.identity.bitrix_product_id}</span>
            {data.identity.article ? <span>Артикул: {data.identity.article}</span> : null}
            <span className="product-insights__status">
              {text(data.lifecycle.label || data.lifecycle.status, "Не определён")}
            </span>
            <span>Расчёт {text(data.source.calculated_at)}</span>
          </div>
        </div>
        {(relatedOrderUrl || data.identity.bitrix_url) ? (
          <nav aria-label="Действия по товару" className="product-insights__hero-actions">
            {!hasOrderContext && relatedOrderUrl ? (
              <a className="product-insights__primary-action" href={relatedOrderUrl} target="_top">
                {primaryBlocker ? "Разобрать блокер" : "Открыть заказ"}
              </a>
            ) : null}
            {data.identity.bitrix_url ? (
              <a className="product-insights__bitrix-link" href={resolveBitrixPortalUrl(data.identity.bitrix_url)} target="_top">
                Открыть карточку Bitrix24 <ArrowTopRightOnSquareIcon aria-hidden="true" />
              </a>
            ) : null}
          </nav>
        ) : null}
      </header>

      {hasOrderContext ? (
        <section className="product-insights__order-context" aria-labelledby="order-context-title">
          <div className="product-insights__order-title">
            <span><ShoppingCartIcon aria-hidden="true" /></span>
            <div>
              <h2 id="order-context-title">В этом заказе</h2>
              {order && orderLine ? <strong>Заказ №{order.id} · строка {orderLine.line_number}</strong> : null}
            </div>
          </div>
          {orderError ? <p className="product-insights__context-error">{orderError}</p> : null}
          {!orderError && !orderLine ? <p className="product-insights__context-error">Строка заказа не найдена.</p> : null}
          {order && orderLine ? (
            <>
              <label>Количество<span><input aria-label="Количество в заказе" disabled={saving || locked || orderLine.removed} min="0" onChange={(event) => setEdit((current) => ({ ...current, quantity: event.target.value }))} step="1" type="number" value={edit.quantity} /> шт.</span></label>
              <label>Цена закупки<span><input aria-label="Цена закупки" disabled={saving || locked || orderLine.removed} min="0" onChange={(event) => setEdit((current) => ({ ...current, price: event.target.value }))} step="0.01" type="number" value={edit.price} /> {orderLine.currency === "RUB" ? "₽" : orderLine.currency}</span></label>
              <div className="product-insights__supplier"><span>Поставщик</span><strong>{text(order.supplier_name || data.supply.supplier_name)}</strong></div>
              <button className="product-insights__confirm" disabled={saving || locked || orderLine.removed || !editIsValid} onClick={() => void saveLine(orderLine)} type="button">
                {saving ? "Сохраняем…" : locked ? "Заказ уже подтверждён" : "Подтвердить строку"}
              </button>
              {saveMessage ? (
                <p className={`product-insights__save-message ${saveMessage === "Строка подтверждена и сохранена" ? "is-success" : "is-error"}`}>
                  {saveMessage === "Строка подтверждена и сохранена" ? <CheckCircleIcon aria-hidden="true" /> : <ExclamationTriangleIcon aria-hidden="true" />}
                  {saveMessage}
                </p>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}

      <section className="product-insights__decision-grid" aria-label="Решение по товару">
        <article className="product-insights__recommendation">
          <span>Рекомендовано заказать</span>
          <strong>{number(data.demand.recommended_order, " шт.")}</strong>
          <details className="product-insights__recommendation-details">
            <summary title={recommendationText}>
              <span>{recommendationText}</span>
              <b>Подробнее</b>
            </summary>
            <p>{recommendationText}</p>
          </details>
        </article>
        <article className={`product-insights__blocker-summary ${primaryBlocker ? "has-blocker" : "is-clear"}`}>
          {primaryBlocker ? <ExclamationTriangleIcon aria-hidden="true" /> : <CheckCircleIcon aria-hidden="true" />}
          <div>
            <span>{primaryBlocker ? "Главный блокер" : "Блокеров нет"}</span>
            <strong title={primaryBlocker?.message}>{primaryBlocker?.message || "Товар готов к решению"}</strong>
            {primaryBlocker ? <small>{procurementRiskLabel(primaryBlocker.code)}</small> : null}
            {primaryBlocker && data.orders[0] ? (
              <a
                className="product-insights__blocker-action"
                href={relatedOrderUrl || relatedOrder.app_url}
                target="_top"
              >
                {primaryBlocker.resolution_actions?.[0]?.label || "Проверить блокер"}
              </a>
            ) : null}
          </div>
        </article>
        <article className={`product-insights__signal is-${metricTone(data.supply.profitability_pct)}`}>
          <span>Рентабельность</span><strong>{number(data.supply.profitability_pct, "%")}</strong><small>По текущей цене</small>
        </article>
        <article className={`product-insights__signal is-${metricTone(data.quality.defect_pct, true)}`}>
          <span>Брак</span><strong>{number(data.quality.defect_pct, "%")}</strong><small>Надёжность: {dictionaryLabel(CONFIDENCE_LABELS, data.quality.confidence).toLowerCase()}</small>
        </article>
      </section>

      <div className="product-insights__analytics-grid">
        <section className="product-insights__panel product-insights__demand">
          <div className="product-insights__panel-heading"><div><span>Спрос</span><h2>Скорость продаж</h2></div><span className={sourceClass}>{sourceState === "ready" ? "Данные актуальны" : "Проверьте данные"}</span></div>
          <div className="product-insights__chart" aria-label="Скорость продаж по периодам">
            {chart.map((item) => <div key={item.key}><strong>{number(item.value, " шт./день")}</strong><i><b style={{ height: `${item.height}%` }} /></i><span>{item.label}</span></div>)}
          </div>
          <div className="product-insights__demand-totals"><span>Продано за 30 дней <strong>{number(data.demand.sales_30, " шт.")}</strong></span><span>Целевой запас <strong>{number(data.demand.target_stock, " шт.")}</strong></span></div>
        </section>
        <section className="product-insights__panel product-insights__inventory">
          <div className="product-insights__panel-heading"><div><span>Наличие</span><h2>Остатки и поступления</h2></div></div>
          <div className="product-insights__inventory-list">
            {inventoryRows(data).map((item) => {
              const Icon = item.icon;
              return <article key={item.label}><span className={`is-${item.tone}`}><Icon aria-hidden="true" /></span><div><strong>{item.label}</strong><small>{item.hint}</small></div><b>{item.value}</b></article>;
            })}
          </div>
        </section>
      </div>

      <div className="product-insights__details-grid">
        <section className="product-insights__panel">
          <div className="product-insights__panel-heading"><div><span>Поставка</span><h2>Условия закупки</h2></div></div>
          <dl className="product-insights__facts"><div><dt>Поставщик</dt><dd>{text(data.supply.supplier_name)}</dd></div><div><dt>Закупочная цена</dt><dd>{price(data.supply.purchase_price, data.supply.currency)}</dd></div><div><dt>Срок поставки</dt><dd>{number(data.supply.lead_time_days, " дн.")}</dd></div><div><dt>Товарная семья</dt><dd>{text(data.family.label)}</dd></div></dl>
        </section>
        <section className="product-insights__panel">
          <div className="product-insights__panel-heading"><div><span>Контекст</span><h2>Связанные заказы</h2></div></div>
          {data.orders.length ? (
            <div className="product-insights__orders">
              {data.orders.map((relatedOrder) => <article key={relatedOrder.order_id}><div><strong>{relatedOrder.label}</strong><span>{dictionaryLabel(ORDER_STATUS_LABELS, relatedOrder.status)} · 1С: {dictionaryLabel(ONEC_STATUS_LABELS, relatedOrder.onec_status)}</span></div><nav><a href={relatedOrder.app_url} target="_top">Открыть заказ</a>{relatedOrder.bitrix_process_url ? <a href={resolveBitrixPortalUrl(relatedOrder.bitrix_process_url)} target="_top">Процесс Bitrix24</a> : null}</nav></article>)}
            </div>
          ) : <p className="product-insights__empty">Связанных заказов пока нет.</p>}
        </section>
      </div>
    </main>
  );
}
