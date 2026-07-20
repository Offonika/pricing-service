import { useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchCptCaseDetail,
  fetchCptCases,
  fetchCptQualityMetrics,
  fetchCptQualitySamples,
  fetchCptSummary,
  fetchCptWorklists,
  prepareCptQualitySamples,
  reviewCptQualitySample,
  type CptQualityGroup,
  type CptQualitySample,
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
  data_check: "Сверка данных",
  special_review: "Спецпроверка",
  downgrade_approval: "Согласование понижения",
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
  const canViewQuality = role === "internal" || role === "network_head" || role === "quality";

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
            Расчёт за {snapshotMonthLabel(month)}. Только просмотр — тип цены меняет человек.
          </p>
        </div>
        <div style={{ textAlign: "right", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
          {bitrixUserName && <div>{bitrixUserName}</div>}
          {role && <div>Роль: {roleLabel(role)}{canViewMoney ? " · суммы доступны" : ""}</div>}
          {canViewQuality && (
            <button type="button" onClick={() => setSection("quality")} style={{ ...secondaryButton, marginTop: 8 }}>
              Экспертная оценка
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

      {/* Worklist queues */}
      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
        {WORKLIST_ORDER.map((key) => {
          const count = worklists[key] ?? 0;
          const active = worklist === key;
          return (
            <button
              key={key}
              type="button"
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

function percent(value: number): string {
  return `${(value * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function QualityModule({ role }: { role?: string }) {
  const queryClient = useQueryClient();
  const [perGroup, setPerGroup] = useState(30);
  const [statusFilter, setStatusFilter] = useState<"pending" | "reviewed" | null>("pending");
  const [groups, setGroups] = useState<Record<number, CptQualityGroup>>({});
  const [comments, setComments] = useState<Record<number, string>>({});
  const canPrepare = role === "internal" || role === "network_head";

  const metricsQuery = useQuery({
    queryKey: ["cpt", "quality", "metrics"],
    queryFn: fetchCptQualityMetrics,
  });
  const samplesQuery = useQuery({
    queryKey: ["cpt", "quality", "samples", statusFilter],
    queryFn: () => fetchCptQualitySamples({ status: statusFilter }),
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
    mutationFn: ({ sample, correctGroup, comment }: { sample: CptQualitySample; correctGroup: CptQualityGroup; comment: string }) =>
      reviewCptQualitySample({
        sampleId: sample.id,
        correctGroup,
        comment,
        expectedVersion: sample.version,
      }),
    onSuccess: refreshQuality,
  });

  const metrics = metricsQuery.data;
  const samples = samplesQuery.data?.payload ?? [];

  return (
    <>
      <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
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
        {samplesQuery.isLoading && <p>Загрузка выборки…</p>}
        {samplesQuery.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось загрузить выборку.</p>}
        {!samplesQuery.isLoading && samples.length === 0 && (
          <p style={{ color: "var(--color-text-muted, #667085)" }}>
            {metrics?.selected_count ? "В этом разделе строк нет." : "Контрольная выборка ещё не подготовлена."}
          </p>
        )}
        {samples.map((sample) => {
          const selectedGroup = groups[sample.id] ?? sample.correct_group ?? sample.system_group;
          const comment = comments[sample.id] ?? sample.comment ?? "";
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
                <strong>{recommendationLabel(sample.system_recommendation)}</strong>
                <div style={{ marginTop: 3, color: "var(--color-text-muted, #667085)" }}>{reasonLabel(sample.recommendation_reason)}</div>
                {sample.stop_factors.length > 0 && <div style={{ marginTop: 3, color: "var(--color-text-muted, #667085)" }}>Ограничения: {sample.stop_factors.map(factorLabel).join("; ")}</div>}
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(220px, 1fr) minmax(260px, 2fr) auto", gap: 10, alignItems: "end" }}>
                <label style={{ display: "grid", gap: 4, fontSize: 13 }}>
                  Правильная очередь
                  <select value={selectedGroup} onChange={(event) => setGroups((current) => ({ ...current, [sample.id]: event.target.value as CptQualityGroup }))} style={{ minHeight: 38, padding: "0 10px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8, background: "var(--color-surface, #fff)" }}>
                    {QUALITY_GROUPS.map((group) => <option key={group} value={group}>{QUALITY_GROUP_LABELS[group]}</option>)}
                  </select>
                </label>
                <label style={{ display: "grid", gap: 4, fontSize: 13 }}>
                  Комментарий
                  <input value={comment} maxLength={2000} placeholder="Почему решение верно или требует исправления" onChange={(event) => setComments((current) => ({ ...current, [sample.id]: event.target.value }))} style={{ minHeight: 38, padding: "0 10px", border: "1px solid var(--color-border, #d0d5dd)", borderRadius: 8 }} />
                </label>
                <button type="button" disabled={reviewMutation.isPending} onClick={() => reviewMutation.mutate({ sample, correctGroup: selectedGroup, comment })} style={secondaryButton}>
                  Сохранить оценку
                </button>
              </div>
            </article>
          );
        })}
        {reviewMutation.isError && <p style={{ color: "var(--color-danger, #d92d20)" }}>Не удалось сохранить оценку. Обновите выборку и повторите.</p>}
      </section>

      {metrics && metrics.reviewed_count > 0 && (
        <section style={{ ...card, display: "grid", gap: 10 }}>
          <strong>Качество по очередям</strong>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 680 }}>
              <thead><tr><th style={th}>Очередь</th><th style={th}>Проверено</th><th style={th}>Точность</th><th style={th}>Полнота</th><th style={th}>Ложные</th><th style={th}>Пропущенные</th></tr></thead>
              <tbody>
                {QUALITY_GROUPS.map((group) => {
                  const item = metrics.groups[group];
                  if (!item?.reviewed_count && !item?.false_negative) return null;
                  return <tr key={group}><td style={td}>{QUALITY_GROUP_LABELS[group]}</td><td style={td}>{item.reviewed_count}</td><td style={td}>{percent(item.precision)}</td><td style={td}>{percent(item.recall)}</td><td style={td}>{item.false_positive}</td><td style={td}>{item.false_negative}</td></tr>;
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
