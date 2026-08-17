import { useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";

import {
  fetchCptCaseDetail,
  fetchCptCases,
  fetchCptDataIssues,
  fetchCptPortfolio,
  fetchCptQualityMetrics,
  fetchCptQualitySampleDetail,
  fetchCptQualitySamples,
  fetchCptSummary,
  fetchCptWorklists,
  prepareCptQualitySamples,
  reviewCptQualitySample,
  searchCptProfiles,
  type CptQualityGroup,
  type CptQualitySample,
  type CptPortfolioBucket,
  type CptWorklist,
} from "../api/customerPriceTypes";
import {
  eventLabel,
  factorLabel,
  reasonLabel,
  recommendationLabel,
  roleLabel,
  snapshotMonthLabel,
  statusLabel,
} from "./customerPriceTypeLabels";
import { CustomerPriceTypeEvidence } from "./CustomerPriceTypeEvidence";

interface CustomerPriceTypesWorkspaceProps {
  bitrixMode?: boolean;
  bitrixUserName?: string | null;
  role?: string;
  canViewMoney?: boolean;
}

const WORKLIST_ORDER: CptWorklist[] = [
  "manager_work",
  "isolate",
  "recovery",
  "data_check",
  "special_review",
  "downgrade_approval",
];

const WORKLIST_LABELS: Record<string, string> = {
  manager_work: "Удержание / дожим",
  isolate: "Изолятор",
  recovery: "Реанимация спящих",
  data_check: "Проблемы данных",
  special_review: "Спецпроверка",
  downgrade_approval: "Согласование понижения",
};

const WORKLIST_HINTS: Record<CptWorklist, string> = {
  manager_work:
    "Выручка за три месяца ниже нормы, но в последнем месяце клиент достиг порога удержания. Нужно восстановить объём продаж до нормы.",
  isolate:
    "Выручка за три месяца и за последний месяц ниже порога. Клиент проходит полный месяц изолятора перед дальнейшим решением.",
  recovery:
    "Покупок нет три месяца или дольше, но история работы с клиентом есть. Нужна попытка вернуть клиента.",
  data_check:
    "Внутренняя техническая очередь: не указан тип цены, в договорах разные уровни или данные источников расходятся. Тип цены не изменяется.",
  special_review:
    "Нужна отдельная проверка качества, кредита, экономики или истории клиента.",
  downgrade_approval:
    "Изолятор завершён и есть основания понизить ценовой уровень. Требуется решение руководителя.",
};

const QUALITY_GROUP_LABELS: Record<CptQualityGroup, string> = {
  ...WORKLIST_LABELS,
  no_action: "Действий не требуется",
} as Record<CptQualityGroup, string>;

const QUALITY_GROUPS = Object.keys(QUALITY_GROUP_LABELS) as CptQualityGroup[];

const LEVEL_LABELS: Record<string, string> = {
  retail: "Розница",
  bronze: "Бронза",
  silver: "Серебро",
  gold: "Золото",
  platinum: "Платина",
  unknown: "Не распознан",
};

const REVIEW_STATUS_LABELS: Record<string, string> = {
  ready: "Данные готовы",
  business_conflict: "Бизнес-конфликт договоров",
  technical_incomplete: "Неполные технические данные",
  missing_snapshot: "Нет расчёта",
};

const shell: CSSProperties = {
  display: "grid",
  gap: 16,
  padding: 20,
  maxWidth: 1200,
  margin: "0 auto",
  color: "var(--color-text, #1a1a1a)",
  fontFamily: "inherit",
};
const card: CSSProperties = {
  border: "1px solid var(--color-border, #e2e2e2)",
  borderRadius: 10,
  background: "var(--color-surface, #fff)",
  padding: 16,
};
const th: CSSProperties = {
  textAlign: "left",
  padding: "8px 10px",
  borderBottom: "2px solid var(--color-border, #e2e2e2)",
  fontSize: 12,
  color: "var(--color-text-muted, #667085)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};
const td: CSSProperties = {
  padding: "8px 10px",
  borderBottom: "1px solid var(--color-border, #eee)",
  fontSize: 14,
};

function money(value: string | null | undefined): string {
  if (value == null) return "—";
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return num.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
}

function levelLabel(level: string | null): string {
  if (!level) return "—";
  return LEVEL_LABELS[level] ?? "Не распознан";
}

export function CustomerPriceTypesWorkspace({
  bitrixUserName,
  role,
  canViewMoney,
}: CustomerPriceTypesWorkspaceProps) {
  const [worklist, setWorklist] = useState<CptWorklist | null>(null);
  const [search, setSearch] = useState("");
  const [portfolioSearch, setPortfolioSearch] = useState("");
  const [portfolioBucket, setPortfolioBucket] =
    useState<Exclude<CptPortfolioBucket, "all">>("working_bronze");
  const [selectedCaseId, setSelectedCaseId] = useState<number | null>(null);
  const [section, setSection] = useState<"portfolio" | "quality">("portfolio");

  const summaryQuery = useQuery({
    queryKey: ["cpt", "summary"],
    queryFn: () => fetchCptSummary(),
  });
  const worklistsQuery = useQuery({
    queryKey: ["cpt", "worklists"],
    queryFn: () => fetchCptWorklists(),
  });
  const portfolioQuery = useQuery({
    queryKey: ["cpt", "portfolio", portfolioBucket, portfolioSearch],
    queryFn: () =>
      fetchCptPortfolio({
        bucket: portfolioBucket,
        search: portfolioSearch.trim() || null,
        limit: 100,
      }),
  });
  const casesQuery = useQuery({
    queryKey: ["cpt", "cases", worklist, search],
    queryFn: () => fetchCptCases({ worklist, search: search.trim() || null, limit: 50 }),
  });
  const caseDetailQuery = useQuery({
    queryKey: ["cpt", "case", selectedCaseId],
    queryFn: () => fetchCptCaseDetail(selectedCaseId as number),
    enabled: selectedCaseId != null,
  });

  const summary = summaryQuery.data?.summary;
  const worklists = worklistsQuery.data?.worklists ?? {};
  const month = summaryQuery.data?.snapshot_month ?? worklistsQuery.data?.snapshot_month ?? null;
  const detail = caseDetailQuery.data;
  const canViewQuality =
    role === "internal" || role === "executive" || role === "network_head" || role === "quality";
  const canViewTechnicalWorkspace = role === "internal" || role === "executive" || role === "quality";
  const visibleWorklists = role === "network_head"
    ? WORKLIST_ORDER.filter((item) => item !== "data_check")
    : WORKLIST_ORDER;

  if (section === "quality" && canViewQuality) {
    return (
      <div style={shell}>
        <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div>
            <p style={{ margin: 0, color: "var(--color-primary, #2563eb)", fontWeight: 800, fontSize: 12, letterSpacing: "0.08em", textTransform: "uppercase" }}>
              Управление типами цен
            </p>
            <h1 style={{ margin: "4px 0 0", fontSize: 24 }}>Экспертная оценка</h1>
          </div>
          <button type="button" onClick={() => setSection("portfolio")} style={secondaryButton}>
            Вернуться к портфелю
          </button>
        </header>
        <QualityModule role={role} />
      </div>
    );
  }

  return (
    <div style={shell}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "end", gap: 16 }}>
        <div>
          <p style={{ margin: 0, color: "var(--color-primary, #2563eb)", fontWeight: 800, fontSize: 12, letterSpacing: "0.08em", textTransform: "uppercase" }}>
            Управление типами цен
          </p>
          <h1 style={{ margin: "4px 0 0", fontSize: 24 }}>Портфель клиентов</h1>
          <p style={{ margin: "6px 0 0", color: "var(--color-text-muted, #667085)" }}>
            {month
              ? `Расчёт за ${snapshotMonthLabel(month)}. Только просмотр — тип цены меняет человек.`
              : "Актуальный расчёт не загружен. Тип цены в этом разделе не изменяется."}
          </p>
        </div>
        <div style={{ textAlign: "right", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
          {bitrixUserName && <div>{bitrixUserName}</div>}
          {role && <div>Роль: {roleLabel(role)}{canViewMoney ? " · суммы доступны" : ""}</div>}
          {canViewQuality && (
            <button type="button" onClick={() => setSection("quality")} style={{ ...primaryActionButton, marginTop: 10 }}>
              Экспертная оценка →
            </button>
          )}
        </div>
      </header>

      {/* Summary tiles */}
      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Tile label="Клиентов" value={summary?.profile_count} loading={summaryQuery.isLoading} />
        <Tile label="В работу" value={summary?.actionable_count} loading={summaryQuery.isLoading} accent />
        {summary &&
          ["retail", "bronze", "silver", "gold", "platinum"].map((lvl) =>
            summary.levels[lvl] ? (
              <Tile key={lvl} label={levelLabel(lvl)} value={summary.levels[lvl]} />
            ) : null
          )}
      </section>

      {canViewTechnicalWorkspace && <section style={{ ...card, display: "grid", gap: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <div>
            <strong>Проверенный пакет 82</strong>
            <div style={{ marginTop: 4, color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
              Рабочий тип определяется только по фактическим реализациям договоров.
            </div>
          </div>
          <input
            type="search"
            placeholder="Поиск в пакете по коду или клиенту…"
            value={portfolioSearch}
            onChange={(event) => setPortfolioSearch(event.target.value)}
            style={{ minHeight: 36, minWidth: 280, padding: "0 12px", border: "1px solid var(--color-border, #e2e2e2)", borderRadius: 8, font: "inherit" }}
          />
        </div>

        {portfolioQuery.isError && (
          <p style={{ margin: 0, color: "var(--color-danger, #d92d20)" }}>
            Контрольный пакет пока не загружен или недоступен.
          </p>
        )}
        {portfolioQuery.data && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
              {([
                ["working_bronze", "Рабочий тип 2.Бронзовый"],
                ["review_queue", "Остальные на разбор"],
              ] as const).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setPortfolioBucket(key)}
                  style={{ ...card, textAlign: "left", cursor: "pointer", borderColor: portfolioBucket === key ? "var(--color-primary, #2563eb)" : "var(--color-border, #e2e2e2)", boxShadow: portfolioBucket === key ? "0 0 0 2px var(--color-primary, #2563eb)" : "none" }}
                >
                  <div style={{ fontSize: 22, fontWeight: 800 }}>
                    {(portfolioQuery.data.counts[key] ?? 0).toLocaleString("ru-RU")}
                  </div>
                  <div style={{ color: "var(--color-text-muted, #667085)", fontSize: 13 }}>{label}</div>
                </button>
              ))}
            </div>

            {portfolioQuery.data.mismatch_count > 0 && (
              <div role="alert" style={{ border: "1px solid #f0b429", background: "#fff8e1", borderRadius: 8, padding: 12 }}>
                Расчёт расходится с эталонным распределением: {portfolioQuery.data.mismatch_count}. Автоматической подмены результата нет.
                {(portfolioQuery.data.review_status_counts.technical_incomplete ?? 0) > 0 && (
                  <> Неполных технических данных: {portfolioQuery.data.review_status_counts.technical_incomplete}.</>
                )}
              </div>
            )}

            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 900 }}>
                <thead>
                  <tr>
                    <th style={th}>Код 1С</th>
                    <th style={th}>Клиент</th>
                    <th style={th}>Вычисленный тип</th>
                    <th style={th}>Рабочий договор</th>
                    <th style={th}>Операционная очередь</th>
                    <th style={th}>Сверка</th>
                  </tr>
                </thead>
                <tbody>
                  {portfolioQuery.data.payload.map((row) => (
                    <tr
                      key={row.counterparty_ref}
                      onClick={() => row.case_id != null && setSelectedCaseId(row.case_id)}
                      style={{ cursor: row.case_id != null ? "pointer" : "default" }}
                    >
                      <td style={td}>{row.counterparty_code}</td>
                      <td style={td}>
                        <div>{row.counterparty_name ?? "—"}</div>
                        <small style={{ color: "var(--color-text-muted, #667085)" }}>{row.department_name ?? "—"}</small>
                      </td>
                      <td style={td}>{row.current_price_type ?? "Ручная проверка"}</td>
                      <td style={td}>
                        {row.working_contracts.length > 0
                          ? row.working_contracts.map((contract) => (
                              <div key={contract.contract_ref ?? contract.contract_name}>
                                {contract.contract_name ?? "Без названия"} · {contract.price_type_name ?? "без типа"}
                                <small style={{ display: "block", color: "var(--color-text-muted, #667085)" }}>
                                  {contract.sale_document_count_12m ?? 0} реализаций · последняя {contract.last_sale_at ?? "—"}
                                </small>
                              </div>
                            ))
                          : "Нет однозначного рабочего договора"}
                      </td>
                      <td style={td}>{row.action_required && row.case_type ? WORKLIST_LABELS[row.case_type] ?? row.case_type : "Действий не требуется"}</td>
                      <td style={td}>
                        {row.reconciliation_status === "match" ? "Совпадает" : row.reconciliation_status === "missing_snapshot" ? "Нет расчёта" : "Расхождение"}
                        <small style={{ display: "block", color: "var(--color-text-muted, #667085)" }}>
                          {REVIEW_STATUS_LABELS[row.review_status] ?? row.review_status}
                        </small>
                      </td>
                    </tr>
                  ))}
                  {portfolioQuery.data.payload.length === 0 && (
                    <tr><td style={td} colSpan={6}>Карточек не найдено.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>}

      {/* Worklist queues */}
      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
        {visibleWorklists.map((key) => {
          const count = worklists[key] ?? 0;
          const active = worklist === key;
          return (
            <button
              key={key}
              type="button"
              title={WORKLIST_HINTS[key]}
              aria-label={`${WORKLIST_LABELS[key]}: ${count.toLocaleString("ru-RU")}. ${WORKLIST_HINTS[key]}`}
              onClick={() => setWorklist(active ? null : key)}
              style={{
                ...card,
                textAlign: "left",
                cursor: "pointer",
                borderColor: active ? "var(--color-primary, #2563eb)" : "var(--color-border, #e2e2e2)",
                boxShadow: active ? "0 0 0 2px var(--color-primary, #2563eb)" : "none",
                opacity: count === 0 ? 0.55 : 1,
              }}
            >
              <div style={{ fontSize: 22, fontWeight: 800 }}>{count.toLocaleString("ru-RU")}</div>
              <div style={{ color: "var(--color-text-muted, #667085)", fontSize: 13 }}>{WORKLIST_LABELS[key]}</div>
            </button>
          );
        })}
      </section>

      {/* Filters + cases */}
      <section style={{ ...card, display: "grid", gap: 12 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <strong>{worklist ? WORKLIST_LABELS[worklist] : "Все кейсы, требующие действий"}</strong>
          <input
            type="search"
            placeholder="Поиск по коду РБ, имени…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            style={{
              flex: "1 1 260px",
              minHeight: 36,
              padding: "0 12px",
              border: "1px solid var(--color-border, #e2e2e2)",
              borderRadius: 8,
              font: "inherit",
            }}
          />
          {worklist && (
            <button type="button" onClick={() => setWorklist(null)} style={{ minHeight: 36, padding: "0 12px", borderRadius: 8, border: "1px solid var(--color-border, #e2e2e2)", background: "transparent", cursor: "pointer" }}>
              Сбросить фильтр
            </button>
          )}
        </div>
        <p style={{ margin: 0, color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
          Правило расчёта: выручка одного контрагента суммируется в одну сумму по всем
          его договорам и вариантам типа цены. Несколько договоров одного ценового
          уровня считаются вместе; разные уровни отправляются на сверку данных.
        </p>

        {casesQuery.isLoading && <p>Загрузка…</p>}
        {casesQuery.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось загрузить кейсы.</p>}
        {casesQuery.data && (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 720 }}>
              <thead>
                <tr>
                  <th style={th}>Код 1С</th>
                  <th style={th}>Клиент</th>
                  <th style={th}>Очередь</th>
                  <th style={th}>Рекомендация</th>
                  <th style={th}>Статус</th>
                </tr>
              </thead>
              <tbody>
                {casesQuery.data.payload.map((row) => (
                  <tr
                    key={row.id}
                    onClick={() => setSelectedCaseId(row.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{row.counterparty_code ?? "—"}</td>
                    <td style={td}>{row.counterparty_name ?? "—"}</td>
                    <td style={td}>{WORKLIST_LABELS[row.case_type] ?? "Неизвестная очередь"}</td>
                    <td style={td}>{row.recommended_price_type ?? recommendationLabel(row.system_recommendation)}</td>
                    <td style={td}>{statusLabel(row.approval_status)}</td>
                  </tr>
                ))}
                {casesQuery.data.payload.length === 0 && (
                  <tr>
                    <td style={td} colSpan={5}>Кейсов не найдено.</td>
                  </tr>
                )}
              </tbody>
            </table>
            <p style={{ margin: "8px 0 0", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
              Показано {casesQuery.data.payload.length} из {casesQuery.data.total}.
            </p>
          </div>
        )}
      </section>

      {/* Case detail drawer */}
      {selectedCaseId != null && (
        <div
          role="dialog"
          aria-modal="true"
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.35)", display: "flex", justifyContent: "flex-end", zIndex: 50 }}
          onClick={() => setSelectedCaseId(null)}
        >
          <aside
            style={{ width: "min(480px, 100%)", height: "100%", overflowY: "auto", background: "var(--color-surface, #fff)", padding: 20, display: "grid", gap: 12, alignContent: "start" }}
            onClick={(event) => event.stopPropagation()}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
              <h2 style={{ margin: 0, fontSize: 18 }}>Карточка кейса</h2>
              <button type="button" onClick={() => setSelectedCaseId(null)} style={{ border: "none", background: "transparent", fontSize: 22, cursor: "pointer", lineHeight: 1 }}>×</button>
            </div>
            {caseDetailQuery.isLoading && <p>Загрузка…</p>}
            {detail && (
              <>
                <div style={card}>
                  <div style={{ fontWeight: 800 }}>{detail.case.counterparty_code ?? "—"} · {detail.case.counterparty_name ?? "—"}</div>
                  <Row k="Тип цены" v={detail.snapshot.current_price_type ?? "—"} />
                  <Row k="Рекомендация" v={recommendationLabel(detail.snapshot.system_recommendation)} />
                  <Row k="Цель" v={detail.snapshot.recommended_price_type ?? "—"} />
                  <Row k="Очередь" v={WORKLIST_LABELS[detail.case.case_type] ?? "Неизвестная очередь"} />
                  <Row k="Согласование" v={statusLabel(detail.case.approval_status)} />
                  {detail.snapshot.money_visible ? (
                    <>
                      <Row k="Оборот 3м" v={money(detail.snapshot.total_3m)} />
                      <Row k="Последний месяц" v={money(detail.snapshot.last_month)} />
                    </>
                  ) : (
                    <Row k="Суммы" v="скрыты по роли" />
                  )}
                </div>
                <div style={card}>
                  <div style={{ fontWeight: 700, marginBottom: 6 }}>Причины</div>
                  <div style={{ fontSize: 13, color: "var(--color-text-muted, #667085)" }}>
                    {reasonLabel(detail.snapshot.recommendation_reason)}
                  </div>
                  {detail.snapshot.stop_factors.length > 0 && (
                    <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {detail.snapshot.stop_factors.map((sf) => (
                        <span key={sf} style={{ fontSize: 11, padding: "2px 8px", borderRadius: 999, background: "var(--color-surface-muted, #f2f4f7)", color: "var(--color-text-muted, #667085)" }}>{factorLabel(sf)}</span>
                      ))}
                    </div>
                  )}
                </div>
                {detail.snapshot.contract_candidates.length > 0 && (
                  <div style={card}>
                    <div style={{ fontWeight: 700, marginBottom: 8 }}>Договоры и типы цен</div>
                    <div style={{ overflowX: "auto" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                        <thead>
                          <tr>
                            <th style={th}>Договор</th>
                            <th style={th}>Тип цены</th>
                            <th style={th}>Состояние</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.snapshot.contract_candidates.map((contract, index) => (
                            <tr key={contract.contract_ref ?? `${contract.contract_name}-${index}`}>
                              <td style={td}>{contract.contract_name ?? "Без названия"}</td>
                              <td style={td}>{contract.price_type_name ?? "Не задан"}</td>
                              <td style={td}>
                                {contract.price_type_change_target
                                  ? "Договор для изменения типа цены"
                                  : contract.used_for_calculation
                                    ? "Используется в расчёте"
                                    : contract.price_type_missing
                                      ? "Не участвует: тип не задан"
                                      : contract.price_type_marked
                                        ? "Не участвует: тип помечен на удаление"
                                        : "Не участвует: тип не распознан"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
                {detail.guidance && (
                  <div style={{ ...card, borderColor: "var(--color-warning, #f79009)", background: "var(--color-warning-subtle, #fffaeb)" }}>
                    <div style={{ fontWeight: 800, marginBottom: 10 }}>{detail.guidance.title}</div>
                    <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
                      <div>
                        <strong>По нашим правилам</strong>
                        <p style={{ margin: "4px 0 0" }}>{detail.guidance.rules}</p>
                      </div>
                      <div>
                        <strong>Рекомендуемое действие</strong>
                        <p style={{ margin: "4px 0 0" }}>{detail.guidance.recommended_action}</p>
                      </div>
                      <div>
                        <strong>Какой тип цены должен остаться</strong>
                        <p style={{ margin: "4px 0 0" }}>{detail.guidance.expected_price_type}</p>
                      </div>
                      <div>
                        <strong>На что обратить внимание менеджеру</strong>
                        <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                          {detail.guidance.manager_attention.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    </div>
                  </div>
                )}
                <div style={card}>
                  <div style={{ fontWeight: 700, marginBottom: 6 }}>События</div>
                  <ul style={{ margin: 0, paddingLeft: 16, fontSize: 13 }}>
                    {detail.events.map((event) => (
                      <li key={event.id}>
                        <span style={{ color: "var(--color-text-muted, #667085)" }}>{event.event_at.slice(0, 16).replace("T", " ")}</span>{" "}
                        {eventLabel(event.event_type)}
                        {event.comment ? ` — ${reasonLabel(event.comment)}` : ""}
                      </li>
                    ))}
                    {detail.events.length === 0 && <li>—</li>}
                  </ul>
                </div>
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

const secondaryButton: CSSProperties = {
  minHeight: 36,
  padding: "0 12px",
  borderRadius: 8,
  border: "1px solid var(--color-border, #d0d5dd)",
  background: "var(--color-surface, #fff)",
  color: "inherit",
  cursor: "pointer",
  font: "inherit",
};

const primaryActionButton: CSSProperties = {
  minHeight: 42,
  padding: "0 16px",
  borderRadius: 9,
  border: "1px solid var(--color-primary, #2563eb)",
  background: "var(--color-primary, #2563eb)",
  color: "#fff",
  cursor: "pointer",
  font: "inherit",
  fontWeight: 800,
  boxShadow: "0 4px 12px rgba(37, 99, 235, 0.3)",
};

function percent(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${(value * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function QualityModule({ role }: { role?: string }) {
  const queryClient = useQueryClient();
  const [perGroup, setPerGroup] = useState(30);
  const [statusFilter, setStatusFilter] = useState<"pending" | "reviewed" | null>("pending");
  const [groups, setGroups] = useState<Record<number, CptQualityGroup>>({});
  const [comments, setComments] = useState<Record<number, string>>({});
  const [reviewActions, setReviewActions] = useState<Record<number, "incorrect" | "data_issue" | null>>({});
  const [expandedSampleId, setExpandedSampleId] = useState<number | null>(null);
  const [globalSearch, setGlobalSearch] = useState("");
  const [dataIssueSearch, setDataIssueSearch] = useState("");
  const canPrepare = role === "internal" || role === "executive" || role === "network_head";
  const canViewDataIssues = role === "internal" || role === "executive" || role === "quality";

  const metricsQuery = useQuery({
    queryKey: ["cpt", "quality", "metrics"],
    queryFn: fetchCptQualityMetrics,
  });
  const samplesQuery = useQuery({
    queryKey: ["cpt", "quality", "samples", statusFilter],
    queryFn: () => fetchCptQualitySamples({ status: statusFilter }),
  });
  const profileSearchQuery = useQuery({
    queryKey: ["cpt", "profile-search", globalSearch],
    queryFn: () => searchCptProfiles(globalSearch.trim()),
    enabled: globalSearch.trim().length >= 2,
  });
  const dataIssuesQuery = useQuery({
    queryKey: ["cpt", "data-issues", dataIssueSearch],
    queryFn: () => fetchCptDataIssues(dataIssueSearch.trim() || null),
    enabled: canViewDataIssues,
  });
  const sampleDetailQuery = useQuery({
    queryKey: ["cpt", "quality", "sample", expandedSampleId],
    queryFn: () => fetchCptQualitySampleDetail(expandedSampleId as number),
    enabled: expandedSampleId != null,
  });
  const refreshQuality = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["cpt", "quality", "metrics"] }),
      queryClient.invalidateQueries({ queryKey: ["cpt", "quality", "samples"] }),
    ]);
  };
  const prepareMutation = useMutation({
    mutationFn: () => prepareCptQualitySamples(perGroup),
    onSuccess: refreshQuality,
  });
  const reviewMutation = useMutation({
    mutationFn: ({ sample, reviewResult, correctGroup, comment }: { sample: CptQualitySample; reviewResult: "correct" | "incorrect" | "data_issue"; correctGroup?: CptQualityGroup | null; comment?: string }) =>
      reviewCptQualitySample({
        sampleId: sample.id,
        reviewResult,
        correctGroup,
        comment,
        expectedVersion: sample.version,
      }),
    onSuccess: async (saved) => {
      await refreshQuality();
      await queryClient.invalidateQueries({ queryKey: ["cpt", "profile-search"] });
      await queryClient.invalidateQueries({ queryKey: ["cpt", "data-issues"] });
      const result = saved.review_result === "data_issue"
        ? "Ошибка в данных передана технической команде"
        : saved.review_result === "incorrect"
          ? "Исправленная оценка сохранена"
          : "Результат подтверждён";
      toast.success(`${result}. ${saved.reviewed_by ?? "Пользователь"}, ${new Date(saved.reviewed_at ?? Date.now()).toLocaleString("ru-RU")}`);
    },
    onError: () => toast.error("Не удалось сохранить оценку. Обновите выборку и повторите."),
  });

  const metrics = metricsQuery.data;
  const samples = samplesQuery.data?.payload ?? [];

  return (
    <>
      <section style={{ ...card, display: "grid", gap: 10 }}>
        <div>
          <strong>Поиск по всему портфелю</strong>
          <p style={{ margin: "4px 0 0", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
            Найдите клиента по названию, коду или идентификатору 1С. Оценка доступна только для назначенной контрольной выборки.
          </p>
        </div>
        <input
          type="search"
          aria-label="Поиск клиента по всему портфелю"
          placeholder="Введите название или код 1С…"
          value={globalSearch}
          onChange={(event) => setGlobalSearch(event.target.value)}
          style={{ minHeight: 40, padding: "0 12px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8, font: "inherit" }}
        />
        {globalSearch.trim().length === 1 && <small>Введите не менее двух символов.</small>}
        {profileSearchQuery.isLoading && <p>Поиск…</p>}
        {profileSearchQuery.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось выполнить поиск.</p>}
        {profileSearchQuery.data && (
          <div style={{ display: "grid", gap: 8 }}>
            {profileSearchQuery.data.payload.map((item) => (
              <div key={item.counterparty_ref} style={{ ...card, padding: 12, display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                <div>
                  <strong>{item.counterparty_code ?? "—"} · {item.counterparty_name ?? "—"}</strong>
                  <div style={{ marginTop: 3, fontSize: 13 }}>{item.result_label}</div>
                  <small style={{ color: "var(--color-text-muted, #667085)" }}>
                    Текущий тип: {item.current_price_type ?? "не определён"}
                    {item.result_state === "change_proposed" ? ` · Предлагается: ${item.recommended_price_type ?? "—"}` : ""}
                  </small>
                </div>
                {item.can_review ? (
                  <button type="button" onClick={() => setExpandedSampleId(item.quality_sample_id)} style={secondaryButton}>Перейти к оценке</button>
                ) : (
                  <span style={{ fontSize: 12, color: "var(--color-text-muted, #667085)" }}>
                    {item.quality_sample_status === "reviewed" ? "Уже проверено" : "Только просмотр"}
                  </span>
                )}
              </div>
            ))}
            {profileSearchQuery.data.payload.length === 0 && <p>Клиенты не найдены.</p>}
          </div>
        )}
      </section>

      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
        <Tile label="Клиентов в срезе" value={metrics?.population_count} loading={metricsQuery.isLoading} />
        <Tile label="Подготовлено" value={metrics?.selected_count} loading={metricsQuery.isLoading} />
        <Tile label="Проверено" value={metrics?.reviewed_count} loading={metricsQuery.isLoading} />
        <div style={card}>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{metrics ? percent(metrics.coverage) : "…"}</div>
          <div style={{ fontSize: 13, opacity: 0.85 }}>Покрытие выборки</div>
        </div>
        <div style={card}>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{metrics ? percent(metrics.override_rate) : "…"}</div>
          <div style={{ fontSize: 13, opacity: 0.85 }}>Исправлено экспертом</div>
        </div>
        <div style={{ ...card, borderColor: metrics?.critical_false_downgrade_count ? "var(--color-danger, #d92d20)" : "var(--color-border, #e2e2e2)" }}>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{metrics?.critical_false_downgrade_count ?? 0}</div>
          <div style={{ fontSize: 13, opacity: 0.85 }}>Ошибочных понижений</div>
        </div>
      </section>

      {metrics && !metrics.metrics_ready && metrics.selected_count > 0 && (
        <p style={{ ...card, margin: 0, borderColor: "var(--color-warning, #f79009)", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
          Метрики предварительные: итоговая оценка появится после проверки всей контрольной выборки.
        </p>
      )}

      {canPrepare && (
        <section style={{ ...card, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <div style={{ flex: "1 1 360px" }}>
            <strong>Контрольная выборка</strong>
            <p style={{ margin: "4px 0 0", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
              Система добавит указанное число клиентов из каждой очереди и из группы без действий. Повторный запуск не создаёт дубли.
            </p>
          </div>
          <label style={{ display: "grid", gap: 4, fontSize: 13 }}>
            На каждую группу
            <input type="number" min={1} max={200} value={perGroup} onChange={(event) => setPerGroup(Number(event.target.value))} style={{ width: 100, minHeight: 36, padding: "0 8px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8 }} />
          </label>
          <button type="button" disabled={prepareMutation.isPending || perGroup < 1 || perGroup > 200} onClick={() => prepareMutation.mutate()} style={secondaryButton}>
            {prepareMutation.isPending ? "Подготовка…" : "Подготовить выборку"}
          </button>
          {prepareMutation.isError && <span style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось подготовить выборку.</span>}
        </section>
      )}

      <section style={{ ...card, display: "grid", gap: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
          <strong>Оценки эксперта</strong>
          <select value={statusFilter ?? "all"} onChange={(event) => setStatusFilter(event.target.value === "all" ? null : event.target.value as "pending" | "reviewed")} style={{ minHeight: 36, padding: "0 10px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8, background: "var(--color-surface, #fff)" }}>
            <option value="pending">Ожидают проверки</option>
            <option value="reviewed">Проверены</option>
            <option value="all">Все</option>
          </select>
        </div>
        <p role="note" style={{ margin: 0, padding: 10, borderRadius: 8, background: "var(--color-surface-muted, #f8fafc)", fontSize: 13 }}>
          Оценка проверяет качество расчёта и не утверждает изменение типа цены. Никакие данные в 1С отсюда не изменяются.
        </p>
        {samplesQuery.isLoading && <p>Загрузка выборки…</p>}
        {samplesQuery.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось загрузить выборку.</p>}
        {!samplesQuery.isLoading && samples.length === 0 && (
          <p style={{ color: "var(--color-text-muted, #667085)" }}>
            {metrics?.selected_count ? "В этом разделе строк нет." : "Контрольная выборка ещё не подготовлена."}
          </p>
        )}
        {samples.map((sample) => {
          const selectedAction = reviewActions[sample.id] ?? null;
          const alternativeGroups = QUALITY_GROUPS.filter((group) => group !== "data_check" && group !== sample.system_group);
          const selectedGroup = groups[sample.id] ?? sample.correct_group ?? (selectedAction === "incorrect" ? alternativeGroups[0] : sample.system_group);
          const comment = comments[sample.id] ?? sample.comment ?? "";
          const needsComment = selectedAction === "incorrect" || selectedAction === "data_issue";
          const canSubmitAlternative = Boolean(
            selectedAction
            && (!needsComment || comment.trim())
            && (selectedAction !== "incorrect" || (selectedGroup !== sample.system_group && selectedGroup !== "data_check"))
          );
          return (
            <article key={sample.id} style={{ borderTop: "1px solid var(--color-border, #eaecf0)", paddingTop: 14, display: "grid", gap: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                <div>
                  <strong>{sample.counterparty_code ?? "—"} · {sample.counterparty_name ?? "—"}</strong>
                  <div style={{ color: "var(--color-text-muted, #667085)", fontSize: 13, marginTop: 3 }}>
                    Решение системы: {QUALITY_GROUP_LABELS[sample.system_group]}
                  </div>
                </div>
                <span style={{ fontSize: 12, color: "var(--color-text-muted, #667085)" }}>
                  {sample.status === "reviewed" ? "Проверено" : "Ожидает проверки"}
                </span>
              </div>
              <div style={{ fontSize: 13 }}>
                <strong>{sample.system_recommendation === "data_check" ? "Рекомендация не сформирована" : recommendationLabel(sample.system_recommendation)}</strong>
                <div style={{ marginTop: 3 }}>
                  Текущий тип: <strong>{sample.current_price_type ?? "—"}</strong>
                  {sample.system_recommendation !== "data_check" && <> · Рекомендуемый: <strong>{sample.recommended_price_type ?? "—"}</strong></>}
                </div>
                <div style={{ marginTop: 3, color: "var(--color-text-muted, #667085)" }}>{reasonLabel(sample.recommendation_reason)}</div>
                {sample.stop_factors.length > 0 && <div style={{ marginTop: 3, color: "var(--color-text-muted, #667085)" }}>Ограничения: {sample.stop_factors.map(factorLabel).join("; ")}</div>}
              </div>
              <div>
                <button type="button" onClick={() => setExpandedSampleId((current) => current === sample.id ? null : sample.id)} style={secondaryButton}>
                  {expandedSampleId === sample.id ? "Скрыть исходные данные" : "Показать исходные данные"}
                </button>
              </div>
              {expandedSampleId === sample.id && (
                <div style={{ ...card, padding: 12, background: "var(--color-surface-muted, #f8fafc)", display: "grid", gap: 10 }}>
                  {sampleDetailQuery.isLoading && <span>Загрузка исходных данных…</span>}
                  {sampleDetailQuery.isError && <span style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось загрузить исходные данные.</span>}
                  {sampleDetailQuery.data && (
                    <>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 8 }}>
                        <Row k="Подразделение" v={sampleDetailQuery.data.profile.department_name ?? "—"} />
                        <Row k="Ответственный" v={sampleDetailQuery.data.profile.owner_name ?? "—"} />
                        <Row k="Статус источника" v={statusLabel(sampleDetailQuery.data.snapshot.source_status)} />
                        <Row k="Тип проверки" v={sampleDetailQuery.data.snapshot.review_type ? reasonLabel(sampleDetailQuery.data.snapshot.review_type) : "—"} />
                        {sampleDetailQuery.data.snapshot.money_visible ? (
                          <>
                            <Row k="Оборот за 3 месяца" v={money(sampleDetailQuery.data.snapshot.total_3m)} />
                            <Row k="Последний месяц" v={money(sampleDetailQuery.data.snapshot.last_month)} />
                          </>
                        ) : (
                          <Row k="Денежные показатели" v="скрыты по роли" />
                        )}
                      </div>
                      {sampleDetailQuery.data.snapshot.money_visible && sampleDetailQuery.data.snapshot.monthly_sales && (
                        <div>
                          <strong style={{ fontSize: 13 }}>Продажи по месяцам</strong>
                          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 4, fontSize: 13 }}>
                            {Object.entries(sampleDetailQuery.data.snapshot.monthly_sales).map(([month, value]) => <span key={month}>{month}: <strong>{money(value)}</strong></span>)}
                          </div>
                        </div>
                      )}
                      {sampleDetailQuery.data.snapshot.conflicts.length > 0 && <div style={{ fontSize: 13 }}>Конфликты данных: {sampleDetailQuery.data.snapshot.conflicts.map(reasonLabel).join("; ")}</div>}
                      {sampleDetailQuery.data.profile.master_data_flags.length > 0 && <div style={{ fontSize: 13 }}>Признаки справочника: {sampleDetailQuery.data.profile.master_data_flags.map(reasonLabel).join("; ")}</div>}
                      <CustomerPriceTypeEvidence kind="history" title="История" value={sampleDetailQuery.data.snapshot.history} />
                      <CustomerPriceTypeEvidence kind="returns" title="Возвраты" value={sampleDetailQuery.data.snapshot.returns} />
                      {sampleDetailQuery.data.snapshot.money_visible && <CustomerPriceTypeEvidence kind="economics" title="Экономика" value={sampleDetailQuery.data.snapshot.economics} />}
                      {sampleDetailQuery.data.snapshot.money_visible && <CustomerPriceTypeEvidence kind="payments" title="Оплаты" value={sampleDetailQuery.data.snapshot.payments} />}
                    </>
                  )}
                </div>
              )}
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10, alignItems: "end" }}>
                <button type="button" disabled={reviewMutation.isPending || sample.status === "reviewed"} onClick={() => reviewMutation.mutate({ sample, reviewResult: "correct" })} style={primaryActionButton}>
                  Результат верный
                </button>
                <button type="button" disabled={sample.status === "reviewed"} onClick={() => setReviewActions((current) => ({ ...current, [sample.id]: current[sample.id] === "incorrect" ? null : "incorrect" }))} style={secondaryButton}>Результат неверный</button>
                <button type="button" disabled={sample.status === "reviewed"} onClick={() => setReviewActions((current) => ({ ...current, [sample.id]: current[sample.id] === "data_issue" ? null : "data_issue" }))} style={secondaryButton}>Ошибка в данных</button>
              </div>
              {selectedAction && sample.status !== "reviewed" && (
                <div style={{ ...card, padding: 12, display: "grid", gap: 10, background: "var(--color-surface-muted, #f8fafc)" }}>
                  {selectedAction === "incorrect" && (
                    <label style={{ display: "grid", gap: 4, fontSize: 13 }}>
                      Правильный результат
                      <select value={selectedGroup} onChange={(event) => setGroups((current) => ({ ...current, [sample.id]: event.target.value as CptQualityGroup }))} style={{ minHeight: 38, padding: "0 10px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8, background: "var(--color-surface, #fff)" }}>
                        {alternativeGroups.map((group) => <option key={group} value={group}>{QUALITY_GROUP_LABELS[group]}</option>)}
                      </select>
                    </label>
                  )}
                  <label style={{ display: "grid", gap: 4, fontSize: 13 }}>
                    Комментарий <span aria-hidden="true">*</span>
                    <input required value={comment} maxLength={2000} placeholder={selectedAction === "data_issue" ? "Какие данные выглядят неверно" : "Почему результат требует исправления"} onChange={(event) => setComments((current) => ({ ...current, [sample.id]: event.target.value }))} style={{ minHeight: 38, padding: "0 10px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8 }} />
                  </label>
                  <button type="button" disabled={reviewMutation.isPending || !canSubmitAlternative} onClick={() => reviewMutation.mutate({ sample, reviewResult: selectedAction, correctGroup: selectedAction === "incorrect" ? selectedGroup : null, comment })} style={secondaryButton}>
                    {selectedAction === "data_issue" ? "Передать технической команде" : "Сохранить исправленную оценку"}
                  </button>
                </div>
              )}
              {sample.status === "reviewed" && (
                <div role="status" style={{ padding: 10, borderRadius: 8, background: "#ecfdf3", color: "#027a48", fontSize: 13 }}>
                  Сохранено: {sample.review_result === "data_issue" ? "ошибка в данных" : sample.review_result === "incorrect" ? "результат исправлен" : "результат подтверждён"}. {sample.reviewed_by ?? "Пользователь"}, {sample.reviewed_at ? new Date(sample.reviewed_at).toLocaleString("ru-RU") : "время не указано"}.
                </div>
              )}
            </article>
          );
        })}
        {reviewMutation.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось сохранить оценку. Обновите выборку и повторите.</p>}
      </section>

      {canViewDataIssues && (
        <section style={{ ...card, display: "grid", gap: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <div>
              <strong>Проблемы данных</strong>
              <p style={{ margin: "4px 0 0", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>Внутренняя очередь. Тип цены по этим карточкам не изменяется.</p>
            </div>
            <input type="search" aria-label="Поиск в проблемах данных" placeholder="Код или название клиента…" value={dataIssueSearch} onChange={(event) => setDataIssueSearch(event.target.value)} style={{ minHeight: 38, padding: "0 10px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8 }} />
          </div>
          {dataIssuesQuery.isLoading && <p>Загрузка…</p>}
          {dataIssuesQuery.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось загрузить проблемы данных.</p>}
          {dataIssuesQuery.data?.payload.map((item) => (
            <div key={`${item.issue_source}-${item.counterparty_ref}`} style={{ borderTop: "1px solid var(--color-border, #eaecf0)", paddingTop: 10 }}>
              <strong>{item.counterparty_code ?? "—"} · {item.counterparty_name ?? "—"}</strong>
              <div style={{ marginTop: 3, fontSize: 13 }}>{item.issue_text}</div>
              <small style={{ color: "var(--color-text-muted, #667085)" }}>Текущий тип: {item.current_price_type ?? "не определён"}. Рекомендация не сформирована.</small>
              {item.comment && <div style={{ marginTop: 3, fontSize: 13 }}>Комментарий: {item.comment}</div>}
            </div>
          ))}
          {dataIssuesQuery.data && dataIssuesQuery.data.payload.length === 0 && <p>Проблемы данных не найдены.</p>}
        </section>
      )}

      {metrics && metrics.reviewed_count > 0 && (
        <section style={{ ...card, display: "grid", gap: 10 }}>
          <strong>Качество по очередям</strong>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 680 }}>
              <thead><tr><th style={th}>Очередь</th><th style={th}>Срез / выборка / проверено</th><th style={th}>Точность</th><th style={th}>Полнота</th><th style={th}>Ложные</th><th style={th}>Пропущенные</th></tr></thead>
              <tbody>
                {QUALITY_GROUPS.map((group) => {
                  const item = metrics.groups[group];
                  if (!item || (item.population_count === 0 && item.selected_count === 0)) return null;
                  return <tr key={group}><td style={td}>{QUALITY_GROUP_LABELS[group]}</td><td style={td}>{item.population_count} / {item.selected_count} / {item.reviewed_count}</td><td style={td}>{percent(item.precision)}</td><td style={td}>{percent(item.recall)}</td><td style={td}>{item.false_positive}</td><td style={td}>{item.false_negative}</td></tr>;
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

function Tile({ label, value, loading, accent }: { label: string; value?: number; loading?: boolean; accent?: boolean }) {
  return (
    <div style={{ ...card, background: accent ? "var(--color-primary, #2563eb)" : "var(--color-surface, #fff)", color: accent ? "#fff" : "inherit" }}>
      <div style={{ fontSize: 22, fontWeight: 800 }}>{loading ? "…" : (value ?? 0).toLocaleString("ru-RU")}</div>
      <div style={{ fontSize: 13, opacity: 0.85 }}>{label}</div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "3px 0", fontSize: 14 }}>
      <span style={{ color: "var(--color-text-muted, #667085)" }}>{k}</span>
      <span style={{ fontWeight: 600, textAlign: "right" }}>{v}</span>
    </div>
  );
}
