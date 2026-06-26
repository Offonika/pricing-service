import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import { clearApiAuthToken, setApiAuthToken } from "../api/client";
import {
  fetchCounterpartyFolderRecommendations,
  fetchReceivableWorkplace,
  updateReceivableWorkplaceItem,
  type CounterpartyFolderRecommendation,
  type ReceivableStatusOption,
  type ReceivableWorkplaceItem,
  type ReceivableWorkplaceSummary,
} from "../api/receivables";

const RECEIVABLES_TOKEN_SESSION_KEY = "pricing.receivables.session_token.v1";
const RECEIVABLES_TOKEN_LEGACY_KEY = "pricing.receivables.token.v1";

type EditState = {
  status: string;
  contacted_staff_ref: string;
  promised_payment_date: string;
  next_action_date: string;
  payment_postponed: boolean;
  comment: string;
};

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

function readInitialToken() {
  window.localStorage.removeItem(RECEIVABLES_TOKEN_LEGACY_KEY);
  return window.sessionStorage.getItem(RECEIVABLES_TOKEN_SESSION_KEY) || "";
}

function getErrorMessage(error: unknown, fallback: string) {
  const status =
    typeof error === "object" && error !== null && "response" in error
      ? (error as { response?: { status?: number } }).response?.status
      : undefined;
  if (status === 401) return "Сессия не принята или истекла. Обновите страницу и откройте витрину заново.";
  if (status === 403) return "Нет доступа к этой витрине или не найдено подразделение для доступа.";
  return error instanceof Error ? error.message : fallback;
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

function initialEdit(item: ReceivableWorkplaceItem): EditState {
  return {
    status: item.status,
    contacted_staff_ref: item.contacted_staff_ref || "",
    promised_payment_date: dateInput(item.promised_payment_date),
    next_action_date: dateInput(item.next_action_date),
    payment_postponed: item.payment_postponed,
    comment: item.comment || "",
  };
}

function rowBadges(item: ReceivableWorkplaceItem) {
  const badges: Array<{ label: string; tone: "danger" | "warning" | "info" }> = [];
  if (item.needs_call_today) badges.push({ label: "звонок сегодня", tone: "info" });
  if (item.no_phone_marker) badges.push({ label: "нет телефона", tone: "danger" });
  if (item.needs_credit_depth_default) badges.push({ label: "7 дней расчетно", tone: "warning" });
  if ((item.effective_overdue_days || 0) >= 90) badges.push({ label: "90+ дней", tone: "danger" });
  else if ((item.effective_overdue_days || 0) >= 30) badges.push({ label: "30+ дней", tone: "warning" });
  return badges;
}

function ReceivableSummary({ summary }: { summary: ReceivableWorkplaceSummary }) {
  const metrics = [
    ["Общая дебиторка", formatMoney(summary.total_receivable)],
    ["Общая просрочка", formatMoney(summary.total_overdue)],
    ["> 30 дней", formatMoney(summary.overdue_over_30_amount)],
    ["> 90 дней", formatMoney(summary.overdue_over_90_amount)],
    ["Позвонить сегодня", formatMoney(summary.need_call_today_amount)],
    ["Без телефона", String(summary.no_phone_count)],
    ["7 дней расчетно", String(summary.credit_depth_default_count)],
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
  saving,
  edit,
  onToggle,
  onEdit,
  onSave,
}: {
  item: ReceivableWorkplaceItem;
  index: number;
  statusOptions: ReceivableStatusOption[];
  expanded: boolean;
  saving: boolean;
  edit: EditState;
  onToggle: () => void;
  onEdit: (patch: Partial<EditState>) => void;
  onSave: () => void;
}) {
  const isPyatigorsk = (item.department_name || "").toLocaleLowerCase("ru-RU").includes("пятигор");
  const rowStatusOptions = statusOptions.filter(
    (option) => option.scope === "common" || (option.scope === "pyatigorsk" && isPyatigorsk)
  );
  const badges = rowBadges(item);
  if (!rowStatusOptions.some((option) => option.value === edit.status)) {
    rowStatusOptions.push({ value: edit.status, label: edit.status, scope: "custom" });
  }
  return (
    <>
      <tr className={item.no_phone_marker ? "receivables__row receivables__row--alert" : "receivables__row"}>
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
        <td>{formatDate(item.oldest_overdue_date)}</td>
        <td>
          <input
            className="receivables__input"
            type="date"
            value={edit.promised_payment_date}
            onChange={(event) => onEdit({ promised_payment_date: event.target.value })}
          />
        </td>
        <td>{formatDate(item.last_contact_at)}</td>
        <td>
          <select
            className="receivables__select"
            value={edit.contacted_staff_ref}
            onChange={(event) => onEdit({ contacted_staff_ref: event.target.value })}
          >
            <option value="">Не выбран</option>
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
          <input
            checked={edit.payment_postponed}
            onChange={(event) => onEdit({ payment_postponed: event.target.checked })}
            type="checkbox"
          />
        </td>
        <td>
          <textarea
            className="receivables__comment"
            value={edit.comment}
            onChange={(event) => onEdit({ comment: event.target.value })}
          />
        </td>
        <td>
          <button className="btn" disabled={saving} onClick={onSave} type="button">
            {saving ? "..." : "Сохранить"}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="receivables__details">
          <td colSpan={16}>
            <div className="receivables__documents">
              <div className="receivables__documents-head">
                <strong>Накладные</strong>
                <span>
                  Всего: {item.invoice_count}, просроченных: {item.overdue_invoice_count}
                </span>
                {item.needs_credit_depth_default && <mark>глубина кредита расчетно 7 дней</mark>}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Номер</th>
                    <th>Дата</th>
                    <th>Сумма</th>
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
}: {
  items: CounterpartyFolderRecommendation[];
  loading: boolean;
}) {
  if (loading) return <div className="receivables__state">Загрузка вкладки контроля папок...</div>;
  if (!items.length) return <div className="receivables__state">По текущей дате рекомендаций нет.</div>;
  return (
    <section className="receivables__folder-tab">
      <table>
        <thead>
          <tr>
            <th>Код 1С</th>
            <th>Клиент</th>
            <th>Текущая папка</th>
            <th>Рекомендованная папка</th>
            <th>Подразделение долга</th>
            <th>Долг</th>
            <th>Статус</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.counterparty_ref}>
              <td className="mono">{item.counterparty_code || ""}</td>
              <td>{item.counterparty_name || item.counterparty_ref}</td>
              <td>{item.current_folder_name || ""}</td>
              <td>{item.recommended_folder_name || ""}</td>
              <td>{item.debt_department_name || ""}</td>
              <td>{formatMoney(item.current_balance)}</td>
              <td>{item.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function ReceivablesWorkplace({
  bitrixMode = false,
  bitrixUserName,
  accessLevel = "full",
}: ReceivablesWorkplaceProps) {
  const [token, setToken] = useState(() => (bitrixMode ? "" : readInitialToken()));
  const [date, setDate] = useState(todayIso());
  const [departmentRef, setDepartmentRef] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [items, setItems] = useState<ReceivableWorkplaceItem[]>([]);
  const [summary, setSummary] = useState<ReceivableWorkplaceSummary>(emptySummary);
  const [statusOptions, setStatusOptions] = useState<ReceivableStatusOption[]>([]);
  const [edits, setEdits] = useState<Record<string, EditState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [saving, setSaving] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [tab, setTab] = useState<"work" | "folders">("work");
  const [folderItems, setFolderItems] = useState<CounterpartyFolderRecommendation[]>([]);
  const [foldersLoading, setFoldersLoading] = useState(false);
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

  const departments = useMemo(() => {
    const byRef = new Map<string, string>();
    items.forEach((item) => {
      if (item.department_ref) byRef.set(item.department_ref, item.department_name || item.department_ref);
    });
    return [...byRef.entries()].sort((a, b) => a[1].localeCompare(b[1], "ru"));
  }, [items]);

  const loadWorkplace = useCallback(async () => {
    if (!hasToken) {
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
        status: statusFilter,
      });
      setItems(data.payload);
      setSummary(data.summary);
      setStatusOptions(data.status_options);
      setEdits(Object.fromEntries(data.payload.map((item) => [item.counterparty_ref, initialEdit(item)])));
    } catch (error: unknown) {
      setMessage(getErrorMessage(error, "Не удалось загрузить дебиторку"));
    } finally {
      setLoading(false);
    }
  }, [date, departmentRef, hasToken, statusFilter]);

  const loadFolders = useCallback(async () => {
    if (!hasToken) {
      setFolderItems([]);
      return;
    }
    setFoldersLoading(true);
    try {
      const data = await fetchCounterpartyFolderRecommendations(date);
      setFolderItems(data.payload);
    } catch (error: unknown) {
      setMessage(getErrorMessage(error, "Не удалось загрузить контроль папок"));
    } finally {
      setFoldersLoading(false);
    }
  }, [date, hasToken]);

  useEffect(() => {
    void loadWorkplace();
  }, [loadWorkplace]);

  useEffect(() => {
    if (tab === "folders") void loadFolders();
  }, [loadFolders, tab]);

  const saveItem = async (item: ReceivableWorkplaceItem) => {
    const edit = edits[item.counterparty_ref] || initialEdit(item);
    setSaving((prev) => new Set(prev).add(item.counterparty_ref));
    const staff = item.staff_options.find((option) => option.staff_ref === edit.contacted_staff_ref);
    try {
      const response = await updateReceivableWorkplaceItem(date, item.counterparty_ref, {
        status: edit.status,
        contacted_staff_ref: edit.contacted_staff_ref || null,
        contacted_staff_name: staff?.staff_name || null,
        promised_payment_date: edit.promised_payment_date || null,
        next_action_date: edit.next_action_date || null,
        payment_postponed: edit.payment_postponed,
        comment: edit.comment,
      });
      setItems((prev) =>
        prev.map((row) => (row.counterparty_ref === item.counterparty_ref ? response.item : row))
      );
      setEdits((prev) => ({ ...prev, [item.counterparty_ref]: initialEdit(response.item) }));
      toast.success("Сохранено");
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : "Не удалось сохранить");
    } finally {
      setSaving((prev) => {
        const next = new Set(prev);
        next.delete(item.counterparty_ref);
        return next;
      });
    }
  };

  return (
    <div className="app receivables">
      <header className="app__header receivables__header">
        <h1>Дебиторка покупателей</h1>
        {bitrixMode && bitrixUserName && <span className="app__user">{bitrixUserName}</span>}
        <input className="app__search" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
        <select className="app__select" value={departmentRef} onChange={(event) => setDepartmentRef(event.target.value)}>
          <option value="">{accessLevel === "full" ? "Все подразделения" : "Все доступные подразделения"}</option>
          {departments.map(([ref, name]) => (
            <option key={ref} value={ref}>
              {name}
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
      {!hasToken && <div className="receivables__auth-state">Введите внутренний токен, чтобы открыть витрину.</div>}
      {hasToken && <ReceivableSummary summary={summary} />}
      <nav className="receivables__tabs">
        <button className={tab === "work" ? "btn" : "btn btn--ghost"} onClick={() => setTab("work")} type="button">
          Рабочий список
        </button>
        <button className={tab === "folders" ? "btn" : "btn btn--ghost"} onClick={() => setTab("folders")} type="button">
          Контроль папок
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
                  <th>Старая просрочка</th>
                  <th>Обещанная дата</th>
                  <th>Последний контакт</th>
                  <th>Кто общался</th>
                  <th>Статус</th>
                  <th>Следующий контакт</th>
                  <th>Перенес</th>
                  <th>Комментарий</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, index) => (
                  <ReceivableRow
                    key={item.counterparty_ref}
                    edit={edits[item.counterparty_ref] || initialEdit(item)}
                    expanded={expanded.has(item.counterparty_ref)}
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
          {!loading && !items.length && <div className="receivables__state">На выбранную дату строк нет.</div>}
        </section>
      )}
      {hasToken && tab === "folders" && <FolderRecommendations items={folderItems} loading={foldersLoading} />}
      <footer className="receivables__legend">
        <span>Красная строка: нет телефона.</span>
        <span>Желтая метка: расчетный срок 7 дней или просрочка 30+.</span>
        <span>Кнопка + раскрывает накладные клиента.</span>
      </footer>
    </div>
  );
}
