import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchProcurementProductCard,
  fetchProcurementProductCardByCode,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { resolveBitrixPortalUrl } from "../api/bitrix";
import { procurementErrorText } from "../utils/procurementErrorMessages";
import { procurementRiskLabel } from "../utils/procurementRiskLabels";

interface Props {
  productId?: string;
  nomenclatureCode?: string;
  onBack?: () => void;
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

const LIFECYCLE_LABELS: Record<string, string> = {
  fruit: "Рассматриваем",
  newborn: "Заказали",
  newborn_need: "Добираем",
  new_item: "Завезли",
  sales_start: "Пошли продажи",
  sale: "Растим",
  working: "Поддерживаем",
  pension: "Допродаём",
  review: "Разбор",
};

const SOURCE_STATE_LABELS: Record<string, string> = {
  ready: "данные актуальны",
  stale: "данные устарели",
  partial: "данные загружены частично",
  missing: "данных нет",
};

const CURRENCY_LABELS: Record<string, string> = {
  RUB: "руб.",
  AED: "дирхам ОАЭ",
  CNY: "юань",
  USD: "доллар США",
  EUR: "евро",
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
  const currencyCode = text(currency, "");
  const currencyLabel = CURRENCY_LABELS[currencyCode]
    || (/[a-z]/i.test(currencyCode) ? `${currencyCode} (код валюты)` : currencyCode);
  return [amount, currencyLabel].filter(Boolean).join(" ");
}

function finiteNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function dictionaryLabel(labels: Record<string, string>, value: unknown) {
  const raw = text(value);
  return labels[raw] || (/[a-z]/i.test(raw) ? `${raw} (техническое значение)` : raw);
}

function lifecycleLabel(card: ProcurementProductCard) {
  const label = text(card.lifecycle.label, "");
  if (label) return label;
  const status = text(card.lifecycle.status, "не определён");
  return LIFECYCLE_LABELS[status]
    || (/[a-z]/i.test(status) ? `${status} (технический статус)` : status);
}

function supplierLabel(card: ProcurementProductCard) {
  const supplier = text(card.supply.supplier_name, "");
  if (supplier) return supplier;
  const need = finiteNumber(card.demand.recommended_order) || 0;
  return need > 0
    ? "не выбран — требуется перед заказом"
    : "не требуется до появления потребности";
}

function blockerCountLabel(count: number) {
  const remainder100 = count % 100;
  const remainder10 = count % 10;
  if (remainder100 >= 11 && remainder100 <= 14) return `${count} блокеров`;
  if (remainder10 === 1) return `${count} блокер`;
  if (remainder10 >= 2 && remainder10 <= 4) return `${count} блокера`;
  return `${count} блокеров`;
}

const COMPARISON_ROWS: Array<{
  section: string;
  label: string;
  value: (card: ProcurementProductCard) => string;
}> = [
  {
    section: "Решение по карточке",
    label: "Жизненный статус",
    value: lifecycleLabel,
  },
  {
    section: "Решение по карточке",
    label: "Блокеры",
    value: (card) => card.blockers.length ? blockerCountLabel(card.blockers.length) : "нет",
  },
  {
    section: "Решение по карточке",
    label: "Рекомендация",
    value: (card) => text(card.recommendation, "нет рекомендации"),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Продажи за 180 дней",
    value: (card) => number(card.demand.sales_180, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Продажи за 90 дней",
    value: (card) => number(card.demand.sales_90, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Продажи за 30 дней",
    value: (card) => number(card.demand.sales_30, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Скорость за 180 дней",
    value: (card) => number(card.demand.rate_180, " шт./день"),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Скорость за 90 дней",
    value: (card) => number(card.demand.rate_90, " шт./день"),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Скорость за 30 дней",
    value: (card) => number(card.demand.rate_30, " шт./день"),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Доступный остаток",
    value: (card) => number(card.demand.sellable_stock, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Заказы покупателей",
    value: (card) => number(card.demand.customer_orders, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Товар в пути",
    value: (card) => number(card.demand.incoming, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Целевой запас",
    value: (card) => number(card.demand.target_stock, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "Рекомендовано заказать",
    value: (card) => number(card.demand.recommended_order, " шт."),
  },
  {
    section: "Спрос и количество — отвечает Омар",
    label: "В текущем заказе",
    value: (card) => number(card.demand.current_order, " шт."),
  },
  {
    section: "Возвраты и качество — отвечает Сергей",
    label: "Возвраты за 180 дней",
    value: (card) => number(card.quality.return_qty_180, " шт."),
  },
  {
    section: "Возвраты и качество — отвечает Сергей",
    label: "Возвраты «Новый» за 90 дней",
    value: (card) => number(card.quality.batch_return_qty_90, " шт."),
  },
  {
    section: "Возвраты и качество — отвечает Сергей",
    label: "Подтверждённый брак",
    value: (card) => number(card.quality.defect_pct, "%"),
  },
  {
    section: "Возвраты и качество — отвечает Сергей",
    label: "Надёжность показателя",
    value: (card) => dictionaryLabel(CONFIDENCE_LABELS, card.quality.confidence),
  },
  {
    section: "Поставка",
    label: "Поставщик",
    value: supplierLabel,
  },
  {
    section: "Поставка",
    label: "Закупочная цена",
    value: (card) => price(card.supply.purchase_price, card.supply.currency),
  },
  {
    section: "Поставка",
    label: "Рентабельность",
    value: (card) => number(card.supply.profitability_pct, "%"),
  },
  {
    section: "Поставка",
    label: "Срок поставки",
    value: (card) => number(card.supply.lead_time_days, " дн."),
  },
];

function FamilyComparison({ data }: { data: ProcurementProductCard }) {
  const members = data.family.comparison_members?.length
    ? data.family.comparison_members
    : [{
        role: "primary",
        role_label: "Основная карточка",
        rank: 0,
        speed_score: 0,
        card: data,
      }];
  const hiddenCount = Number(data.family.hidden_member_count || 0);
  const rankingLabel = text(
    data.family.ranking_source_label,
    "скорость завершённых продаж за 30 и 90 дней",
  );
  return (
    <section className="product-insights__section product-insights__family-review">
      <div className="product-insights__family-heading">
        <div>
          <h2>Сравнение семьи: {text(data.family.label)}</h2>
          <p>Основная карточка выделена цветом. Кандидаты выбраны по показателю: {rankingLabel}.</p>
        </div>
        <span>
          Показано {members.length} из {number(data.family.total_member_count || members.length)}
          {hiddenCount > 0 ? ` · ещё ${hiddenCount} не вошли в четвёрку кандидатов` : ""}
        </span>
      </div>
      <div className="product-insights__comparison-wrap">
        <table aria-label="Сравнение основной карточки и кандидатов семьи" className="product-insights__comparison">
          <thead>
            <tr>
              <th>Показатель</th>
              {members.map((member, index) => (
                <th
                  className={member.role === "primary" ? "is-primary" : ""}
                  key={`${member.card.identity.nomenclature_code}-${index}`}
                >
                  <span>{member.role === "primary" ? "Основная карточка" : `Кандидат ${member.rank}`}</span>
                  <strong>{text(member.card.identity.name, "Без названия")}</strong>
                  <small>Код 1С: {text(member.card.identity.nomenclature_code)}</small>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {COMPARISON_ROWS.map((row, rowIndex) => {
              const showSection = rowIndex === 0
                || COMPARISON_ROWS[rowIndex - 1].section !== row.section;
              return [
                showSection ? (
                  <tr className="product-insights__comparison-section" key={`section-${row.section}`}>
                    <th colSpan={members.length + 1}>{row.section}</th>
                  </tr>
                ) : null,
                <tr key={`${row.section}-${row.label}`}>
                  <th>{row.label}</th>
                  {members.map((member, index) => (
                    <td
                      className={member.role === "primary" ? "is-primary" : ""}
                      key={`${member.card.identity.nomenclature_code}-${row.label}-${index}`}
                    >
                      {row.value(member.card)}
                    </td>
                  ))}
                </tr>,
              ];
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function ProcurementProductInsights({ productId, nomenclatureCode, onBack }: Props) {
  const [data, setData] = useState<ProcurementProductCard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(
        nomenclatureCode
          ? await fetchProcurementProductCardByCode(nomenclatureCode)
          : await fetchProcurementProductCard(productId || "")
      );
    } catch (requestError) {
      setError(procurementErrorText(requestError));
    } finally {
      setLoading(false);
    }
  }, [nomenclatureCode, productId]);

  useEffect(() => {
    if (!productId && !nomenclatureCode) {
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    const request = nomenclatureCode
      ? fetchProcurementProductCardByCode(nomenclatureCode)
      : fetchProcurementProductCard(productId || "");
    request
      .then((response) => { if (!cancelled) setData(response); })
      .catch((requestError) => { if (!cancelled) setError(procurementErrorText(requestError)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [nomenclatureCode, productId]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = data?.identity.name
      ? `${data.identity.name} — ${nomenclatureCode ? "разбор карточки" : "показатели товара"}`
      : nomenclatureCode ? "Разбор карточки" : "Показатели товара";
    return () => { document.title = previousTitle; };
  }, [data?.identity.name, nomenclatureCode]);

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

  if (!productId && !nomenclatureCode) {
    return <div className="product-insights product-insights--state">Bitrix24 не передал код товара.</div>;
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
      {onBack ? (
        <nav className="product-insights__review-nav" aria-label="Навигация разбора карточки">
          <button className="btn btn--ghost" onClick={onBack} type="button">
            ← Вернуться на Витрину
          </button>
          <span>Витрина → Разбор → {data.identity.nomenclature_code}</span>
        </nav>
      ) : null}
      <header className="product-insights__control">
        <div className="product-insights__control-heading">
          <h1>{nomenclatureCode ? "Разбор карточки и семьи" : "Контроль закупки"}</h1>
          <span>Расчёт: {text(data.source.calculated_at)}</span>
        </div>
        <div className="product-insights__badges">
          <span>
            Жизненный статус: {lifecycleLabel(data)}
          </span>
          <span className={sourceClass}>
            {SOURCE_STATE_LABELS[sourceState]
              || (/[a-z]/i.test(sourceState)
                ? `${sourceState} (техническое состояние)`
                : sourceState)}
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

      {nomenclatureCode ? <FamilyComparison data={data} /> : null}

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
                        <dt>{procurementRiskLabel(key)}</dt>
                        <dd>{text(value)}</dd>
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
