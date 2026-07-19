import { useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  fetchCptCaseDetail,
  fetchCptCases,
  fetchCptSummary,
  fetchCptWorklists,
  type CptWorklist,
} from "../api/customerPriceTypes";

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
  return LEVEL_LABELS[level] ?? level;
}

export function CustomerPriceTypesWorkspace({
  bitrixUserName,
  role,
  canViewMoney,
}: CustomerPriceTypesWorkspaceProps) {
  const [worklist, setWorklist] = useState<CptWorklist | null>(null);
  const [search, setSearch] = useState("");
  const [selectedCaseId, setSelectedCaseId] = useState<number | null>(null);

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

  return (
    <div style={shell}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "end", gap: 16 }}>
        <div>
          <p style={{ margin: 0, color: "var(--color-primary, #2563eb)", fontWeight: 800, fontSize: 12, letterSpacing: "0.08em", textTransform: "uppercase" }}>
            Управление типами цен
          </p>
          <h1 style={{ margin: "4px 0 0", fontSize: 24 }}>Портфель клиентов</h1>
          <p style={{ margin: "6px 0 0", color: "var(--color-text-muted, #667085)" }}>
            Расчёт за {month ?? "—"}. Только просмотр — тип цены меняет человек.
          </p>
        </div>
        <div style={{ textAlign: "right", color: "var(--color-text-muted, #667085)", fontSize: 13 }}>
          {bitrixUserName && <div>{bitrixUserName}</div>}
          {role && <div>роль: {role}{canViewMoney ? " · деньги видны" : ""}</div>}
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
          <strong>{worklist ? WORKLIST_LABELS[worklist] : "Все actionable-кейсы"}</strong>
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
                    <td style={td}>{WORKLIST_LABELS[row.case_type] ?? row.case_type}</td>
                    <td style={td}>{row.recommended_price_type ?? row.system_recommendation}</td>
                    <td style={td}>{row.approval_status}</td>
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
                  <Row k="Рекомендация" v={detail.snapshot.system_recommendation} />
                  <Row k="Цель" v={detail.snapshot.recommended_price_type ?? "—"} />
                  <Row k="Очередь" v={WORKLIST_LABELS[detail.case.case_type] ?? detail.case.case_type} />
                  <Row k="Согласование" v={detail.case.approval_status} />
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
                    {detail.snapshot.recommendation_reason}
                  </div>
                  {detail.snapshot.stop_factors.length > 0 && (
                    <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {detail.snapshot.stop_factors.map((sf) => (
                        <span key={sf} style={{ fontSize: 11, padding: "2px 8px", borderRadius: 999, background: "var(--color-surface-muted, #f2f4f7)", color: "var(--color-text-muted, #667085)" }}>{sf}</span>
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
                        {event.event_type}
                        {event.comment ? ` — ${event.comment}` : ""}
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
