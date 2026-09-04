import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  fetchProcurementProductCardByCode,
  saveProcurementFamilyDistributionReview,
  saveProcurementFamilyQualityReview,
  type ProcurementFamilyReviewCard,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { resolveBitrixPortalUrl } from "../api/bitrix";
import { procurementErrorText } from "../utils/procurementErrorMessages";

interface Props {
  nomenclatureCode: string;
  onBack: () => void;
}

const SOURCE_LABELS: Record<string, string> = {
  ready: "данные актуальны",
  stale: "данные устарели",
  partial: "данные загружены частично",
  missing: "данных нет",
};

function display(value: unknown, suffix = "") {
  if (value === null || value === undefined || value === "") return "нет данных";
  const parsed = Number(value);
  if (Number.isFinite(parsed)) {
    return `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 3 })}${suffix}`;
  }
  return String(value);
}

function compactList(value: unknown) {
  if (!Array.isArray(value) || value.length === 0) return "нет данных";
  return value.map((item) => {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      const row = item as Record<string, unknown>;
      return [row.number || row.document_number, row.reason || row.supplier_name]
        .filter(Boolean).join(" — ") || JSON.stringify(item);
    }
    return String(item);
  }).join("; ");
}

const ROWS: Array<{
  group: string;
  label: string;
  value: (card: ProcurementProductCard) => string;
}> = [
  { group: "Карточка", label: "Код 1С", value: (card) => display(card.identity.nomenclature_code) },
  { group: "Карточка", label: "Жизненный статус", value: (card) => display(card.lifecycle.label || card.lifecycle.status) },
  { group: "Карточка", label: "День рождения", value: (card) => display(card.lifecycle.birthday) },
  { group: "Состояние строки", label: "Блокеры", value: (card) => card.blockers.length ? `${card.blockers.length} шт.` : "нет" },
  { group: "Спрос и расчёт", label: "Продажи 30 / 90 / 180", value: (card) => `${display(card.demand.sales_30)} / ${display(card.demand.sales_90)} / ${display(card.demand.sales_180)} шт.` },
  { group: "Спрос и расчёт", label: "Скорость 180 → 90 → 30", value: (card) => `${display(card.demand.rate_180)} → ${display(card.demand.rate_90)} → ${display(card.demand.rate_30)} шт./день` },
  { group: "Спрос и расчёт", label: "Остаток", value: (card) => display(card.demand.sellable_stock, " шт.") },
  { group: "Спрос и расчёт", label: "Заказы покупателей", value: (card) => display(card.demand.customer_orders, " шт.") },
  { group: "Спрос и расчёт", label: "В пути", value: (card) => display(card.demand.incoming, " шт.") },
  { group: "Спрос и расчёт", label: "Целевой запас", value: (card) => display(card.demand.target_stock, " шт.") },
  { group: "Спрос и расчёт", label: "В текущем заказе", value: (card) => display(card.demand.current_order, " шт.") },
  { group: "Спрос и расчёт", label: "Свежий расчёт", value: (card) => display(card.demand.recommended_order, " шт.") },
  { group: "Качество и возвраты", label: "Возвраты / документов 180", value: (card) => `${display(card.quality.return_qty_180, " шт.")} / ${display(card.quality.return_document_count_180)}` },
  { group: "Качество и возвраты", label: "«Новый» / документов 90", value: (card) => `${display(card.quality.new_quality_return_qty_90 ?? card.quality.batch_return_qty_90, " шт.")} / ${display(card.quality.new_quality_return_document_count_90)}` },
  { group: "Качество и возвраты", label: "Исключено: группа «Сайт»", value: (card) => `${display(card.quality.site_excluded_return_qty_90, " шт.")} / ${display(card.quality.site_excluded_return_document_count_90)} док.` },
  { group: "Качество и возвраты", label: "Причины возврата", value: (card) => compactList(card.quality.return_reasons_90) },
  { group: "Качество и возвраты", label: "Подтверждённый брак", value: (card) => display(card.quality.defect_pct, "%") },
  { group: "Качество и возвраты", label: "Диагностический сигнал", value: (card) => display(card.quality.diagnostic_signal_pct, "%") },
  { group: "Поставка", label: "Поставщик", value: (card) => display(card.supply.supplier_name) },
  { group: "Поставка", label: "Документы поступления", value: (card) => compactList(card.supply.receipt_documents) },
  { group: "Решение", label: "Подсказка", value: (card) => display(card.recommendation) },
];

function reviewMembers(data: ProcurementFamilyReviewCard) {
  return data.family.comparison_members?.length
    ? data.family.comparison_members
    : [{ role: "primary", role_label: "Основная карточка", rank: 0, speed_score: 0, card: data }];
}

export function ProcurementFamilyReview({ nomenclatureCode, onBack }: Props) {
  const [data, setData] = useState<ProcurementFamilyReviewCard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [mobileIndex, setMobileIndex] = useState(0);
  const [qualityResult, setQualityResult] = useState<"confirmed" | "false_positive" | "needs_data">("needs_data");
  const [rootCause, setRootCause] = useState("");
  const [documents, setDocuments] = useState("");
  const [qualityComment, setQualityComment] = useState("");
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [rationale, setRationale] = useState("");
  const [distributionComment, setDistributionComment] = useState("");
  const [saving, setSaving] = useState<"quality" | "distribution" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchProcurementProductCardByCode(nomenclatureCode);
      setData(response);
      setQuantities((current) => Object.fromEntries(
        (response.family.member_codes || [nomenclatureCode]).map((code) => [
          code,
          current[code] ?? Number(
            reviewMembers(response).find((item) => item.card.identity.nomenclature_code === code)
              ?.card.demand.recommended_order || 0
          ),
        ])
      ));
    } catch (requestError) {
      setError(procurementErrorText(requestError));
    } finally {
      setLoading(false);
    }
  }, [nomenclatureCode]);

  useEffect(() => { void load(); }, [load]);

  const members = useMemo(() => data ? reviewMembers(data) : [], [data]);
  const mobileMember = members[Math.min(mobileIndex, Math.max(0, members.length - 1))];

  const saveQuality = async () => {
    if (!data || !rootCause.trim()) return;
    setSaving("quality");
    try {
      const response = await saveProcurementFamilyQualityReview(nomenclatureCode, {
        facts_hash: data.facts_hash,
        registry_version_number: Number(data.family.registry_version_number),
        result: qualityResult,
        root_cause: rootCause.trim(),
        checked_documents: documents.split(/[;\n]/).map((item) => item.trim()).filter(Boolean),
        comment: qualityComment.trim(),
      });
      setData({ ...data, decisions: response.decisions });
      toast.success(response.idempotent ? "Решение уже было сохранено" : "Решение Сергея сохранено");
    } catch (requestError) {
      const message = procurementErrorText(requestError);
      toast.error(message);
      if (message.includes("изменил") || message.includes("устар")) void load();
    } finally {
      setSaving(null);
    }
  };

  const saveDistribution = async () => {
    if (!data || !rationale.trim()) return;
    setSaving("distribution");
    try {
      const response = await saveProcurementFamilyDistributionReview(nomenclatureCode, {
        facts_hash: data.facts_hash,
        registry_version_number: Number(data.family.registry_version_number),
        quantities,
        rationale: rationale.trim(),
        comment: distributionComment.trim(),
      });
      setData({ ...data, decisions: response.decisions });
      toast.success(response.idempotent ? "Решение уже было сохранено" : "Решение Омара сохранено");
    } catch (requestError) {
      const message = procurementErrorText(requestError);
      toast.error(message);
      if (message.includes("изменил") || message.includes("устар")) void load();
    } finally {
      setSaving(null);
    }
  };

  if (loading) return <main className="family-review family-review--state">Загрузка семейного разбора…</main>;
  if (error || !data) return (
    <main className="family-review family-review--state">
      <strong>Не удалось открыть разбор</strong><span>{error || "Данные не найдены"}</span>
      <button className="btn" onClick={() => void load()} type="button">Повторить</button>
      <button className="btn btn--ghost" onClick={onBack} type="button">Вернуться на Витрину</button>
    </main>
  );

  const source = String(data.source.state || "missing");
  const orderUrl = data.orders[0]?.app_url;
  return (
    <main className="family-review">
      <nav className="family-review__nav" aria-label="Навигация разбора">
        <button className="btn btn--ghost" onClick={onBack} type="button">← Вернуться на Витрину</button>
        {orderUrl ? <a className="btn" href={orderUrl}>К заказу</a> : null}
      </nav>
      <header className="family-review__header">
        <div>
          <span>Разбор карточки и товарной семьи</span>
          <h1>{data.identity.name}</h1>
          <p>Код 1С: {data.identity.nomenclature_code} · семья: {display(data.family.label)}</p>
        </div>
        <div className="family-review__source">
          <strong>Срез: {display(data.source.calculated_at)}</strong>
          <span className={source === "ready" ? "is-ready" : "is-warning"}>{SOURCE_LABELS[source] || `${source} (состояние источника)`}</span>
          <small>Показано {members.length} из {data.family.total_member_count || members.length}{data.family.hidden_member_count ? ` · скрыто ${data.family.hidden_member_count}` : ""}</small>
        </div>
      </header>

      <section className="family-review__matrix" aria-label="Сравнение членов товарной семьи" tabIndex={0}>
        <table>
          <thead><tr><th>Показатель</th>{members.map((member) => (
            <th className={member.role === "primary" ? "is-primary" : ""} key={member.card.identity.nomenclature_code}>
              <span>{member.role === "primary" ? "Основная карточка" : `Кандидат ${member.rank}`}</span>
              <strong>{member.card.identity.name || "Без названия"}</strong>
              <small>{member.card.identity.nomenclature_code}</small>
            </th>
          ))}</tr></thead>
          <tbody>{ROWS.map((row, index) => {
            const section = index === 0 || ROWS[index - 1].group !== row.group;
            return [
              section ? <tr className="family-review__section" key={`section-${row.group}`}><th colSpan={members.length + 1}>{row.group}</th></tr> : null,
              <tr key={`${row.group}-${row.label}`}><th>{row.label}</th>{members.map((member) => <td className={member.role === "primary" ? "is-primary" : ""} key={`${member.card.identity.nomenclature_code}-${row.label}`}>{row.value(member.card)}</td>)}</tr>,
            ];
          })}</tbody>
        </table>
      </section>

      {mobileMember ? <section className="family-review__mobile-card">
        <div className="family-review__switcher">
          <button disabled={mobileIndex === 0} onClick={() => setMobileIndex((value) => value - 1)} type="button" aria-label="Предыдущий товар">←</button>
          <strong>{mobileIndex + 1} / {members.length}</strong>
          <button disabled={mobileIndex >= members.length - 1} onClick={() => setMobileIndex((value) => value + 1)} type="button" aria-label="Следующий товар">→</button>
        </div>
        <h2>{mobileMember.card.identity.name}</h2><p>{mobileMember.role_label} · {mobileMember.card.identity.nomenclature_code}</p>
        <dl>{ROWS.map((row) => <div key={`${row.group}-${row.label}`}><dt>{row.label}</dt><dd>{row.value(mobileMember.card)}</dd></div>)}</dl>
      </section> : null}

      <section className="family-review__decisions">
        <article>
          <header><div><span>Сергей · качество и возвраты</span><h2>Решение по качеству</h2></div>{data.decisions.quality ? <b className="is-ready">Сохранено</b> : <b>Нужно решение</b>}</header>
          <label>Результат<select value={qualityResult} onChange={(event) => setQualityResult(event.target.value as typeof qualityResult)}><option value="confirmed">Блокер подтверждён</option><option value="false_positive">Ложный блокер</option><option value="needs_data">Нужны данные</option></select></label>
          <label>Корневая причина<textarea value={rootCause} onChange={(event) => setRootCause(event.target.value)} required /></label>
          <label>Проверенные документы<textarea placeholder="Номера через точку с запятой или с новой строки" value={documents} onChange={(event) => setDocuments(event.target.value)} /></label>
          <label>Комментарий<textarea value={qualityComment} onChange={(event) => setQualityComment(event.target.value)} /></label>
          <button className="btn" disabled={saving !== null || !rootCause.trim()} onClick={() => void saveQuality()} type="button">{saving === "quality" ? "Сохраняем…" : "Сохранить решение Сергея"}</button>
        </article>
        <article>
          <header><div><span>Омар · семья и количество</span><h2>Распределение количества</h2></div>{data.decisions.distribution ? <b className="is-ready">Сохранено</b> : <b>Нужно решение</b>}</header>
          <div className="family-review__quantities">{(data.family.member_codes || []).map((code) => <label key={code}><span>{code}</span><input min="0" step="1" type="number" value={quantities[code] ?? 0} onChange={(event) => setQuantities((current) => ({ ...current, [code]: Math.max(0, Math.trunc(Number(event.target.value) || 0)) }))} /></label>)}</div>
          <label>Обоснование<textarea value={rationale} onChange={(event) => setRationale(event.target.value)} required /></label>
          <label>Комментарий<textarea value={distributionComment} onChange={(event) => setDistributionComment(event.target.value)} /></label>
          <button className="btn" disabled={saving !== null || !rationale.trim()} onClick={() => void saveDistribution()} type="button">{saving === "distribution" ? "Сохраняем…" : "Сохранить решение Омара"}</button>
        </article>
      </section>

      <footer className={`family-review__readiness ${data.decisions.blocker_ready ? "is-ready" : ""}`}>
        <strong>{data.decisions.blocker_ready ? "Разбор завершён" : "Блокер пока не закрыт"}</strong>
        <span>{data.decisions.blocker_ready ? "Оба актуальных решения сохранены." : "Нужны актуальные решения по качеству и количеству."}</span>
        {data.identity.bitrix_url ? <a href={resolveBitrixPortalUrl(data.identity.bitrix_url)} target="_top">Оригинальная карточка Bitrix24</a> : null}
      </footer>
    </main>
  );
}
