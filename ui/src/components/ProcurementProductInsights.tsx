import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchProcurementProductCard,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { resolveBitrixPortalUrl } from "../api/bitrix";
import { procurementErrorText } from "../utils/procurementErrorMessages";
import { procurementRiskLabel } from "../utils/procurementRiskLabels";

interface Props {
  productId: string;
}

const DEMAND_WINDOWS = [
  { key: "rate_180", label: "180 дней" },
  { key: "rate_90", label: "90 дней" },
  { key: "rate_30", label: "30 дней" },
] as const;

const CONFIDENCE_LABELS: Record<string, string> = {
  high: "высокая",
  medium: "средняя",
  low: "низкая",
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

const EVIDENCE_LABELS: Record<string, string> = {
  return_qty: "Возвратов",
  share_pct: "Доля возвратов, %",
  return_share_pct: "Доля возвратов, %",
  minimum_return_qty: "Порог возвратов",
  minimum_share_pct: "Порог доли, %",
  window_days: "Период, дней",
  suspected_batch: "Предполагаемая партия",
  defect_pct: "Брак поставщика, %",
  history_units: "База продаж, шт.",
  minimum_defect_pct: "Порог брака, %",
  minimum_history_units: "Минимальная база, шт.",
  price_change_pct: "Изменение цены, %",
  maximum_change_pct: "Допустимое изменение, %",
  history_count: "Закупок в истории",
};

function text(value: unknown, fallback = "нет данных") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function number(value: unknown, suffix = "") {
  if (value === null || value === undefined || value === "") return "нет данных";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 3 })}${suffix}`;
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="product-insights__metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function price(value: unknown, currency: unknown) {
  const amount = number(value);
  if (amount === "нет данных") return amount;
  return [amount, text(currency, "")].filter(Boolean).join(" ");
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

function evidenceLabel(key: string) {
  return EVIDENCE_LABELS[key] || procurementRiskLabel(key);
}

function evidenceValue(key: string, value: unknown) {
  if (key.endsWith("_pct") || key === "share_pct") return number(value, "%");
  if (key === "window_days") return number(value, " дн.");
  return text(value);
}

function blockerCountLabel(count: number) {
  const remainder100 = count % 100;
  const remainder10 = count % 10;
  if (remainder100 >= 11 && remainder100 <= 14) return `${count} блокеров`;
  if (remainder10 === 1) return `${count} блокер`;
  if (remainder10 >= 2 && remainder10 <= 4) return `${count} блокера`;
  return `${count} блокеров`;
}

export function ProcurementProductInsights({ productId }: Props) {
  const [data, setData] = useState<ProcurementProductCard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await fetchProcurementProductCard(productId));
    } catch (requestError) {
      setError(procurementErrorText(requestError));
    } finally {
      setLoading(false);
    }
  }, [productId]);

  useEffect(() => {
    if (!productId) {
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    fetchProcurementProductCard(productId)
      .then((response) => { if (!cancelled) setData(response); })
      .catch((requestError) => { if (!cancelled) setError(procurementErrorText(requestError)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [productId]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = data?.identity.name
      ? `${data.identity.name} — показатели товара`
      : "Показатели товара";
    return () => { document.title = previousTitle; };
  }, [data?.identity.name]);

  const chart = useMemo(() => {
    if (!data) return [];
    const values = DEMAND_WINDOWS.map((item) => ({
      ...item,
      value: data.demand[item.key],
      numericValue: finiteNumber(data.demand[item.key]),
    }));
    const max = Math.max(
      ...values.map((item) => item.numericValue ?? 0),
      0
    );
    return values.map((item) => ({
      ...item,
      width: item.numericValue !== null && max > 0 ? (item.numericValue / max) * 100 : 0,
    }));
  }, [data]);

  if (!productId) {
    return <div className="product-insights product-insights--state">Bitrix24 не передал ID товара.</div>;
  }
  if (loading) {
    return <div className="product-insights product-insights--state">Загрузка показателей товара…</div>;
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
  const stateClass = data.blockers.length
    ? "is-blocked"
    : sourceClass;

  return (
    <main className={`product-insights ${stateClass}`}>
      <header className="product-insights__control">
        <div className="product-insights__control-heading">
          <h1>Контроль закупки</h1>
          <span>Расчёт: {text(data.source.calculated_at)}</span>
        </div>
        <div className="product-insights__badges">
          <span>
            Жизненный статус: {text(data.lifecycle.label || data.lifecycle.status, "не определён")}
          </span>
          <span className={sourceClass}>
            {sourceState === "ready" ? "Данные актуальны" : "Проверьте данные"}
          </span>
          {data.blockers.length ? (
            <span className="is-blocked">{blockerCountLabel(data.blockers.length)}</span>
          ) : null}
        </div>
        <div className="product-insights__decision">
          <div>
            <span>Рекомендовано заказать</span>
            <strong>{number(data.demand.recommended_order, " шт.")}</strong>
          </div>
          <p>{text(data.recommendation, "Рекомендация пока не рассчитана")}</p>
        </div>
      </header>

      {data.blockers.length ? (
        <section className="product-insights__section product-insights__section--blocked">
          <h2>Требует внимания</h2>
          <div className="product-insights__blockers">
            {data.blockers.map((blocker) => (
              <article key={`${blocker.code}-${blocker.line_id || "product"}`}>
                <strong>{blocker.message || procurementRiskLabel(blocker.code)}</strong>
                <small>{procurementRiskLabel(blocker.code)}</small>
                {Object.keys(blocker.evidence || {}).length ? (
                  <dl>
                    {Object.entries(blocker.evidence).map(([key, value]) => (
                      <div key={key}>
                        <dt>{evidenceLabel(key)}</dt>
                        <dd>{evidenceValue(key, value)}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                {blocker.resolution_actions?.length ? (
                  <p>
                    Действие: {blocker.resolution_actions.map((item) => item.label).join(" · ")}
                  </p>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <section className="product-insights__section">
        <h2>Спрос и потребность</h2>
        <div className="product-insights__metrics">
          <Metric label="Продажи 30 дней" value={number(data.demand.sales_30, " шт.")} />
          <Metric label="Продажи 90 дней" value={number(data.demand.sales_90, " шт.")} />
          <Metric label="Продажи 180 дней" value={number(data.demand.sales_180, " шт.")} />
          <Metric label="Заказы покупателей" value={number(data.demand.customer_orders, " шт.")} />
          <Metric label="В пути" value={number(data.demand.incoming, " шт.")} />
          <Metric label="Целевой запас" value={number(data.demand.target_stock, " шт.")} />
          <Metric label="В текущем заказе" value={number(data.demand.current_order, " шт.")} />
        </div>
        <div className="product-insights__chart" aria-label="Скорость продаж по окнам">
          {chart.map((item) => (
            <div key={item.key}>
              <span>{item.label}</span>
              <i><b style={{ width: `${item.width}%` }} /></i>
              <strong>{number(item.value, " шт./день")}</strong>
            </div>
          ))}
        </div>
      </section>

      <div className="product-insights__columns">
        <section className="product-insights__section">
          <h2>Качество</h2>
          <div className="product-insights__metrics">
            <Metric label="Возвраты 180 дней" value={number(data.quality.return_qty_180, " шт.")} />
            <Metric label="Возвраты «Новый» 90" value={number(data.quality.batch_return_qty_90, " шт.")} />
            <Metric label="Подтверждённый брак" value={number(data.quality.defect_pct, "%")} />
            <Metric
              label="Надёжность"
              value={dictionaryLabel(CONFIDENCE_LABELS, data.quality.confidence)}
            />
          </div>
        </section>
        <section className="product-insights__section">
          <h2>Поставка</h2>
          <div className="product-insights__metrics">
            <Metric label="Поставщик" value={text(data.supply.supplier_name)} />
            <Metric label="Закупочная цена" value={price(data.supply.purchase_price, data.supply.currency)} />
            <Metric label="Рентабельность" value={number(data.supply.profitability_pct, "%")} />
            <Metric label="Срок поставки" value={number(data.supply.lead_time_days, " дн.")} />
          </div>
        </section>
      </div>

      <section className="product-insights__section">
        <h2>Товарная семья</h2>
        <div className="product-insights__metrics product-insights__metrics--compact">
          <Metric label="Семья" value={text(data.family.label)} />
          <Metric label="Карточек в семье" value={number(data.family.member_count)} />
        </div>
      </section>

      <section className="product-insights__section">
        <h2>Связанные заказы</h2>
        {data.orders.length ? (
          <div className="product-insights__orders">
            {data.orders.map((order) => (
              <article key={order.order_id}>
                <div>
                  <strong>{order.label}</strong>
                  <span>
                    {dictionaryLabel(ORDER_STATUS_LABELS, order.status)} · 1С:{" "}
                    {dictionaryLabel(ONEC_STATUS_LABELS, order.onec_status)}
                  </span>
                </div>
                <nav>
                  <a href={order.app_url} target="_top">Открыть проект</a>
                  {order.bitrix_process_url ? (
                    <a href={resolveBitrixPortalUrl(order.bitrix_process_url)} target="_top">
                      Бизнес-процесс
                    </a>
                  ) : null}
                </nav>
              </article>
            ))}
          </div>
        ) : (
          <p>Связанных заказов пока нет.</p>
        )}
      </section>
    </main>
  );
}
