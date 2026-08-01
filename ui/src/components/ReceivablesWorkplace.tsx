import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import { resolveBitrixPortalUrl } from "../api/bitrix";
import { clearApiAuthToken, setApiAuthToken } from "../api/client";
import {
  fetchCounterpartyFolderRecommendations,
  fetchReceivableWorkplace,
  fetchReceivableWorkplaceMeta,
  buildReceivableWorkplaceActionPayload,
  receivablesErrorMessage,
  updateReceivableWorkplaceItem,
  type CounterpartyFolderRecommendation,
  type CounterpartyFolderQueue,
  type ReceivableCacheComponent,
  type ReceivableDepartmentOption,
  type ReceivableStatusOption,
  type ReceivableWorkplaceSortBy,
  type ReceivableWorkplaceSortDir,
  type ReceivableWorkplaceEditState,
  type ReceivableWorkplaceItem,
  type ReceivableWorkplaceSummary,
} from "../api/receivables";

const RECEIVABLES_TOKEN_SESSION_KEY = "pricing.receivables.session_token.v1";
const RECEIVABLES_TOKEN_LEGACY_KEY = "pricing.receivables.token.v1";

type EditState = ReceivableWorkplaceEditState;

type QuickFilter = "" | "call_today" | "no_phone" | "overdue_30" | "overdue_90" | "postponed";
type MinimumDebtFilter = "" | "500" | "1000";
type ReceivablesTab = "work" | "folders";

const emptySummary: ReceivableWorkplaceSummary = {
  row_count: 0,
  total_receivable: "0",
  total_overdue: "0",
  overdue_over_30_amount: "0",
  overdue_over_90_amount: "0",
  need_call_today_amount: "0",
  no_phone_count: 0,
  credit_depth_default_count: 0,
};

type ReceivablesWorkplaceProps = {
  bitrixMode?: boolean;
  bitrixUserName?: string | null;
  accessLevel?: "full" | "department";
  departmentRefs?: string[];
};

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function readInitialDate() {
  const params = new URLSearchParams(window.location.search);
  const value = params.get("date") || "";
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : todayIso();
}

function readInitialToken() {
  window.localStorage.removeItem(RECEIVABLES_TOKEN_LEGACY_KEY);
  return window.sessionStorage.getItem(RECEIVABLES_TOKEN_SESSION_KEY) || "";
}

function readInitialTab(): ReceivablesTab {
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab") === "folders" || window.location.hash === "#folders") return "folders";
  return "work";
}

function readDashboardReturnUrl() {
  const value = new URLSearchParams(window.location.search).get("return_to") || "";
  if (value.startsWith("/bitrix/executive-dashboard/") || value.startsWith("/executive-dashboard/")) {
    return value;
  }
  return "";
}

function getErrorMessage(error: unknown, fallback: string) {
  return receivablesErrorMessage(error, fallback);
}

function getErrorStatus(error: unknown) {
  return typeof error === "object" && error !== null && "response" in error
    ? (error as { response?: { status?: number } }).response?.status
    : undefined;
}

function dateInput(value?: string | null) {
  return value ? value.slice(0, 10) : "";
}

function formatMoney(value: string | number | null | undefined) {
  const numberValue = Number(value || 0);
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(Number.isFinite(numberValue) ? numberValue : 0);
}

function formatDate(value?: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 10);
  return parsed.toLocaleDateString("ru-RU");
}

function formatDateTime(value?: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.replace("T", " ").slice(0, 16);
  return parsed.toLocaleString("ru-RU", {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

function newActionId() {
  if ("crypto" in window && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function initialEdit(item: ReceivableWorkplaceItem): EditState {
  return {
    status: item.status,
    contacted_staff_ref: item.contacted_staff_ref || "",
    promised_payment_date: dateInput(item.promised_payment_date),
    last_contact_at: dateInput(item.last_contact_at),
    next_action_date: dateInput(item.next_action_date),
    payment_postponed: item.payment_postponed,
    comment: item.comment || "",
  };
}

function debtRuleLabel(value?: string | null) {
  const labels: Record<string, string> = {
    statement_direct_payment_match: "закрыто ближайшей оплатой",
    statement_multi_sale_payment_match: "группа закрыта одной оплатой",
    statement_bottom_up_balance_cutoff: "подбор от текущего остатка",
    statement_unmatched_open_sale: "нет закрывающего документа",
    statement_structure_confirmed_open: "подтверждено структурой 1С",
    confirmed_open: "подтверждено структурой 1С",
  };
  return value ? labels[value] || value : "расчет по открытым документам";
}

function folderStatusLabel(value?: string | null) {
  const labels: Record<string, string> = {
    move_recommended: "перенести",
    needs_review: "нужна проверка",
    no_overdue: "нет просрочки",
    ok: "папка совпадает",
  };
  return value ? labels[value] || value : "";
}

function folderReviewReasonLabel(value?: string | null) {
  const labels: Record<string, string> = {
    department_folder_missing: "не найдена папка подразделения",
    open_structure_document_not_found: "не найден открытый документ по структуре 1С",
    open_debt_statement_missing: "в ведомости нет документов для подтверждения долга",
    open_debt_structure_unconfirmed: "структура документов 1С не подтверждена",
    open_debt_document_total_below_balance: "сумма найденных накладных меньше долга",
    open_debt_document_total_above_balance: "сумма найденных накладных больше долга",
    origin_document_structure_confirmed_manual_review: "исходная накладная требует ручной сверки",
    spb_cross_folder_manual_review: "СПБ: нужна ручная проверка между папками",
  };
  return value ? labels[value] || value : "";
}

function matchDetailsText(details: Array<Record<string, unknown>>) {
  return details
    .map((detail) => {
      const documentNumber = String(detail.document_number || detail.document_ref || "").trim();
      const amount = detail.amount ? formatMoney(String(detail.amount)) : "";
      return [documentNumber, amount].filter(Boolean).join(" ");
    })
    .filter(Boolean)
    .join(", ");
}

function departmentsFromItems(items: ReceivableWorkplaceItem[]): ReceivableDepartmentOption[] {
  const byRef = new Map<string, string>();
  items.forEach((item) => {
    if (item.department_ref) byRef.set(item.department_ref, item.department_name || item.department_ref);
  });
  return [...byRef.entries()]
    .sort((a, b) => a[1].localeCompare(b[1], "ru"))
    .map(([department_ref, department_name]) => ({ department_ref, department_name }));
}

function rowBadges(item: ReceivableWorkplaceItem) {
  const badges: Array<{ label: string; tone: "danger" | "warning" | "info" }> = [];
  if (item.needs_call_today) badges.push({ label: "звонок сегодня", tone: "info" });
  if (item.needs_credit_depth_default) badges.push({ label: "7 дней расчетно", tone: "warning" });
  if ((item.effective_overdue_days || 0) >= 90) badges.push({ label: "90+ дней", tone: "danger" });
  else if ((item.effective_overdue_days || 0) >= 30) badges.push({ label: "30+ дней", tone: "warning" });
  return badges;
}

function ReceivableSummary({
  summary,
  totalCount,
  visibleCount,
}: {
  summary: ReceivableWorkplaceSummary;
  totalCount: number;
  visibleCount: number;
}) {
  const metrics = [
    ["Общая дебиторка", formatMoney(summary.total_receivable)],
    ["Общая просрочка", formatMoney(summary.total_overdue)],
    ["> 30 дней", formatMoney(summary.overdue_over_30_amount)],
    ["> 90 дней", formatMoney(summary.overdue_over_90_amount)],
    ["Позвонить сегодня", formatMoney(summary.need_call_today_amount)],
    ["Без телефона", String(summary.no_phone_count)],
    ["7 дней расчетно", String(summary.credit_depth_default_count)],
    ["Показано", `${visibleCount} из ${totalCount}`],
  ];
  return (
    <section className="receivables__summary">
      {metrics.map(([label, value]) => (
        <div className="receivables__metric" key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </section>
  );
}

function ReceivableRow({
  item,
  index,
  statusOptions,
  expanded,
  commentExpanded,
  saving,
  edit,
  onToggle,
  onToggleComment,
  onEdit,
  onSave,
}: {
  item: ReceivableWorkplaceItem;
  index: number;
  statusOptions: ReceivableStatusOption[];
  expanded: boolean;
  commentExpanded: boolean;
  saving: boolean;
  edit: EditState;
  onToggle: () => void;
  onToggleComment: () => void;
  onEdit: (patch: Partial<EditState>) => void;
  onSave: () => void;
}) {
  const isPyatigorsk = (item.department_name || "").toLocaleLowerCase("ru-RU").includes("пятигор");
  const rowStatusOptions = statusOptions.filter(
    (option) => option.scope === "common" || (option.scope === "pyatigorsk" && isPyatigorsk)
  );
  const badges = rowBadges(item);
  if (!rowStatusOptions.some((option) => option.value === edit.status)) {
    rowStatusOptions.push(
      statusOptions.find((option) => option.value === edit.status) || {
        value: edit.status,
        label: edit.status,
        scope: "custom",
      }
    );
  }
  const bitrixCardUrl = resolveBitrixPortalUrl(item.bitrix_detail_url);
  return (
    <>
      <tr className="receivables__row">
        <td>
          <button className="receivables__expand" onClick={onToggle} type="button" title="Накладные">
            {expanded ? "−" : "+"}
          </button>
          {index + 1}
        </td>
        <td className="mono">{item.counterparty_code || ""}</td>
        <td>
          <strong>{item.counterparty_name || item.counterparty_ref}</strong>
          <span>{item.department_name || ""}</span>
          <div className="receivables__client-actions">
            {bitrixCardUrl ? (
              <a className="receivables__card-link" href={bitrixCardUrl} rel="noreferrer" target="_blank">
                Открыть карточку
              </a>
            ) : (
              <span className="receivables__card-link receivables__card-link--disabled">
                Карточка Bitrix не создана
              </span>
            )}
          </div>
          {badges.length > 0 && (
            <div className="receivables__badges">
              {badges.map((badge) => (
                <em className={`receivables__badge receivables__badge--${badge.tone}`} key={badge.label}>
                  {badge.label}
                </em>
              ))}
            </div>
          )}
        </td>
        <td>{item.responsible_name || ""}</td>
        <td className={item.no_phone_marker ? "receivables__phone receivables__phone--missing" : "receivables__phone"}>
          {item.phone || "нет"}
        </td>
        <td>{formatMoney(item.current_balance)}</td>
        <td>{item.effective_overdue_days || 0} дн.</td>
        <td>
          <input
            className="receivables__input"
            type="date"
            value={edit.promised_payment_date}
            onChange={(event) => onEdit({ promised_payment_date: event.target.value })}
          />
        </td>
        <td>
          <input
            className="receivables__input"
            type="date"
            value={edit.last_contact_at}
            onChange={(event) => onEdit({ last_contact_at: event.target.value })}
          />
        </td>
        <td>
          <select
            className="receivables__select"
            value={edit.contacted_staff_ref}
            onChange={(event) => onEdit({ contacted_staff_ref: event.target.value })}
          >
            <option value="">{item.staff_options.length ? "Не выбран" : "Нет сотрудников в источнике"}</option>
            {item.staff_options.map((staff) => (
              <option key={staff.staff_ref} value={staff.staff_ref}>
                {staff.staff_name}
              </option>
            ))}
          </select>
        </td>
        <td>
          <select
            className="receivables__select"
            value={edit.status}
            onChange={(event) => onEdit({ status: event.target.value })}
          >
            {rowStatusOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </td>
        <td>
          <input
            className="receivables__input"
            type="date"
            value={edit.next_action_date}
            onChange={(event) => onEdit({ next_action_date: event.target.value })}
          />
        </td>
        <td className="receivables__center">
          <label className="receivables__postponed">
            <input
              checked={edit.payment_postponed}
              onChange={(event) => onEdit({ payment_postponed: event.target.checked })}
              type="checkbox"
            />
            <span>{item.payment_postponed_count}</span>
          </label>
        </td>
        <td>
          <button
            aria-expanded={commentExpanded}
            className="receivables__comment-preview"
            onClick={onToggleComment}
            title={edit.comment || "Добавить комментарий"}
            type="button"
          >
            {edit.comment || "Добавить комментарий"}
          </button>
        </td>
      </tr>
      {commentExpanded && (
        <tr className="receivables__comment-editor-row">
          <td colSpan={14}>
            <div className="receivables__comment-editor">
              <label>
                <strong>Комментарий по {item.counterparty_name || item.counterparty_ref}</strong>
                <textarea
                  autoFocus
                  className="receivables__comment receivables__comment--expanded"
                  value={edit.comment}
                  onChange={(event) => onEdit({ comment: event.target.value })}
                />
              </label>
              <div className="receivables__comment-editor-actions">
                <button className="btn btn--ghost" onClick={onToggleComment} type="button">
                  Свернуть
                </button>
                <button className="btn" disabled={saving} onClick={onSave} type="button">
                  {saving ? "Сохраняем..." : "Сохранить комментарий"}
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}
      {expanded && (
        <tr className="receivables__details">
          <td colSpan={14}>
            <div className="receivables__documents">
              <div className="receivables__documents-head">
                <strong>Накладные</strong>
                <span>
                  Всего: {item.invoice_count}, просроченных: {item.overdue_invoice_count}
                </span>
                {item.needs_credit_depth_default && <mark>7 дней расчётно</mark>}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Номер</th>
                    <th>Дата</th>
                    <th>Сумма</th>
                    <th>Остаток</th>
                    <th>Правило</th>
                    <th>Срок</th>
                    <th>Просрочка</th>
                    <th>Менеджер</th>
                  </tr>
                </thead>
                <tbody>
                  {item.documents.map((document) => (
                    <tr key={`${document.document_ref || document.document_number}-${document.document_date || ""}`}>
                      <td>{document.document_number || ""}</td>
                      <td>{formatDate(document.document_date)}</td>
                      <td>{formatMoney(document.amount)}</td>
                      <td>{formatMoney(document.open_amount || document.amount)}</td>
                      <td>
                        <span className="receivables__debt-rule">
                          {debtRuleLabel(document.selection_rule || document.document_structure_status)}
                        </span>
                        {document.closing_amount && (
                          <small>Закрыто: {formatMoney(document.closing_amount)}</small>
                        )}
                        {document.return_amount && (
                          <small>Возврат: {formatMoney(document.return_amount)}</small>
                        )}
                        {document.statement_balance_after && (
                          <small>Баланс: {formatMoney(document.statement_balance_after)}</small>
                        )}
                        {document.match_details?.length > 0 && (
                          <small>{matchDetailsText(document.match_details)}</small>
                        )}
                      </td>
                      <td>{formatDate(document.due_date)}</td>
                      <td>{document.overdue_days || 0} дн.</td>
                      <td>{document.manager_name || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function FolderRecommendations({
  items,
  loading,
  summary,
  sourceStatus,
  queue,
  onQueueChange,
}: {
  items: CounterpartyFolderRecommendation[];
  loading: boolean;
  summary: Record<string, unknown>;
  sourceStatus: string;
  queue: CounterpartyFolderQueue;
  onQueueChange: (queue: CounterpartyFolderQueue) => void;
}) {
  if (loading) return <div className="receivables__state">Загрузка вкладки контроля папок...</div>;
  const sourceStale = sourceStatus === "source_stale";
  const computedAt = typeof summary.computed_at === "string" ? summary.computed_at : "";
  return (
    <section className="receivables__folder-tab">
      <div className="receivables__quick-filters">
        {[
          ["actionable", "Требует действия"],
          ["business_review", "Бизнес-проверка"],
          ["data_quality", "Ошибки данных"],
          ["excluded", "Исключено"],
        ].map(([value, label]) => (
          <button
            className={queue === value ? "btn btn--compact" : "btn btn--compact btn--ghost"}
            key={value}
            onClick={() => onQueueChange(value as CounterpartyFolderQueue)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>
      {sourceStale && (
        <div className="receivables__state">
          Источник накладных устарел. Номера накладных скрыты, автоматические переносы отключены.
        </div>
      )}
      <div className="receivables__folder-summary">
        <span>Строк: {String(summary.total_count ?? items.length)}</span>
        <span>Требует действия: {String(summary.actionable_count ?? 0)}</span>
        <span>Бизнес-проверка: {String(summary.business_review_count ?? 0)}</span>
        <span>Ошибки данных: {String(summary.data_quality_count ?? 0)}</span>
        <span>Источник: {sourceStatus || "ready"}</span>
        {computedAt && <span>Расчет: {formatDateTime(computedAt)}</span>}
      </div>
      {!items.length ? (
        <div className="receivables__state">В выбранной очереди строк нет.</div>
      ) : (
      <table>
        <thead>
          <tr>
            <th>Код 1С</th>
            <th>Клиент</th>
            <th>Текущая папка</th>
            <th>Рекомендованная папка</th>
            <th>Долгообразующая накладная</th>
            <th>Долг</th>
            <th>Статус</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const recommendedFolder =
              item.recommended_folder_display_name || item.recommended_folder_name || "";
            const debtInvoiceNumber = sourceStale
              ? ""
              : item.debt_document_number || item.origin_document_number || "";
            const debtInvoiceDate = sourceStale
              ? ""
              : item.debt_document_date || item.origin_document_date || "";
            const reviewReason = folderReviewReasonLabel(item.review_reason);
            return (
              <tr key={item.counterparty_ref}>
                <td className="mono">{item.counterparty_code || ""}</td>
                <td>{item.counterparty_name || item.counterparty_ref}</td>
                <td title={item.current_folder_name || ""}>
                  {item.current_folder_display_name || item.current_folder_name || ""}
                </td>
                <td title={item.recommended_folder_name || ""}>
                  {recommendedFolder || "—"}
                </td>
                <td title={debtInvoiceNumber}>
                  {debtInvoiceNumber || "—"}
                  {debtInvoiceDate && <small>{formatDate(debtInvoiceDate)}</small>}
                </td>
                <td>{formatMoney(item.current_balance)}</td>
                <td>
                  {folderStatusLabel(item.status)}
                  {reviewReason && <small>{reviewReason}</small>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      )}
    </section>
  );
}

export function ReceivablesWorkplace({
  bitrixMode = false,
  bitrixUserName,
  accessLevel = "full",
}: ReceivablesWorkplaceProps) {
  const [token, setToken] = useState(() => (bitrixMode ? "" : readInitialToken()));
  const [date, setDate] = useState(readInitialDate);
  const [departmentRef, setDepartmentRef] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [minimumDebt, setMinimumDebt] = useState<MinimumDebtFilter>("");
  const [sortBy, setSortBy] = useState<ReceivableWorkplaceSortBy>("balance");
  const [sortDir, setSortDir] = useState<ReceivableWorkplaceSortDir>("desc");
  const [quickFilter, setQuickFilter] = useState<QuickFilter>("");
  const [items, setItems] = useState<ReceivableWorkplaceItem[]>([]);
  const [summary, setSummary] = useState<ReceivableWorkplaceSummary>(emptySummary);
  const [totalCount, setTotalCount] = useState(0);
  const [visibleCount, setVisibleCount] = useState(0);
  const [statusOptions, setStatusOptions] = useState<ReceivableStatusOption[]>([]);
  const [departmentOptions, setDepartmentOptions] = useState<ReceivableDepartmentOption[]>([]);
  const [cacheStatus, setCacheStatus] = useState<Record<string, ReceivableCacheComponent>>({});
  const [sourceStatus, setSourceStatus] = useState("");
  const [edits, setEdits] = useState<Record<string, EditState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [commentsExpanded, setCommentsExpanded] = useState<Set<string>>(() => new Set());
  const [saving, setSaving] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [tab, setTab] = useState<ReceivablesTab>(() => readInitialTab());
  const [folderItems, setFolderItems] = useState<CounterpartyFolderRecommendation[]>([]);
  const [folderSummary, setFolderSummary] = useState<Record<string, unknown>>({});
  const [folderSourceStatus, setFolderSourceStatus] = useState("");
  const [folderQueue, setFolderQueue] = useState<CounterpartyFolderQueue>("actionable");
  const [foldersLoading, setFoldersLoading] = useState(false);
  const [metaLoaded, setMetaLoaded] = useState(false);
  const dashboardReturnUrl = useMemo(readDashboardReturnUrl, []);
  const normalizedToken = token.trim();
  const hasToken = bitrixMode || normalizedToken.length > 0;

  useEffect(() => {
    if (bitrixMode) return;
    if (normalizedToken) {
      setApiAuthToken(normalizedToken);
      window.sessionStorage.setItem(RECEIVABLES_TOKEN_SESSION_KEY, normalizedToken);
      window.localStorage.removeItem(RECEIVABLES_TOKEN_LEGACY_KEY);
    } else {
      clearApiAuthToken();
      window.sessionStorage.removeItem(RECEIVABLES_TOKEN_SESSION_KEY);
      window.localStorage.removeItem(RECEIVABLES_TOKEN_LEGACY_KEY);
    }
  }, [bitrixMode, normalizedToken]);

  useEffect(() => {
    if (!hasToken) return;
    let cancelled = false;
    const requestedDate = metaLoaded ? date || undefined : undefined;
    fetchReceivableWorkplaceMeta(requestedDate)
      .then((data) => {
        if (cancelled) return;
        setMetaLoaded(true);
        setDepartmentOptions(data.department_options);
        setCacheStatus(data.cache_status || {});
        if (!metaLoaded && data.latest_snapshot_date) setDate(data.latest_snapshot_date);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setMetaLoaded(true);
        const status = getErrorStatus(error);
        if (status === 404 || status === 405) {
          setSourceStatus("meta_unavailable");
          return;
        }
        setMessage(getErrorMessage(error, "Не удалось загрузить параметры витрины"));
      });
    return () => {
      cancelled = true;
    };
  }, [date, hasToken, metaLoaded]);

  const displayedItems = useMemo(() => {
    if (!quickFilter) return items;
    return items.filter((item) => {
      if (quickFilter === "call_today") return item.needs_call_today;
      if (quickFilter === "no_phone") return item.no_phone_marker;
      if (quickFilter === "overdue_30") return (item.effective_overdue_days || 0) >= 30;
      if (quickFilter === "overdue_90") return (item.effective_overdue_days || 0) >= 90;
      if (quickFilter === "postponed") return item.payment_postponed_count > 0;
      return true;
    });
  }, [items, quickFilter]);

  const loadWorkplace = useCallback(async () => {
    if (!hasToken || !date) {
      setItems([]);
      setSummary(emptySummary);
      setStatusOptions([]);
      setMessage("");
      return;
    }
    setLoading(true);
    setMessage("");
    try {
      const data = await fetchReceivableWorkplace({
        date,
        department_ref: departmentRef,
        min_debt: minimumDebt ? Number(minimumDebt) : undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
        status: statusFilter,
      });
      const payload = data.payload || [];
      setItems(payload);
      setSummary(data.summary);
      setTotalCount(data.total_count ?? data.summary?.row_count ?? payload.length);
      setVisibleCount(data.visible_count ?? payload.length);
      setStatusOptions(data.status_options);
      setDepartmentOptions(
        data.department_options?.length ? data.department_options : departmentsFromItems(payload)
      );
      setCacheStatus(data.cache_status || {});
      setSourceStatus(data.source_status || "ready");
      setEdits(Object.fromEntries(payload.map((item) => [item.counterparty_ref, initialEdit(item)])));
    } catch (error: unknown) {
      setMessage(getErrorMessage(error, "Не удалось загрузить дебиторку"));
    } finally {
      setLoading(false);
    }
  }, [date, departmentRef, hasToken, minimumDebt, sortBy, sortDir, statusFilter]);

  const loadFolders = useCallback(async () => {
    if (!hasToken || !date) {
      setFolderItems([]);
      return;
    }
    setFoldersLoading(true);
    try {
      const data = await fetchCounterpartyFolderRecommendations(date, folderQueue);
      setFolderItems(data.payload);
      setFolderSummary(data.summary || {});
      setFolderSourceStatus(data.source_status);
    } catch (error: unknown) {
      setMessage(getErrorMessage(error, "Не удалось загрузить контроль папок"));
    } finally {
      setFoldersLoading(false);
    }
  }, [date, folderQueue, hasToken]);

  useEffect(() => {
    void loadWorkplace();
  }, [loadWorkplace]);

  useEffect(() => {
    if (tab === "folders") void loadFolders();
  }, [loadFolders, tab]);

  const saveItem = async (item: ReceivableWorkplaceItem) => {
    const edit = edits[item.counterparty_ref] || initialEdit(item);
    const payload = buildReceivableWorkplaceActionPayload(item, edit, newActionId());
    if (Object.keys(payload).length === 1) {
      toast("Нет изменений для сохранения");
      return;
    }
    setSaving((prev) => new Set(prev).add(item.counterparty_ref));
    try {
      const response = await updateReceivableWorkplaceItem(
        date,
        item.counterparty_ref,
        payload
      );
      setItems((prev) =>
        prev.map((row) => (row.counterparty_ref === item.counterparty_ref ? response.item : row))
      );
      setEdits((prev) => ({ ...prev, [item.counterparty_ref]: initialEdit(response.item) }));
      toast.success("Сохранено");
    } catch (error: unknown) {
      toast.error(receivablesErrorMessage(error, "Не удалось сохранить"));
    } finally {
      setSaving((prev) => {
        const next = new Set(prev);
        next.delete(item.counterparty_ref);
        return next;
      });
    }
  };

  const openDebtComputedAt = cacheStatus.open_debt?.computed_at;

  return (
    <div className="app receivables">
      <header className="app__header receivables__header">
        {dashboardReturnUrl && (
          <a className="btn btn--ghost receivables__back" href={dashboardReturnUrl}>
            Назад в витрину
          </a>
        )}
        <h1>Дебиторка покупателей</h1>
        {bitrixMode && bitrixUserName && <span className="app__user">{bitrixUserName}</span>}
        <input className="app__search" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
        <select className="app__select" value={departmentRef} onChange={(event) => setDepartmentRef(event.target.value)}>
          <option value="">{accessLevel === "full" ? "Все подразделения" : "Все доступные подразделения"}</option>
          {departmentOptions.map((department) => (
            <option key={department.department_ref} value={department.department_ref}>
              {department.department_name}
            </option>
          ))}
        </select>
        <select className="app__select" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
          <option value="">Все статусы</option>
          {statusOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select
          className="app__select"
          value={minimumDebt}
          onChange={(event) => setMinimumDebt(event.target.value as MinimumDebtFilter)}
        >
          <option value="">Любая сумма долга</option>
          <option value="500">Долг &gt; 500 ₽</option>
          <option value="1000">Долг &gt; 1 000 ₽</option>
        </select>
        <select
          className="app__select"
          value={sortBy}
          onChange={(event) => setSortBy(event.target.value as ReceivableWorkplaceSortBy)}
        >
          <option value="balance">По сумме</option>
          <option value="overdue_days">По дням просрочки</option>
        </select>
        <select
          className="app__select"
          value={sortDir}
          onChange={(event) => setSortDir(event.target.value as ReceivableWorkplaceSortDir)}
        >
          <option value="desc">По убыванию</option>
          <option value="asc">По возрастанию</option>
        </select>
        {!bitrixMode && (
          <input
            className="app__search receivables__token"
            placeholder="Внутренний токен"
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
        )}
        <button className="btn btn--ghost" disabled={!hasToken || loading} onClick={loadWorkplace} type="button">
          {loading ? "Обновляем..." : "Обновить"}
        </button>
      </header>
      {hasToken && (
        <div className="receivables__freshness">
          <span>Дата витрины: {date || "не выбрана"}</span>
          <span>Источник: {sourceStatus || cacheStatus.open_debt?.source_status || "ожидает загрузки"}</span>
          {openDebtComputedAt && <span>Долг рассчитан: {formatDateTime(openDebtComputedAt)}</span>}
        </div>
      )}
      {hasToken && cacheStatus.open_debt?.source_status === "source_stale" && (
        <div className="receivables__state">
          Источник накладных устарел. Суммы долга актуальны, номера и даты накладных временно скрыты.
        </div>
      )}
      {!hasToken && <div className="receivables__auth-state">Введите внутренний токен, чтобы открыть витрину.</div>}
      {hasToken && <ReceivableSummary summary={summary} totalCount={totalCount} visibleCount={visibleCount} />}
      {hasToken && (
        <div className="receivables__quick-filters">
        {[
          ["", "Все"],
          ["call_today", "Позвонить сегодня"],
          ["no_phone", "Нет телефона"],
          ["overdue_30", "30+"],
          ["overdue_90", "90+"],
          ["postponed", "Переносили"],
        ].map(([value, label]) => (
          <button
            className={quickFilter === value ? "btn btn--compact" : "btn btn--compact btn--ghost"}
            key={value}
            onClick={() => setQuickFilter(value as QuickFilter)}
            type="button"
          >
            {label}
          </button>
        ))}
        </div>
      )}
      <nav className="receivables__tabs">
        <button className={tab === "work" ? "btn" : "btn btn--ghost"} onClick={() => setTab("work")} type="button">
          Рабочий список
        </button>
        <button className={tab === "folders" ? "btn" : "btn btn--ghost"} onClick={() => setTab("folders")} type="button">
          Закрепление клиентов
        </button>
      </nav>
      {message && <div className="products-table__state products-table__state--error">{message}</div>}
      {hasToken && tab === "work" && (
        <section className="receivables__table-wrap">
          {loading ? (
            <div className="receivables__state">Загрузка рабочего списка...</div>
          ) : (
            <table className="receivables__table">
              <thead>
                <tr>
                  <th>№</th>
                  <th>Код 1С</th>
                  <th>Клиент</th>
                  <th>Ответственный</th>
                  <th>Телефон</th>
                  <th>Общий долг</th>
                  <th>Просрочено</th>
                  <th>Обещанная дата</th>
                  <th>Последний контакт</th>
                  <th>Кто общался</th>
                  <th>Статус</th>
                  <th>Следующий контакт</th>
                  <th>Перенес</th>
                  <th>Комментарий</th>
                </tr>
              </thead>
              <tbody>
                {displayedItems.map((item, index) => (
                  <ReceivableRow
                    key={item.counterparty_ref}
                    edit={edits[item.counterparty_ref] || initialEdit(item)}
                    expanded={expanded.has(item.counterparty_ref)}
                    commentExpanded={commentsExpanded.has(item.counterparty_ref)}
                    index={index}
                    item={item}
                    onEdit={(patch) =>
                      setEdits((prev) => ({
                        ...prev,
                        [item.counterparty_ref]: {
                          ...(prev[item.counterparty_ref] || initialEdit(item)),
                          ...patch,
                        },
                      }))
                    }
                    onSave={() => void saveItem(item)}
                    onToggleComment={() =>
                      setCommentsExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(item.counterparty_ref)) next.delete(item.counterparty_ref);
                        else next.add(item.counterparty_ref);
                        return next;
                      })
                    }
                    onToggle={() =>
                      setExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(item.counterparty_ref)) next.delete(item.counterparty_ref);
                        else next.add(item.counterparty_ref);
                        return next;
                      })
                    }
                    saving={saving.has(item.counterparty_ref)}
                    statusOptions={statusOptions}
                  />
                ))}
              </tbody>
            </table>
          )}
          {!loading && !displayedItems.length && <div className="receivables__state">На выбранную дату строк нет.</div>}
        </section>
      )}
      {hasToken && tab === "folders" && (
        <FolderRecommendations
          items={folderItems}
          loading={foldersLoading}
          sourceStatus={folderSourceStatus}
          summary={folderSummary}
          queue={folderQueue}
          onQueueChange={setFolderQueue}
        />
      )}
      <footer className="receivables__legend">
        <span>Красное "нет" в телефоне: номера нет.</span>
        <span>Желтая метка: 7 дней расчётно или просрочка 30+.</span>
        <span>Кнопка + раскрывает накладные клиента.</span>
      </footer>
    </div>
  );
}
