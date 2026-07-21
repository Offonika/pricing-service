import type { CSSProperties, ReactNode } from "react";

import { statusLabel } from "./customerPriceTypeLabels";

export type CustomerPriceTypeEvidenceKind = "history" | "returns" | "economics" | "payments";

interface CustomerPriceTypeEvidenceProps {
  kind: CustomerPriceTypeEvidenceKind;
  title: string;
  value: Record<string, unknown> | null;
}

const blockStyle: CSSProperties = {
  marginTop: 8,
  padding: 12,
  border: "1px solid var(--color-border, #e4e7ec)",
  borderRadius: 10,
  background: "var(--color-surface, #fff)",
};

const gridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))",
  gap: "8px 20px",
};

function hasValue(value: unknown): boolean {
  return value !== null && value !== undefined && value !== "";
}

function formatNumber(value: unknown, maximumFractionDigits = 2): string {
  if (!hasValue(value)) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return parsed.toLocaleString("ru-RU", { maximumFractionDigits });
}

function formatMoney(value: unknown): string {
  if (!hasValue(value)) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
}

function formatPercent(value: unknown): string {
  if (!hasValue(value)) return "—";
  return `${formatNumber(value)}%`;
}

function formatDate(value: unknown): string {
  if (!hasValue(value)) return "—";
  const raw = String(value);
  const parsed = new Date(`${raw}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return raw;
  return parsed.toLocaleDateString("ru-RU");
}

function formatSourceNote(value: unknown, kind: CustomerPriceTypeEvidenceKind): string {
  if (!hasValue(value)) return "—";
  const raw = String(value);
  if (raw.includes("_AccumRg7550") || raw.includes("_AccumRg7580")) {
    return "Данные только для чтения из 1С: выручка, себестоимость и возвраты по браку или качеству.";
  }
  if (raw.includes("_Document196") || raw.includes("_AccumRg7614")) {
    return "Данные только для чтения из 1С: наличные, безналичные платежи и эквайринг.";
  }
  if (kind === "returns" && raw.startsWith("1С read-only")) {
    return "Данные только для чтения из 1С по возвратам, связанным с браком или качеством.";
  }
  return raw.replace(/^1С read-only:\s*/i, "Данные только для чтения из 1С: ");
}

function formatPaymentForm(value: unknown): string {
  const labels: Record<string, string> = {
    cash: "Преимущественно наличные",
    bank: "Преимущественно безналичные",
    mixed: "Смешанная",
    unknown: "Не определена",
  };
  return labels[String(value ?? "")] ?? String(value ?? "—");
}

function formatReviewType(value: unknown): string {
  if (!hasValue(value)) return "Не требуется";
  const labels: Record<string, string> = {
    data_check: "Сверка данных",
    quality: "Проверка качества",
    credit: "Кредитная проверка",
    economics: "Проверка экономики",
  };
  return labels[String(value)] ?? String(value);
}

function statusValue(value: Record<string, unknown>): string {
  const raw = value.source_status ?? value.status;
  return hasValue(raw) ? statusLabel(String(raw)) : "—";
}

function ReadableRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 14, padding: "5px 0" }}>
      <span style={{ color: "var(--color-text-muted, #667085)" }}>{label}</span>
      <strong style={{ textAlign: "right", fontWeight: 650 }}>{value}</strong>
    </div>
  );
}

function SourceNote({ value, kind }: { value: unknown; kind: CustomerPriceTypeEvidenceKind }) {
  if (!hasValue(value)) return null;
  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--color-border, #eaecf0)" }}>
      <div style={{ color: "var(--color-text-muted, #667085)", fontSize: 12, marginBottom: 3 }}>Источник данных</div>
      <div style={{ lineHeight: 1.45 }}>{formatSourceNote(value, kind)}</div>
    </div>
  );
}

function HistoryEvidence({ value }: { value: Record<string, unknown> }) {
  return (
    <div style={gridStyle}>
      <ReadableRow label="Доступная история" value={hasValue(value.coverage_months) ? `${formatNumber(value.coverage_months, 0)} мес.` : "—"} />
      <ReadableRow label="Первая покупка" value={formatDate(value.first_activity_date)} />
      <ReadableRow label="Месяцев без покупок подряд" value={formatNumber(value.consecutive_zero_months, 0)} />
    </div>
  );
}

function ReturnsEvidence({ value }: { value: Record<string, unknown> }) {
  return (
    <>
      <div style={gridStyle}>
        <ReadableRow label="Статус данных" value={statusValue(value)} />
        <ReadableRow label="Возвраты по браку за 90 дней" value={formatMoney(value.defect_return_amount_90 ?? value.return_amount)} />
        <ReadableRow label="Доля возвратов" value={formatPercent(value.return_rate_pct)} />
        <ReadableRow label="Дополнительная проверка" value={formatReviewType(value.review_type)} />
      </div>
      <SourceNote value={value.source_note} kind="returns" />
    </>
  );
}

function EconomicsEvidence({ value }: { value: Record<string, unknown> }) {
  const periods = [30, 60, 90] as const;
  const rows = [
    { label: "Выручка", key: "revenue" },
    { label: "Себестоимость", key: "cost_of_sales" },
    { label: "Валовая прибыль", key: "gross_profit" },
    { label: "Возвраты по браку", key: "defect_return_amount" },
  ] as const;
  return (
    <>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", minWidth: 520, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={tableHeadStyle}>Показатель</th>
              {periods.map((period) => <th key={period} style={{ ...tableHeadStyle, textAlign: "right" }}>{period} дней</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td style={tableCellStyle}>{row.label}</td>
                {periods.map((period) => (
                  <td key={period} style={{ ...tableCellStyle, textAlign: "right", fontWeight: 650 }}>
                    {formatMoney(value[`${row.key}_${period}`])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ ...gridStyle, marginTop: 10 }}>
        <ReadableRow label="Валовая маржа за 90 дней" value={formatPercent(value.gross_margin_pct_90)} />
        <ReadableRow label="Рентабельность за 90 дней" value={formatPercent(value.profitability_pct_90)} />
        <ReadableRow label="Статус данных" value={statusValue(value)} />
      </div>
      <SourceNote value={value.source_note} kind="economics" />
    </>
  );
}

function PaymentsEvidence({ value }: { value: Record<string, unknown> }) {
  return (
    <>
      <div style={gridStyle}>
        <ReadableRow label="Основная форма оплаты" value={formatPaymentForm(value.payment_form_primary)} />
        <ReadableRow label="Доля наличных за 90 дней" value={formatPercent(value.cash_share_90)} />
        <ReadableRow label="Доля безналичных за 90 дней" value={formatPercent(value.bank_share_90)} />
        <ReadableRow label="Просроченная задолженность" value={formatMoney(value.overdue ?? value.overdue_amount)} />
        <ReadableRow label="Статус данных" value={statusValue(value)} />
      </div>
      <SourceNote value={value.source_note} kind="payments" />
    </>
  );
}

const tableHeadStyle: CSSProperties = {
  padding: "7px 9px",
  borderBottom: "1px solid var(--color-border, #d0d5dd)",
  color: "var(--color-text-muted, #667085)",
  fontSize: 12,
  fontWeight: 700,
  textAlign: "left",
};

const tableCellStyle: CSSProperties = {
  padding: "8px 9px",
  borderBottom: "1px solid var(--color-border, #eaecf0)",
};

export function CustomerPriceTypeEvidence({ kind, title, value }: CustomerPriceTypeEvidenceProps) {
  if (!value || Object.keys(value).length === 0) return null;
  return (
    <details style={{ fontSize: 13 }}>
      <summary style={{ cursor: "pointer", fontWeight: 700 }}>{title}</summary>
      <div style={blockStyle}>
        {kind === "history" && <HistoryEvidence value={value} />}
        {kind === "returns" && <ReturnsEvidence value={value} />}
        {kind === "economics" && <EconomicsEvidence value={value} />}
        {kind === "payments" && <PaymentsEvidence value={value} />}
      </div>
    </details>
  );
}
