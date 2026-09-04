import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  approveProcurementClassification,
  approveProcurementLifecycleTransitions,
  decideProcurementLifecycleTransition,
  fetchProcurementClassifications,
  fetchProcurementDashboard,
  fetchProcurementEvents,
  fetchProcurementLifecycleTransitions,
  fetchProcurementOrder,
  fetchProcurementOrderFormation,
  fetchProcurementOrders,
  exportProcurementOrdersExcel,
  type ProcurementClassificationQueue,
  type ProcurementDashboard,
  type ProcurementEventList,
  type ProcurementLifecycleApprovalResponse,
  type ProcurementLifecycleTransition,
  type ProcurementLifecycleTransitionList,
  type ProcurementOrderFormation,
  type ProcurementOrderList,
  type ProcurementOrderListItem,
} from "../api/procurementAssortment";
import {
  openBitrixProcurementProcess,
  resolveBitrixPortalUrl,
  resolveBitrixProductUrl,
} from "../api/bitrix";
import { procurementErrorText } from "../utils/procurementErrorMessages";
import {
  procurementBlockerSummaryLabel,
  procurementRiskLabel,
} from "../utils/procurementRiskLabels";
import { ProcurementOrderAssistant } from "./ProcurementOrderAssistant";
import { ProcurementFamilyReview } from "./ProcurementFamilyReview";
import { ProcurementOrderFormationApp } from "./ProcurementOrderFormationApp";

interface Props {
  bitrixUserName?: string | null;
  bitrixItemId?: string;
}

type WorkspaceTab = "dashboard" | "assistant" | "orders" | "properties" | "history";
const LIFECYCLE_READINESS = ["all", "ready", "review", "blocked", "stale"] as const;
type LifecycleReadiness = (typeof LIFECYCLE_READINESS)[number];

type OrderRegistrySource = "" | "generated" | "onec_import";
type OrderRegistryBlockers = "all" | "with" | "without";

interface OrderRegistryViewState {
  page: number;
  search: string;
  lifecycleStatus: string;
  supplier: string;
  contour: string;
  onecNumber: string;
  dateFrom: string;
  dateTo: string;
  source: OrderRegistrySource;
  blockers: OrderRegistryBlockers;
}

const ORDER_REGISTRY_VIEW_STATE_KEY = "pricing.procurement.orders-registry-view.v1";
const DEFAULT_ORDER_REGISTRY_VIEW_STATE: OrderRegistryViewState = {
  page: 1,
  search: "",
  lifecycleStatus: "",
  supplier: "",
  contour: "",
  onecNumber: "",
  dateFrom: "",
  dateTo: "",
  source: "",
  blockers: "all",
};

function readOrderRegistryViewState(): OrderRegistryViewState {
  try {
    const raw = window.sessionStorage.getItem(ORDER_REGISTRY_VIEW_STATE_KEY);
    if (!raw) return DEFAULT_ORDER_REGISTRY_VIEW_STATE;
    const value = JSON.parse(raw) as Partial<OrderRegistryViewState>;
    const source = ["", "generated", "onec_import"].includes(String(value.source || ""))
      ? String(value.source || "") as OrderRegistrySource
      : "";
    const blockers = ["all", "with", "without"].includes(String(value.blockers || ""))
      ? String(value.blockers) as OrderRegistryBlockers
      : "all";
    return {
      page: Number.isInteger(value.page) && Number(value.page) > 0 ? Number(value.page) : 1,
      search: typeof value.search === "string" ? value.search : "",
      lifecycleStatus: typeof value.lifecycleStatus === "string" ? value.lifecycleStatus : "",
      supplier: typeof value.supplier === "string" ? value.supplier : "",
      contour: typeof value.contour === "string" ? value.contour : "",
      onecNumber: typeof value.onecNumber === "string" ? value.onecNumber : "",
      dateFrom: typeof value.dateFrom === "string" ? value.dateFrom : "",
      dateTo: typeof value.dateTo === "string" ? value.dateTo : "",
      source,
      blockers,
    };
  } catch {
    return DEFAULT_ORDER_REGISTRY_VIEW_STATE;
  }
}

type WorkspaceRoute =
  | { kind: "tab"; tab: WorkspaceTab }
  | {
      kind: "lifecycle";
      status: string;
      scope: "action" | "all";
      readiness: LifecycleReadiness;
      proposalId?: number;
    }
  | { kind: "order"; orderId: number; focusLineId?: number }
  | { kind: "review"; nomenclatureCode: string };

const TAB_LABELS: Record<WorkspaceTab, string> = {
  dashboard: "Витрина",
  assistant: "Помощник",
  orders: "Заказы",
  properties: "Свойства",
  history: "История",
};

// Названия статусов действующие; прежние держим рядом и показываем мелкой
// строкой (решение пользователя 2026-08-19), чтобы витрина сходилась со
// старыми отчётами и устными договорённостями.
const MANUAL_STATUS_LABELS: Record<string, string> = {
  matrix: "Держим всегда",
  on_demand: "Только под заказ",
  replace_candidate: "Меняем на аналог",
  nonliquid: "Выводим",
  do_not_order: "Не закупаем",
  pension: "Допродаём",
  review: "Разбор",
};

const MANUAL_STATUS_LEGACY_LABELS: Record<string, string> = {
  matrix: "Матричный",
  on_demand: "Под заказ",
  replace_candidate: "Кандидат на замену",
  nonliquid: "Кандидат на неликвид",
  do_not_order: "Не закупать",
  pension: "Пенсия",
  review: "Review / разбор",
};

const ORDER_STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  review: "На проверке",
  blocked: "Заблокирован",
  transmitting: "Передаётся в 1С",
  active: "Активен",
  in_transit: "В пути",
  partially_received: "Частично поступил",
  received: "Поступил",
  cancelled: "Отменён",
};

export function OrderBlockerCell({ order }: { order: ProcurementOrderList["items"][number] }) {
  const blockerCount = order.blockers?.length || 0;
  if (!blockerCount) return null;

  const products = order.blocked_products || [];
  const label = procurementBlockerSummaryLabel(order.blockers);
  if (products.length === 1) {
    const product = products[0];
    const href = resolveBitrixPortalUrl(product.bitrix_url)
      || resolveBitrixProductUrl(product.bitrix_product_id);
    return href ? (
      <a
        className="state-pill state-pill--blocked order-registry__blocker-pill"
        href={href}
        rel="noreferrer"
        target="_blank"
      >
        {label}
      </a>
    ) : (
      <span className="state-pill state-pill--blocked order-registry__blocker-pill">{label}</span>
    );
  }
  if (products.length > 1) {
    return (
      <details className="order-registry__blocker-products">
        <summary className="state-pill state-pill--blocked order-registry__blocker-pill">
          {label}
        </summary>
        <div>
          {products.map((product) => {
            const href = resolveBitrixPortalUrl(product.bitrix_url)
              || resolveBitrixProductUrl(product.bitrix_product_id);
            return href ? (
              <a href={href} key={product.line_id} rel="noreferrer" target="_blank">
                <strong>{product.name}</strong>
                <small>{product.blocker_count} блокер(а) · строка {product.line_number}</small>
              </a>
            ) : (
              <span key={product.line_id}><strong>{product.name}</strong></span>
            );
          })}
        </div>
      </details>
    );
  }
  return (
    <span className="state-pill state-pill--blocked order-registry__blocker-pill">{label}</span>
  );
}

const ORDER_CONTOUR_LABELS: Record<string, string> = {
  ordinary: "Обычный",
  cargo: "Карго",
  ved_import: "ВЭД импорт",
};

const LIFECYCLE_STATUS_LABELS: Record<string, string> = {
  fruit: "Рассматриваем",
  newborn: "Заказали",
  newborn_need: "Добираем",
  new_item: "Завезли",
  sales_start: "Пошли продажи",
  sale: "Растим",
  working: "Поддерживаем",
};

const LIFECYCLE_LEGACY_LABELS: Record<string, string> = {
  fruit: "Плод",
  newborn: "Новорожденный",
  newborn_need: "ДН / Добор новорождённого",
  new_item: "Новинка",
  sales_start: "СП / Старт продаж",
  sale: "ПРОДАЖА",
  working: "Рабочий",
};

function statusLegacyLabel(status: string) {
  return LIFECYCLE_LEGACY_LABELS[status] || MANUAL_STATUS_LEGACY_LABELS[status] || "";
}

function statusScreenLabel(status: string) {
  const label = LIFECYCLE_STATUS_LABELS[status] || MANUAL_STATUS_LABELS[status] || status;
  const legacy = statusLegacyLabel(status);
  return legacy && legacy !== label ? `${label} (${legacy})` : label;
}

const LIFECYCLE_FACT_LABELS: Record<string, string> = {
  customer_order_count_1c: "Заказов покупателей",
  customer_order_qty_1c: "Количество в заказах",
  supplier_order_count_1c: "Заказов поставщику",
  supplier_order_qty_1c: "Количество у поставщика",
  cargo_handoff_count_1c: "Передач в груз",
  card_created_at_1c: "Карточка создана",
  model_birth_date: "Модель на рынке с",
  family_member_count: "Доступных SKU в сегменте",
};

// Статусы предложения приходят кодом; на экране закупщику нужен русский текст.
const PROPOSAL_STATUS_LABELS: Record<string, string> = {
  proposed: "Ожидает решения",
  approved: "Утверждено, готов dry-run",
  sent_to_1c: "Передано в 1С",
  applied: "Применено в 1С",
  reflected: "Проверено в каталоге",
  rejected: "Отклонено",
  conflict: "Конфликт при проверке",
  stale: "Устарело",
};

function proposalStatusLabel(status: string) {
  return PROPOSAL_STATUS_LABELS[status] || status;
}

const EVENT_LABELS: Record<string, string> = {
  order_conditions_changed: "Изменены условия заказа",
  order_line_changed: "Изменена строка заказа",
  order_line_removed: "Строка исключена из заказа",
  order_checked_and_sent: "Заказ проверен и отправлен",
  classification_proposed: "Предложено изменение свойства",
  classification_approved: "Изменение свойства утверждено",
  lifecycle_transitions_approved: "Утверждён пакет переходов",
  lifecycle_manual_decision: "Принято ручное решение по статусу",
  lifecycle_transition_auto_applied: "Жизненный статус изменён автоматически",
  assistant_order_assembled: "Проект заказа собран помощником",
};

function errorText(error: unknown) {
  return procurementErrorText(error);
}

function money(value: string | number, currency = "RUB") {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(numeric);
}

function number(value: string | number) {
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 }).format(numeric)
    : String(value);
}

function dateTime(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}

function dateOnly(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(`${value.slice(0, 10)}T00:00:00`);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("ru-RU").format(parsed);
}

function lifecycleFactLabel(key: string) {
  return LIFECYCLE_FACT_LABELS[key] || key.replaceAll("_", " ");
}

function lifecycleFactValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  return String(value);
}

function routeFromLocation(): WorkspaceRoute {
  const root = "/bitrix/procurement-order-formation";
  const path = window.location.pathname.replace(/\/+$/, "");
  const relative = path.startsWith(root) ? path.slice(root.length) : "";
  const lifecycleMatch = relative.match(/^\/lifecycle\/([^/]+)$/);
  if (lifecycleMatch) {
    const params = new URLSearchParams(window.location.search);
    const readinessParam = params.get("readiness");
    const proposalId = Number(params.get("proposal"));
    return {
      kind: "lifecycle",
      status: decodeURIComponent(lifecycleMatch[1]),
      scope: params.get("scope") === "all" ? "all" : "action",
      readiness: LIFECYCLE_READINESS.includes(readinessParam as LifecycleReadiness)
        ? readinessParam as LifecycleReadiness
        : "all",
      proposalId: Number.isInteger(proposalId) && proposalId > 0 ? proposalId : undefined,
    };
  }
  const orderMatch = relative.match(/^\/orders\/(\d+)$/);
  if (orderMatch) {
    const lineId = Number(new URLSearchParams(window.location.search).get("line"));
    return {
      kind: "order",
      orderId: Number(orderMatch[1]),
      focusLineId: Number.isInteger(lineId) && lineId > 0 ? lineId : undefined,
    };
  }
  const reviewMatch = relative.match(/^\/review\/([^/]+)$/);
  if (reviewMatch) {
    return { kind: "review", nomenclatureCode: decodeURIComponent(reviewMatch[1]) };
  }
  if (relative === "/assistant") return { kind: "tab", tab: "assistant" };
  if (relative === "/orders") return { kind: "tab", tab: "orders" };
  if (relative === "/properties") return { kind: "tab", tab: "properties" };
  if (relative === "/history") return { kind: "tab", tab: "history" };
  return { kind: "tab", tab: "dashboard" };
}

function routeUrl(route: WorkspaceRoute) {
  const root = "/bitrix/procurement-order-formation";
  if (route.kind === "lifecycle") {
    const params = new URLSearchParams({ scope: route.scope });
    if (route.readiness !== "all") params.set("readiness", route.readiness);
    if (route.proposalId) params.set("proposal", String(route.proposalId));
    return `${root}/lifecycle/${encodeURIComponent(route.status)}?${params}`;
  }
  if (route.kind === "order") {
    const suffix = route.focusLineId ? `?line=${route.focusLineId}` : "";
    return `${root}/orders/${route.orderId}${suffix}`;
  }
  if (route.kind === "review") {
    return `${root}/review/${encodeURIComponent(route.nomenclatureCode)}`;
  }
  if (route.tab === "dashboard") return root;
  return `${root}/${route.tab}`;
}

function AppShell({
  bitrixUserName,
  activeTab,
  onNavigate,
  children,
}: Props & {
  activeTab: WorkspaceTab;
  onNavigate: (route: WorkspaceRoute) => void;
  children: React.ReactNode;
}) {
  const today = new Date();
  const todayLabel = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(today);
  return (
    <div className="order-workspace">
      <header className={`order-workspace__header${activeTab === "assistant" ? " order-workspace__header--assistant" : ""}`}>
        <div>
          <h1>Формирование заказа</h1>
          <p>Дисплеи · ответственный Омар · данные и факты из 1С</p>
        </div>
        <nav aria-label="Разделы формирования заказа" className="order-workspace__tabs">
          {(Object.keys(TAB_LABELS) as WorkspaceTab[]).map((tab) => (
            <button
              aria-current={activeTab === tab ? "page" : undefined}
              className={activeTab === tab ? "is-active" : ""}
              key={tab}
              onClick={() => onNavigate({ kind: "tab", tab })}
              type="button"
            >
              {TAB_LABELS[tab]}
            </button>
          ))}
        </nav>
        {activeTab === "assistant" ? (
          <div className="order-workspace__assistant-meta">
            <time dateTime={today.toISOString().slice(0, 10)}>{todayLabel}</time>
            {bitrixUserName && <span className="order-workspace__user">{bitrixUserName}</span>}
          </div>
        ) : bitrixUserName ? (
          <span className="order-workspace__user">{bitrixUserName}</span>
        ) : null}
      </header>
      {children}
    </div>
  );
}

function LoadingState({ message = "Загрузка..." }: { message?: string }) {
  return (
    <div aria-busy="true" aria-label={message} className="order-workspace__state order-workspace__state--loading">
      <span />
      <span />
      <span />
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="order-workspace__state order-workspace__state--error">
      <strong>Не удалось загрузить данные</strong>
      <span>{message}</span>
      <button className="btn btn--ghost" onClick={onRetry} type="button">
        Повторить
      </button>
    </div>
  );
}

function ProcessLinkState({
  state,
  error,
  onBack,
}: {
  state: "pending" | "broken";
  error?: string | null;
  onBack: () => void;
}) {
  return (
    <div className={`order-workspace__state order-workspace__state--${state === "broken" ? "error" : "loading"}`}>
      <strong>{state === "pending" ? "Карточка создаётся…" : "Связь требует восстановления"}</strong>
      <span>{state === "pending"
        ? "Заказ создан в 1С. Плановая синхронизация повторит связь автоматически."
        : error || "Связь с процессом подтверждённо нарушена."}</span>
      <button className="btn btn--ghost" onClick={onBack} type="button">Вернуться к заказам</button>
    </div>
  );
}

type DashboardSort = "overdue" | "unblocked" | "responsible";
const DASHBOARD_STATE_KEY = "pricing.procurement.dashboard-view.v2";

function readDashboardState() {
  try {
    const raw = window.sessionStorage.getItem(DASHBOARD_STATE_KEY);
    const value = raw ? JSON.parse(raw) as Record<string, unknown> : {};
    return {
      manualFilter: typeof value.manualFilter === "string" ? value.manualFilter : null,
      search: typeof value.search === "string" ? value.search : "",
      sort: ["overdue", "unblocked", "responsible"].includes(String(value.sort))
        ? value.sort as DashboardSort : "overdue" as DashboardSort,
      focusedCode: typeof value.focusedCode === "string" ? value.focusedCode : "",
      selectedCodes: Array.isArray(value.selectedCodes)
        ? value.selectedCodes.filter((item): item is string => typeof item === "string") : [],
      scrollY: Number(value.scrollY) || 0,
    };
  } catch {
    return { manualFilter: null, search: "", sort: "overdue" as DashboardSort, focusedCode: "", selectedCodes: [] as string[], scrollY: 0 };
  }
}

function Dashboard({
  data,
  onOpenLifecycle,
  onOpenReview,
  onRefresh,
}: {
  data: ProcurementDashboard;
  onOpenLifecycle: (
    status: string,
    scope: "action" | "all",
    options?: { readiness?: LifecycleReadiness; proposalId?: number }
  ) => void;
  onOpenReview: (nomenclatureCode: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const initialView = useMemo(readDashboardState, []);
  const [manualFilter, setManualFilter] = useState<string | null>(initialView.manualFilter);
  const [search, setSearch] = useState(initialView.search);
  const [sort, setSort] = useState<DashboardSort>(initialView.sort);
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(new Set(initialView.selectedCodes));
  const [focusedCode, setFocusedCode] = useState(initialView.focusedCode);
  const [approving, setApproving] = useState(false);
  const manualFilterLabel = manualFilter ? MANUAL_STATUS_LABELS[manualFilter] : null;
  const unfilteredRows = manualFilter
    ? data.manual_attention.filter((item) => item.filter_status === manualFilter)
    : data.attention;
  const attentionRows = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("ru-RU");
    const rows = unfilteredRows.filter((item) => !query || [
      item.product_name, item.nomenclature_code, item.reason, item.recommendation,
    ].some((value) => String(value || "").toLocaleLowerCase("ru-RU").includes(query)));
    return [...rows].sort((left, right) => {
      if (sort === "unblocked") {
        const leftBlocked = left.decision_state === "blocked" ? 1 : 0;
        const rightBlocked = right.decision_state === "blocked" ? 1 : 0;
        return leftBlocked - rightBlocked || left.product_name.localeCompare(right.product_name, "ru");
      }
      if (sort === "responsible") {
        return String(left.responsible_name || data.responsible_name).localeCompare(String(right.responsible_name || data.responsible_name), "ru")
          || left.product_name.localeCompare(right.product_name, "ru");
      }
      const score = (item: typeof left) => item.overdue ? 0 : item.urgency === "blocked" ? 1 : 2;
      return score(left) - score(right) || left.product_name.localeCompare(right.product_name, "ru");
    });
  }, [data.responsible_name, search, sort, unfilteredRows]);
  const openAttention = (item: ProcurementDashboard["attention"][number]) => {
    if (item.kind !== "lifecycle" || !item.proposal_id) return;
    onOpenLifecycle(item.current_status, "action", {
      readiness: item.decision_state as LifecycleReadiness,
      proposalId: item.proposal_id,
    });
  };
  const canOpenReview = (item: ProcurementDashboard["attention"][number]) =>
    item.filter_status === "review"
    || item.decision_state === "review"
    || item.action_label === "Открыть разбор";

  const openRow = useCallback((item: ProcurementDashboard["attention"][number]) => {
    if (item.filter_status === "review" || item.decision_state === "review" || item.action_label === "Открыть разбор") {
      onOpenReview(item.nomenclature_code);
    } else if (item.kind === "lifecycle" && item.proposal_id) {
      onOpenLifecycle(item.current_status, "action", {
        readiness: item.decision_state as LifecycleReadiness,
        proposalId: item.proposal_id,
      });
    }
  }, [onOpenLifecycle, onOpenReview]);

  const approveSelected = useCallback(async () => {
    const candidates = attentionRows.filter((item) =>
      selectedCodes.has(item.nomenclature_code)
      && item.kind === "lifecycle"
      && item.proposal_id
      && item.decision_state === "ready"
    );
    if (!candidates.length || approving) return;
    setApproving(true);
    try {
      const details = await Promise.all(candidates.map((item) => fetchProcurementLifecycleTransitions({
        status: item.current_status,
        scope: "action",
        readiness: "ready",
        proposal_id: Number(item.proposal_id),
        page_size: 1,
      })));
      const rows = details.flatMap((item) => item.items).filter((item) => item.selectable && item.ready);
      if (!rows.length) throw new Error("Выбранные строки уже изменились. Обновите Витрину.");
      const response = await approveProcurementLifecycleTransitions(rows);
      toast.success(`Подтверждено: ${response.summary.approved}`);
      setSelectedCodes(new Set());
      await onRefresh();
    } catch (requestError) {
      toast.error(procurementErrorText(requestError));
    } finally {
      setApproving(false);
    }
  }, [approving, attentionRows, onRefresh, selectedCodes]);

  useEffect(() => {
    window.sessionStorage.setItem(DASHBOARD_STATE_KEY, JSON.stringify({
      manualFilter, search, sort, focusedCode, selectedCodes: [...selectedCodes], scrollY: window.scrollY,
    }));
  }, [focusedCode, manualFilter, search, selectedCodes, sort]);

  useEffect(() => {
    const restore = window.setTimeout(() => window.scrollTo({ top: initialView.scrollY }), 0);
    const remember = () => {
      const current = readDashboardState();
      window.sessionStorage.setItem(DASHBOARD_STATE_KEY, JSON.stringify({ ...current, scrollY: window.scrollY }));
    };
    window.addEventListener("scroll", remember, { passive: true });
    return () => { window.clearTimeout(restore); window.removeEventListener("scroll", remember); };
  }, [initialView.scrollY]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, button, a")) return;
      const currentIndex = Math.max(0, attentionRows.findIndex((item) => item.nomenclature_code === focusedCode));
      if ((event.key === "j" || event.key === "J") && attentionRows.length) {
        event.preventDefault();
        setFocusedCode(attentionRows[Math.min(attentionRows.length - 1, currentIndex + 1)].nomenclature_code);
      } else if ((event.key === "k" || event.key === "K") && attentionRows.length) {
        event.preventDefault();
        setFocusedCode(attentionRows[Math.max(0, currentIndex - 1)].nomenclature_code);
      } else if (event.code === "Space" && focusedCode) {
        event.preventDefault();
        setSelectedCodes((current) => {
          const next = new Set(current);
          if (next.has(focusedCode)) next.delete(focusedCode); else next.add(focusedCode);
          return next;
        });
      } else if (event.key === "Enter" && focusedCode) {
        const item = attentionRows.find((row) => row.nomenclature_code === focusedCode);
        if (item) openRow(item);
      } else if ((event.key === "a" || event.key === "A") && selectedCodes.size) {
        event.preventDefault();
        void approveSelected();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [approveSelected, attentionRows, focusedCode, openRow, selectedCodes.size]);
  return (
    <main className="order-workspace__content procurement-dashboard">
      <section className="order-workspace__section-heading">
        <div>
          <h2>Жизненные статусы</h2>
          <p>Общее количество открывает все товары; кнопка решения показывает куда и сколько.</p>
        </div>
        <span>Расчёт от {dateTime(data.updated_at)}</span>
      </section>
      <section className="lifecycle-cards">
        {data.cards.map((card) => {
          const breakdown = Object.entries(card.action_breakdown || {});
          const legacyLabel = card.legacy_label || statusLegacyLabel(card.status);
          return (
          <article className={`lifecycle-card lifecycle-card--${card.urgency}`} key={card.status}>
            <button
              className="lifecycle-card__total"
              onClick={() => onOpenLifecycle(card.status, "all")}
              type="button"
            >
              <span>{card.label}</span>
              {legacyLabel && <small className="lifecycle-card__legacy">раньше: {legacyLabel}</small>}
              <strong>{card.total_count}</strong>
            </button>
            <button
              className="lifecycle-card__action"
              disabled={card.action_count === 0}
              onClick={() => onOpenLifecycle(card.status, "action")}
              type="button"
            >
              {card.action_count === 0
                ? "Решений нет"
                : `${card.action_label} · ${card.action_count}`}
            </button>
            {breakdown.length > 1 ? (
              <div className="lifecycle-card__targets" aria-label="Направления решений">
                {breakdown.map(([target, count]) => (
                  <span key={target}>
                    {target === "review" ? "Разбор" : `→ ${LIFECYCLE_STATUS_LABELS[target] || target}`} {count}
                  </span>
                ))}
              </div>
            ) : null}
            {card.blocked_count > 0 ? (
              <small className="lifecycle-card__blockers">С блокерами: {card.blocked_count}</small>
            ) : null}
          </article>
          );
        })}
      </section>

      <section className="order-workspace__section-heading order-workspace__section-heading--manual">
        <div>
          <h2>Ручные статусы и контроль</h2>
          <p>Нажмите карточку, чтобы отфильтровать список ниже.</p>
        </div>
      </section>
      <section className="manual-status-grid">
        {Object.entries(MANUAL_STATUS_LABELS).map(([status, label]) => (
          <button
            aria-label={`Показать товары: ${label}`}
            aria-pressed={manualFilter === status}
            className={`manual-status-card manual-status-card--${status}${manualFilter === status ? " is-active" : ""}`}
            disabled={(data.manual_status_counts[status] || 0) === 0}
            key={status}
            onClick={() => setManualFilter((current) => current === status ? null : status)}
            type="button"
          >
            <span>
              {label}
              {statusLegacyLabel(status) && (
                <small className="manual-status-card__legacy">раньше: {statusLegacyLabel(status)}</small>
              )}
            </span>
            <strong>{data.manual_status_counts[status] || 0}</strong>
          </button>
        ))}
      </section>

      <section className="attention-panel">
        <div className="order-workspace__section-heading">
          <div>
            <h2>{manualFilterLabel ? `Ручной статус: ${manualFilterLabel}` : "Очередь решений"}</h2>
            <p>
              {manualFilterLabel
                ? `Показаны товары выбранного ручного статуса: ${attentionRows.length}.`
                : `Приоритетная очередь: ${attentionRows.length}.`}
            </p>
          </div>
          {manualFilter ? (
            <button className="btn btn--ghost btn--small" onClick={() => setManualFilter(null)} type="button">
              Сбросить фильтр
            </button>
          ) : null}
        </div>
        <div className="attention-toolbar">
          <label>
            <span className="sr-only">Поиск в очереди</span>
            <input onChange={(event) => setSearch(event.target.value)} placeholder="Поиск товара или причины" type="search" value={search} />
          </label>
          <label>
            <span className="sr-only">Сортировка очереди</span>
            <select onChange={(event) => setSort(event.target.value as DashboardSort)} value={sort}>
              <option value="overdue">Сначала просроченные</option>
              <option value="unblocked">Сначала без блокеров</option>
              <option value="responsible">По ответственному</option>
            </select>
          </label>
          <button className="btn btn--small" disabled={approving || selectedCodes.size === 0} onClick={() => void approveSelected()} type="button">
            {approving ? "Подтверждаем…" : `Подтвердить выбранные · ${selectedCodes.size}`}
          </button>
          <small>Клавиши: J/K — строка, Space — выбор, Enter — открыть, A — подтвердить</small>
        </div>
        {!manualFilter ? (
          <section aria-label="Сводка очереди решений" className="attention-summary">
            <button
              className="attention-summary__item attention-summary__item--ready"
              disabled={data.decision_summary.ready_count === 0}
              onClick={() => onOpenLifecycle("all", "action", { readiness: "ready" })}
              type="button"
            >
              <span>Готово к подтверждению</span>
              <strong>{data.decision_summary.ready_count}</strong>
            </button>
            <button
              className="attention-summary__item attention-summary__item--review"
              disabled={data.decision_summary.review_count === 0}
              onClick={() => onOpenLifecycle("all", "action", { readiness: "review" })}
              type="button"
            >
              <span>Нужен разбор</span>
              <strong>{data.decision_summary.review_count}</strong>
            </button>
            <button
              className="attention-summary__item attention-summary__item--blocked"
              disabled={data.decision_summary.blocked_count === 0}
              onClick={() => onOpenLifecycle("all", "action", { readiness: "blocked" })}
              type="button"
            >
              <span>Есть блокеры</span>
              <strong>{data.decision_summary.blocked_count}</strong>
            </button>
          </section>
        ) : null}
        {attentionRows.length === 0 ? (
          <div className="order-workspace__empty">
            {manualFilterLabel ? "В выбранном ручном статусе товаров нет." : "Открытых решений нет."}
          </div>
        ) : (
          <div className="order-workspace__table-wrap">
            <table className="order-workspace__table">
              <thead>
                <tr>
                  <th aria-label="Выбор"></th>
                  <th>Товар</th>
                  <th>Предлагаемое решение</th>
                  <th>Основание</th>
                  <th>Состояние</th>
                  <th aria-label="Действие"></th>
                </tr>
              </thead>
              <tbody>
                {attentionRows.map((item) => (
                  <tr
                    aria-current={focusedCode === item.nomenclature_code ? "true" : undefined}
                    className={`${item.kind === "lifecycle" || canOpenReview(item) ? "attention-row attention-row--clickable" : "attention-row"}${focusedCode === item.nomenclature_code ? " is-focused" : ""}`}
                    key={`${item.kind}-${item.nomenclature_code}-${item.filter_status}`}
                    onClick={canOpenReview(item) ? () => onOpenReview(item.nomenclature_code) : item.kind === "lifecycle" ? () => openAttention(item) : undefined}
                    onFocus={() => setFocusedCode(item.nomenclature_code)}
                    tabIndex={0}
                  >
                    <td onClick={(event) => event.stopPropagation()}>
                      <input aria-label={`Выбрать ${item.product_name}`} checked={selectedCodes.has(item.nomenclature_code)} onChange={() => setSelectedCodes((current) => {
                        const next = new Set(current);
                        if (next.has(item.nomenclature_code)) next.delete(item.nomenclature_code); else next.add(item.nomenclature_code);
                        return next;
                      })} type="checkbox" />
                    </td>
                    <td>
                      <strong>{item.product_name}</strong>
                      <small>{item.nomenclature_code} · {item.current_status_label}</small>
                      <small>Ответственный: {item.responsible_name || data.responsible_name}</small>
                    </td>
                    <td><span className="transition-pill">{item.action_label}</span></td>
                    <td>{item.fact_summary}</td>
                    <td>
                      <span className={`state-pill state-pill--${item.urgency}`}>{item.decision_state_label}</span>
                    </td>
                    <td>
                      {canOpenReview(item) ? (
                        <button className="attention-action" onClick={(event) => { event.stopPropagation(); onOpenReview(item.nomenclature_code); }} type="button">
                          Открыть разбор
                        </button>
                      ) : item.kind === "lifecycle" && item.proposal_id ? (
                        <button
                          className="attention-action"
                          onClick={(event) => {
                            event.stopPropagation();
                            openAttention(item);
                          }}
                          type="button"
                        >
                          Проверить
                        </button>
                      ) : <span>—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}

export function LifecycleQueue({
  status,
  scope,
  initialReadiness,
  proposalId,
  onClose,
}: {
  status: string;
  scope: "action" | "all";
  initialReadiness: LifecycleReadiness;
  proposalId?: number;
  onClose: () => void;
}) {
  const [data, setData] = useState<ProcurementLifecycleTransitionList | null>(null);
  const [search, setSearch] = useState("");
  const [readiness, setReadiness] = useState<LifecycleReadiness>(initialReadiness);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ProcurementLifecycleApprovalResponse | null>(null);
  const [manualProposalId, setManualProposalId] = useState<number | null>(null);
  const [manualDecision, setManualDecision] = useState<"pension" | "working">("pension");
  const [manualReason, setManualReason] = useState("");
  const [manualReplacement, setManualReplacement] = useState("");
  const [manualNoReplacement, setManualNoReplacement] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchProcurementLifecycleTransitions({
        status,
        scope,
        readiness,
        search,
        proposal_id: proposalId,
        page,
        page_size: 50,
      });
      setData(response);
      setSelected(new Set());
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setLoading(false);
    }
  }, [page, proposalId, readiness, scope, search, status]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setReadiness(initialReadiness);
    setPage(1);
  }, [initialReadiness, proposalId, scope, status]);

  const readyRows = useMemo(
    () => (data?.items || []).filter((item) => item.selectable && item.proposal_id),
    [data]
  );
  const selectedRows = useMemo(
    () =>
      (data?.items || []).filter(
        (item) => item.proposal_id && selected.has(item.proposal_id)
      ),
    [data, selected]
  );
  const first = data?.items[0];
  const statusLabel = status === "all"
    ? "Все статусы"
    : LIFECYCLE_STATUS_LABELS[status] || first?.current_status_label || status;
  const title = readiness === "stale"
    ? `${statusLabel}: архив прошлых расчётов`
    : readiness === "review"
      ? `${statusLabel}: ручной разбор`
    : scope === "all"
    ? `${statusLabel}: все товары`
    : status === "working"
      ? "Рабочий: товары на пересмотр"
      : `${statusLabel}: требуется решение`;
  const subtitle = readiness === "stale"
    ? "Старые предложения сохранены для истории и недоступны для утверждения"
    : scope === "all"
    ? "Полный список товаров выбранного жизненного статуса"
    : proposalId
      ? "Открыта точная строка для проверки перед решением"
      : "Товары, требующие ручного решения. Перед подтверждением проверьте факты 1С.";

  const selectReady = () => {
    setSelected(new Set(readyRows.slice(0, 100).map((item) => item.proposal_id as number)));
  };

  const approve = async (rows: ProcurementLifecycleTransition[]) => {
    if (rows.length === 0) return;
    setLoading(true);
    setResult(null);
    try {
      const response = await approveProcurementLifecycleTransitions(rows.slice(0, 100));
      setResult(response);
      const approved = response.summary.approved;
      toast.success(`Обработано: ${approved}`);
      await load();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setLoading(false);
    }
  };

  const saveManualDecision = async (item: ProcurementLifecycleTransition) => {
    if (!manualReason.trim()) return;
    if (manualDecision === "pension" && !manualReplacement.trim() && !manualNoReplacement) return;
    setLoading(true);
    try {
      const response = await decideProcurementLifecycleTransition(item, {
        decision: manualDecision,
        reason: manualReason.trim(),
        replacement_sku_code: manualDecision === "pension" ? manualReplacement.trim() || null : null,
        no_replacement: manualDecision === "pension" && manualNoReplacement,
      });
      toast.success(response.message);
      setManualProposalId(null);
      setManualReason("");
      setManualReplacement("");
      setManualNoReplacement(false);
      await load();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="lifecycle-queue" role="dialog" aria-modal="true" aria-label={title}>
      <header className="lifecycle-queue__header">
        <div>
          <h1>{title}</h1>
          <p>{subtitle} · расчёт {first?.run_key || "—"} · товаров {data?.total ?? "—"}</p>
        </div>
        <button className="lifecycle-queue__close" onClick={onClose} type="button">
          Закрыть
        </button>
      </header>
      <main className="lifecycle-queue__body">
        {scope === "action" ? (
          <section className="queue-explainer">
            <strong>Что здесь?</strong>
            <span>
              Список исключений для быстрой проверки. Однозначные переходы по фактам 1С
              выполняются автоматически и остаются только в истории.
            </span>
          </section>
        ) : null}
        <section className="queue-summary">
          <div><span>Можно подтвердить</span><strong>{data?.ready_count || 0}</strong></div>
          <div><span>Нужен разбор</span><strong>{data?.review_count || 0}</strong></div>
          <div><span>Есть блокеры</span><strong>{data?.blocked_count || 0}</strong></div>
          <div><span>Выбрано</span><strong>{selected.size}</strong></div>
        </section>
        <section className="queue-toolbar">
          <input
            aria-label="Поиск по коду или названию"
            onChange={(event) => { setSearch(event.target.value); setPage(1); }}
            placeholder="Поиск по коду или названию"
            value={search}
          />
          <select
            aria-label="Готовность перехода"
            onChange={(event) => {
              setReadiness(event.target.value as typeof readiness);
              setPage(1);
            }}
            value={readiness}
          >
            <option value="all">Все требующие решения</option>
            <option value="ready">Можно подтвердить</option>
            <option value="review">Нужен разбор</option>
            <option value="blocked">Есть блокеры</option>
            <option value="stale">Архив прошлых расчётов</option>
          </select>
          <button className="btn btn--ghost" disabled={readyRows.length === 0} onClick={selectReady} type="button">
            Выбрать готовые на странице
          </button>
        </section>

        {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
        {loading && !data ? <LoadingState /> : null}
        {data && data.items.length === 0 ? (
          <div className="order-workspace__empty">
            {scope === "action"
              ? readiness === "stale"
                ? "В архиве прошлых расчётов строк нет."
                : proposalId
                  ? "Это предложение больше не актуально. Вернитесь к витрине и выберите текущую строку."
                : "Решений не требуется: переходы выполнены автоматически или подходящих товаров нет."
              : "В выбранном статусе товаров нет."}
          </div>
        ) : null}
        {data && data.items.length > 0 ? (
          <div className="order-workspace__table-wrap">
            <table className="order-workspace__table lifecycle-queue__table">
              <colgroup>
                <col className="lifecycle-queue__col-select" />
                <col className="lifecycle-queue__col-product" />
                <col className="lifecycle-queue__col-transition" />
                <col className="lifecycle-queue__col-reason" />
                <col className="lifecycle-queue__col-facts" />
                <col className="lifecycle-queue__col-risk" />
                <col className="lifecycle-queue__col-action" />
              </colgroup>
              <thead>
                <tr>
                  <th>Выбор</th>
                  <th>Товар</th>
                  <th>Переход</th>
                  <th>Почему</th>
                  <th>Факты 1С</th>
                  <th>Риск</th>
                  <th>Действие</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => {
                  const proposalId = item.proposal_id || 0;
                  const facts = item.facts.evidence as Record<string, unknown> | undefined;
                  const actionability = item.actionability || (
                    item.selectable
                      ? "batch_approve"
                      : item.decision_state === "review"
                        ? "manual_decision"
                        : "blocked"
                  );
                  return (
                    <tr className={!item.selectable && scope === "action" ? "is-blocked" : ""} key={`${proposalId}-${item.nomenclature_code}`}>
                      <td>
                        {actionability === "batch_approve" ? (
                          <input
                            aria-label={`Выбрать ${item.product_name}`}
                            checked={proposalId > 0 && selected.has(proposalId)}
                            disabled={!item.selectable || selected.size >= 100 && !selected.has(proposalId)}
                            onChange={(event) => {
                              setSelected((current) => {
                                const next = new Set(current);
                                if (event.target.checked) next.add(proposalId);
                                else next.delete(proposalId);
                                return next;
                              });
                            }}
                            type="checkbox"
                          />
                        ) : <span className="state-pill state-pill--warning">Отдельно</span>}
                      </td>
                      <td>
                        <strong>{item.product_name}</strong>
                        <small>{item.nomenclature_code} · папка Дисплеи</small>
                        <small>Ответственный: менеджер по закупке</small>
                      </td>
                      <td>
                        <span className="transition-pill">
                          {item.action_kind === "review"
                            ? item.current_status === "newborn"
                              ? statusScreenLabel("newborn_need")
                              : `${statusScreenLabel("working")} → Разбор`
                            : `${item.current_status_label} → ${item.target_status_label}`}
                        </span>
                      </td>
                      <td className="queue-reason">
                        <strong>{item.reason || "Нужна проверка фактов"}</strong>
                        <span>Основание расчёта</span>
                        <small>Расчёт: {item.run_key}</small>
                      </td>
                      <td>
                        <div className="queue-facts">
                          {facts && Object.keys(facts).length > 0
                            ? Object.entries(facts).slice(0, 4).map(([key, value]) => (
                                <div className="queue-facts__item" key={key}>
                                  <span>{lifecycleFactLabel(key)}</span>
                                  <strong>{lifecycleFactValue(value)}</strong>
                                </div>
                              ))
                            : <small>Факты зафиксированы в расчёте</small>}
                        </div>
                      </td>
                      <td>
                        {item.blockers.length > 0 ? (
                          item.blockers.map((blocker) => (
                            <span className="state-pill state-pill--blocked" key={blocker} title={blocker}>
                              {procurementRiskLabel(blocker)}
                            </span>
                          ))
                        ) : item.stale ? (
                          <span className="state-pill state-pill--warning">данные изменились</span>
                        ) : actionability === "manual_decision" ? (
                          <span className="state-pill state-pill--warning">Решение закупщика</span>
                        ) : (
                          <span className="state-pill state-pill--ready">Нет блокеров</span>
                        )}
                      </td>
                      <td>
                        {actionability === "batch_approve" && item.selectable ? (
                          <button className="btn btn--small" disabled={loading} onClick={() => void approve([item])} type="button">
                            Подтвердить
                          </button>
                        ) : actionability === "manual_decision" ? (
                          manualProposalId === proposalId ? (
                            <div className="lifecycle-manual-decision">
                              <label>
                                Решение
                                <select
                                  onChange={(event) => {
                                    setManualDecision(event.target.value as "pension" | "working");
                                    setManualNoReplacement(false);
                                    setManualReplacement("");
                                  }}
                                  value={manualDecision}
                                >
                                  <option value="pension">Перевести в Допродаём</option>
                                  <option value="working">Оставить Рабочим</option>
                                </select>
                              </label>
                              <label>
                                Причина
                                <textarea
                                  onChange={(event) => setManualReason(event.target.value)}
                                  placeholder="Обязательная причина решения"
                                  value={manualReason}
                                />
                              </label>
                              {manualDecision === "pension" && (
                                <>
                                  <label>
                                    Взамен ведём
                                    <input
                                      disabled={manualNoReplacement}
                                      onChange={(event) => setManualReplacement(event.target.value)}
                                      placeholder="Код 1С (РБ...)"
                                      value={manualReplacement}
                                    />
                                  </label>
                                  <label className="lifecycle-manual-decision__check">
                                    <input
                                      checked={manualNoReplacement}
                                      onChange={(event) => {
                                        setManualNoReplacement(event.target.checked);
                                        if (event.target.checked) setManualReplacement("");
                                      }}
                                      type="checkbox"
                                    />
                                    Замены нет: снято с производства
                                  </label>
                                </>
                              )}
                              <div>
                                <button
                                  className="btn btn--small"
                                  disabled={
                                    loading ||
                                    !manualReason.trim() ||
                                    (manualDecision === "pension" &&
                                      !manualReplacement.trim() &&
                                      !manualNoReplacement)
                                  }
                                  onClick={() => void saveManualDecision(item)}
                                  type="button"
                                >
                                  Сохранить решение
                                </button>
                                <button className="btn btn--ghost btn--small" onClick={() => setManualProposalId(null)} type="button">
                                  Отмена
                                </button>
                              </div>
                            </div>
                          ) : (
                            <button
                              className="btn btn--small"
                              disabled={loading}
                              onClick={() => {
                                setManualProposalId(proposalId);
                                setManualDecision((item.suggested_manual_status as "pension" | "working") || "pension");
                                setManualReason("");
                                setManualReplacement("");
                                setManualNoReplacement(false);
                              }}
                              type="button"
                            >
                              Принять решение
                            </button>
                          )
                        ) : (
                          <button className="btn btn--ghost btn--small" disabled type="button">
                            {item.stale ? "Обновить расчёт" : "На разбор"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        {result ? (
          <section className="queue-result" aria-live="polite">
            <strong>Результат операции</strong>
            <span>Утверждено: {result.summary.approved}</span>
            <span>Устарело: {result.summary.stale}</span>
            <span>Заблокировано: {result.summary.blocked}</span>
            <span>Конфликт: {result.summary.conflict}</span>
            <span>Ошибка: {result.summary.failed}</span>
          </section>
        ) : null}
      </main>
      <footer className="lifecycle-queue__footer">
        <div>
          <strong>Выбрано товаров: {selected.size}</strong>
          <small>В пакет попадают только актуальные строки без блокеров. Максимум 100.</small>
        </div>
        <div>
          <button className="btn btn--ghost" disabled={page <= 1 || loading} onClick={() => setPage((current) => current - 1)} type="button">
            Назад
          </button>
          <button className="btn btn--ghost" disabled={!data || page * data.page_size >= data.total || loading} onClick={() => setPage((current) => current + 1)} type="button">
            Вперёд
          </button>
          <button className="btn" disabled={selectedRows.length === 0 || loading} onClick={() => void approve(selectedRows)} type="button">
            Утвердить выбранные: {selectedRows.length}
          </button>
        </div>
      </footer>
    </div>
  );
}

export function OrdersRegistry({ onOpenOrder }: { onOpenOrder: (orderId: number) => void }) {
  const [data, setData] = useState<ProcurementOrderList | null>(null);
  const [viewState, setViewState] = useState<OrderRegistryViewState>(readOrderRegistryViewState);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [debouncedTextFilters, setDebouncedTextFilters] = useState(() => ({
    search: viewState.search,
    supplier: viewState.supplier,
    onecNumber: viewState.onecNumber,
  }));
  const {
    page,
    search,
    lifecycleStatus,
    supplier,
    contour,
    onecNumber,
    dateFrom,
    dateTo,
    source,
    blockers,
  } = viewState;

  const updateFilters = useCallback((patch: Partial<OrderRegistryViewState>) => {
    setViewState((current) => ({ ...current, ...patch, page: 1 }));
  }, []);

  useEffect(() => {
    try {
      window.sessionStorage.setItem(ORDER_REGISTRY_VIEW_STATE_KEY, JSON.stringify(viewState));
    } catch {
      // WebView может запрещать storage; реестр остаётся рабочим без сохранения состояния.
    }
  }, [viewState]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedTextFilters((current) => (
        current.search === search
        && current.supplier === supplier
        && current.onecNumber === onecNumber
          ? current
          : { search, supplier, onecNumber }
      ));
    }, 350);
    return () => window.clearTimeout(timer);
  }, [onecNumber, search, supplier]);

  const filters = useMemo(() => ({
    search: debouncedTextFilters.search || undefined,
    lifecycle_status: lifecycleStatus || undefined,
    supplier: debouncedTextFilters.supplier || undefined,
    contour: contour || undefined,
    onec_number: debouncedTextFilters.onecNumber || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    source: source || undefined,
    blockers,
  }), [blockers, contour, dateFrom, dateTo, debouncedTextFilters, lifecycleStatus, source]);

  const load = useCallback(async () => {
    setError("");
    try {
      setData(await fetchProcurementOrders({ ...filters, page, page_size: 100 }));
    } catch (requestError) {
      setError(errorText(requestError));
    }
  }, [filters, page]);

  useEffect(() => {
    let cancelled = false;
    fetchProcurementOrders({ ...filters, page, page_size: 100 })
      .then((response) => { if (!cancelled) setData(response); })
      .catch((requestError) => { if (!cancelled) setError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [filters, page]);

  const downloadExcel = async () => {
    setDownloading(true);
    try {
      const { blob, filename } = await exportProcurementOrdersExcel({
        ...filters,
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      window.setTimeout(() => {
        link.remove();
        URL.revokeObjectURL(url);
      }, 1000);
      toast.success("Расчёт заказа скачан");
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setDownloading(false);
    }
  };

  const openOrder = async (order: ProcurementOrderListItem) => {
    const processItemId = order.linked_process?.item_id;
    if (order.linked_process?.state === "not_created") {
      onOpenOrder(order.id);
      return;
    }
    if (order.linked_process?.state !== "linked" || !processItemId) return;
    try {
      await openBitrixProcurementProcess(processItemId);
    } catch (requestError) {
      toast.error(errorText(requestError));
    }
  };

  if (error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (!data) return <LoadingState message="Загрузка заказов..." />;
  return (
    <main className="order-workspace__content">
      <section className="registry-summary">
        <div><span>Заказов</span><strong>{data.summary.orders}</strong></div>
        <div><span>Активных</span><strong>{data.summary.by_status.active || 0}</strong></div>
        <div><span>В пути</span><strong>{data.summary.by_status.in_transit || 0}</strong></div>
        <div><span>Частично поступило</span><strong>{data.summary.by_status.partially_received || 0}</strong></div>
        <div><span>Сумма</span><strong>{money(data.summary.amount)}</strong></div>
      </section>
      <section className="registry-toolbar">
        <input onChange={(event) => updateFilters({ search: event.target.value })} placeholder="Товар или общий поиск" value={search} />
        <input onChange={(event) => updateFilters({ supplier: event.target.value })} placeholder="Поставщик" value={supplier} />
        <input onChange={(event) => updateFilters({ onecNumber: event.target.value })} placeholder="Номер 1С" value={onecNumber} />
        <select onChange={(event) => updateFilters({ lifecycleStatus: event.target.value })} value={lifecycleStatus}>
          <option value="">Все статусы</option>
          {Object.entries(ORDER_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select onChange={(event) => updateFilters({ contour: event.target.value })} value={contour}>
          <option value="">Все контуры</option>
          <option value="ordinary">Обычный</option>
          <option value="cargo">Карго</option>
          <option value="ved_import">ВЭД импорт</option>
        </select>
        <select onChange={(event) => updateFilters({ source: event.target.value as OrderRegistrySource })} value={source}>
          <option value="">Все источники</option>
          <option value="generated">Создано в приложении</option>
          <option value="onec_import">Импортировано из 1С</option>
        </select>
        <label>С <input aria-label="Заказы с даты" onChange={(event) => updateFilters({ dateFrom: event.target.value })} type="date" value={dateFrom} /></label>
        <label>По <input aria-label="Заказы по дату" onChange={(event) => updateFilters({ dateTo: event.target.value })} type="date" value={dateTo} /></label>
        <select onChange={(event) => updateFilters({ blockers: event.target.value as OrderRegistryBlockers })} value={blockers}>
          <option value="all">Все проверки</option>
          <option value="without">Без блокеров</option>
          <option value="with">С блокерами</option>
        </select>
        <button className="btn btn--ghost" disabled={downloading || data.total === 0} onClick={() => void downloadExcel()} type="button">
          {downloading ? "Скачивание..." : "Скачать Excel"}
        </button>
      </section>
      {data.items.length === 0 ? <div className="order-workspace__empty">Заказы не сформированы.</div> : (
        <div className="order-workspace__table-wrap">
          <table className="order-workspace__table order-registry__table">
            <thead><tr><th>Заказ 1С</th><th>Поставщик / условия</th><th>Статус</th><th>Заказано / поступило / открыто</th><th>Ожидаем</th><th>Сумма</th><th>Действие</th></tr></thead>
            <tbody>
              {data.items.map((order) => {
                const blockerCount = order.blockers?.length || 0;
                const processState = order.linked_process?.state || "not_created";
                const productRowsSync = order.linked_process?.product_rows_sync;
                const actionLabel = processState === "linked"
                  ? "Открыть заказ"
                  : processState === "pending"
                    ? "Карточка создаётся…"
                    : processState === "broken"
                      ? "Связь требует восстановления"
                      : "Открыть проект";
                return (
                <tr className={blockerCount > 0 ? "order-registry__row--blocked" : ""} key={order.id}>
                  <td>
                    <strong>{order.onec_document_number || `Проект №${order.id}`}</strong>
                    <small>{dateOnly(order.onec_document_date || order.order_date)}</small>
                    <small>{order.origin === "onec_import" ? "Источник: 1С" : "Источник: приложение"}</small>
                  </td>
                  <td>
                    <strong>{order.supplier_name}</strong>
                    <small>{order.contract_name} · {order.warehouse_name}</small>
                    <small>{ORDER_CONTOUR_LABELS[order.procurement_contour] || order.procurement_contour} · {order.line_count} поз.</small>
                  </td>
                  <td>
                    <span className={`state-pill ${order.lifecycle_status === "blocked" || order.sync_conflict ? "state-pill--blocked" : "state-pill--ready"}`}>
                      {order.lifecycle_status_label}
                    </span>
                    <OrderBlockerCell order={order} />
                    {(order.sync_conflict || order.onec_error) && <small>{order.sync_conflict || order.onec_error}</small>}
                  </td>
                  <td><strong>{number(order.ordered_quantity)}</strong><small>{number(order.received_quantity || 0)} / {number(order.open_quantity ?? order.ordered_quantity)}</small></td>
                  <td>{dateOnly(order.expected_receipt_date)}<small>{order.cargo_dropoff_date ? `Cargo: ${dateOnly(order.cargo_dropoff_date)}` : ""}</small></td>
                  <td><strong>{money(order.total_amount, order.currency)}</strong></td>
                  <td>
                    <button
                      className="btn btn--ghost btn--small"
                      disabled={processState === "pending" || processState === "broken"}
                      onClick={() => void openOrder(order)}
                      type="button"
                    >{actionLabel}</button>
                    {processState === "linked" && order.linked_process?.item_id ? (
                      <>
                        <small>{order.linked_process.stage_name || `Процесс №${order.linked_process.item_id}`}</small>
                        {productRowsSync?.state === "error" ? (
                          <small className="order-registry__product-sync-error">
                            Товары не синхронизированы: {productRowsSync.error || "повторит плановая синхронизация"}
                          </small>
                        ) : productRowsSync?.state === "pending" ? (
                          <small>Товары синхронизируются…</small>
                        ) : productRowsSync?.state === "synced" ? (
                          <small>Товаров в карточке: {productRowsSync.synced_count ?? 0}</small>
                        ) : null}
                      </>
                    ) : processState === "pending" ? (
                      <small>Заказ создан в 1С, связь проверяется</small>
                    ) : processState === "broken" ? (
                      <small className="order-registry__process-error">{order.linked_process?.error || "Требуется reconciliation"}</small>
                    ) : (
                      <small>Процесс появится после создания документа в 1С</small>
                    )}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <footer className="lifecycle-queue__footer">
        <div>
          <strong>Страница {data.page} из {Math.max(1, Math.ceil(data.total / data.page_size))}</strong>
          <small>Показано {data.items.length} из {data.total} заказов.</small>
        </div>
        <div>
          <button className="btn btn--ghost" disabled={page <= 1} onClick={() => setViewState((current) => ({ ...current, page: Math.max(1, current.page - 1) }))} type="button">Назад</button>
          <button className="btn btn--ghost" disabled={page * data.page_size >= data.total} onClick={() => setViewState((current) => ({ ...current, page: current.page + 1 }))} type="button">Вперёд</button>
        </div>
      </footer>
    </main>
  );
}

function ClassificationQueue() {
  const [data, setData] = useState<ProcurementClassificationQueue | null>(null);
  const [status, setStatus] = useState("");
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      setData(await fetchProcurementClassifications({ status, page_size: 100 }));
    } catch (requestError) {
      setError(errorText(requestError));
    }
  }, [status]);

  useEffect(() => {
    let cancelled = false;
    fetchProcurementClassifications({ status, page_size: 100 })
      .then((response) => { if (!cancelled) setData(response); })
      .catch((requestError) => { if (!cancelled) setError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [status]);

  const approve = async (item: ProcurementClassificationQueue["items"][number]) => {
    setLoadingId(item.proposal.id);
    try {
      await approveProcurementClassification(item.order_id, item.line_id, item.proposal.id);
      toast.success("Изменение утверждено");
      await load();
    } catch (requestError) {
      toast.error(errorText(requestError));
    } finally {
      setLoadingId(null);
    }
  };

  if (error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (!data) return <LoadingState message="Загрузка изменений свойств..." />;
  return (
    <main className="order-workspace__content">
      <section className="registry-summary registry-summary--three">
        <div><span>Ожидают решения</span><strong>{data.pending}</strong></div>
        <div><span>Утверждено сегодня</span><strong>{data.approved_today}</strong></div>
        <div><span>Конфликты readback</span><strong>{data.readback_conflicts}</strong></div>
      </section>
      <section className="registry-toolbar registry-toolbar--compact">
        <select onChange={(event) => setStatus(event.target.value)} value={status}>
          <option value="">Все статусы</option>
          <option value="proposed">Ожидают решения</option>
          <option value="approved">Dry-run утверждён</option>
          <option value="sent_to_1c">Передано в 1С</option>
          <option value="conflict">Конфликт</option>
          <option value="reflected">Проверено в каталоге</option>
        </select>
      </section>
      {data.items.length === 0 ? <div className="order-workspace__empty">Изменений свойств нет.</div> : (
        <div className="order-workspace__table-wrap">
          <table className="order-workspace__table">
            <thead><tr><th>Товар</th><th>Изменение</th><th>Причина / автор</th><th>Решение</th></tr></thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.proposal.id}>
                  <td><strong>{item.product_name}</strong><small>{item.nomenclature_code || item.nomenclature_ref}</small><small>Заказ: {item.supplier_name}</small></td>
                  <td><strong>{item.effective_status ? statusScreenLabel(item.effective_status) : "Не задан"} → {item.proposal.proposed_status_label}</strong>{item.proposal.manual_minimum && <small>Минимум: {item.proposal.manual_minimum} · пересмотр {item.proposal.review_date}</small>}</td>
                  <td><strong>{item.proposal.reason}</strong><small>Предложил: {item.proposal.requested_by_name || item.proposal.requested_by_bitrix_user_id}</small></td>
                  <td>
                    {item.proposal.status === "proposed" ? (
                      <div className="proposal-decision">
                        <button
                          className="btn btn--small"
                          disabled={loadingId === item.proposal.id || item.proposal.can_approve === false}
                          onClick={() => void approve(item)}
                          type="button"
                        >
                          Принять
                        </button>
                        {item.proposal.can_approve === false && (
                          <small>
                            {item.proposal.self_proposed
                              ? "Своё предложение согласовать нельзя — нужен второй сотрудник закупки"
                              : "Согласование доступно только допущенным сотрудникам закупки"}
                          </small>
                        )}
                      </div>
                    ) : (
                      <span className={`state-pill ${item.proposal.onec_status === "conflict" ? "state-pill--blocked" : "state-pill--ready"}`}>{proposalStatusLabel(item.proposal.status)}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="order-workspace__footnote">После утверждения: XML свойств → 1С → CommerceML → проверка нового значения каталога Bitrix по GUID.</div>
    </main>
  );
}

export function EventHistory() {
  const [data, setData] = useState<ProcurementEventList | null>(null);
  const [eventType, setEventType] = useState("");
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setError("");
    try {
      setData(await fetchProcurementEvents({ event_type: eventType, page_size: 100 }));
    } catch (requestError) {
      setError(errorText(requestError));
    }
  }, [eventType]);
  useEffect(() => {
    let cancelled = false;
    fetchProcurementEvents({ event_type: eventType, page_size: 100 })
      .then((response) => { if (!cancelled) setData(response); })
      .catch((requestError) => { if (!cancelled) setError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [eventType]);
  if (error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (!data) return <LoadingState message="Загрузка истории..." />;
  return (
    <main className="order-workspace__content">
      <section className="registry-toolbar registry-toolbar--compact">
        <select onChange={(event) => setEventType(event.target.value)} value={eventType}>
          <option value="">Все действия</option>
          {Object.entries(EVENT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </section>
      {data.items.length === 0 ? <div className="order-workspace__empty">История пока пуста.</div> : (
        <div className="event-list">
          {data.items.map((event) => (
            <article key={event.id}>
              <time>{dateTime(event.created_at)}</time>
              <div><strong>{EVENT_LABELS[event.event_type] || event.event_type}</strong><span>{event.user_name || event.actor}</span></div>
              <small>
                {event.entity_type} #{event.entity_id}{event.order_id ? ` · заказ #${event.order_id}` : ""}
                {event.product ? (
                  <> · <a
                    href={resolveBitrixPortalUrl(event.product.bitrix_url) || resolveBitrixProductUrl(event.product.bitrix_product_id)}
                    rel="noreferrer"
                    target="_blank"
                  >{event.product.name || "Карточка товара"}</a></>
                ) : null}
              </small>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}

export function ProcurementOrderFormationWorkspace({ bitrixUserName, bitrixItemId }: Props) {
  const [route, setRoute] = useState<WorkspaceRoute>(() => routeFromLocation());
  const [dashboard, setDashboard] = useState<ProcurementDashboard | null>(null);
  const [dashboardError, setDashboardError] = useState("");
  const [order, setOrder] = useState<ProcurementOrderFormation | null>(null);
  const [orderError, setOrderError] = useState("");
  const [processRedirectError, setProcessRedirectError] = useState("");
  const redirectedOrderId = useRef<number | null>(null);

  const navigate = useCallback((next: WorkspaceRoute, replace = false) => {
    if (next.kind === "order") {
      setOrder(null);
      setOrderError("");
      setProcessRedirectError("");
    }
    window.history[replace ? "replaceState" : "pushState"]({}, "", routeUrl(next));
    setRoute(next);
  }, []);

  useEffect(() => {
    const onPopState = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const loadDashboard = useCallback(async () => {
    setDashboardError("");
    try {
      setDashboard(await fetchProcurementDashboard());
    } catch (requestError) {
      setDashboardError(errorText(requestError));
    }
  }, []);

  useEffect(() => {
    if (bitrixItemId || route.kind !== "tab" || route.tab !== "dashboard") return;
    let cancelled = false;
    fetchProcurementDashboard()
      .then((response) => { if (!cancelled) setDashboard(response); })
      .catch((requestError) => {
        if (!cancelled) setDashboardError(errorText(requestError));
      });
    return () => { cancelled = true; };
  }, [bitrixItemId, route]);

  useEffect(() => {
    if (!bitrixItemId) return;
    let cancelled = false;
    fetchProcurementOrderFormation(bitrixItemId)
      .then((response) => { if (!cancelled) setOrder(response); })
      .catch((requestError) => { if (!cancelled) setOrderError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [bitrixItemId]);

  useEffect(() => {
    if (bitrixItemId || route.kind !== "order") return;
    let cancelled = false;
    fetchProcurementOrder(route.orderId)
      .then((response) => { if (!cancelled) setOrder(response); })
      .catch((requestError) => { if (!cancelled) setOrderError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [bitrixItemId, route]);

  useEffect(() => {
    if (bitrixItemId || route.kind !== "order" || !order) return;
    const processItemId = order.linked_process?.item_id;
    if (order.linked_process?.state !== "linked" || !processItemId) {
      redirectedOrderId.current = null;
      return;
    }
    if (redirectedOrderId.current === order.id) return;
    redirectedOrderId.current = order.id;
    openBitrixProcurementProcess(processItemId)
      .then(() => navigate({ kind: "tab", tab: "orders" }, true))
      .catch((requestError) => {
        redirectedOrderId.current = null;
        setProcessRedirectError(errorText(requestError));
      });
  }, [bitrixItemId, navigate, order, route]);

  if (bitrixItemId) {
    if (orderError) return <ErrorState message={orderError} onRetry={() => window.location.reload()} />;
    if (!order) return <LoadingState message="Загрузка связанного заказа..." />;
    return <ProcurementOrderFormationApp bitrixUserName={bitrixUserName} initialOrder={order} />;
  }

  if (route.kind === "lifecycle") {
    return (
      <LifecycleQueue
        initialReadiness={route.readiness}
        proposalId={route.proposalId}
        scope={route.scope}
        status={route.status}
        onClose={() => navigate({ kind: "tab", tab: "dashboard" })}
      />
    );
  }
  if (route.kind === "review") {
    return (
      <ProcurementFamilyReview
        nomenclatureCode={route.nomenclatureCode}
        onBack={() => navigate({ kind: "tab", tab: "dashboard" })}
      />
    );
  }
  if (route.kind === "order") {
    if (orderError) return <ErrorState message={orderError} onRetry={() => navigate(route, true)} />;
    if (!order) return <LoadingState message="Загрузка карточки заказа..." />;
    if (order.linked_process?.state === "linked") {
      if (processRedirectError) {
        return <ErrorState message={processRedirectError} onRetry={() => {
          redirectedOrderId.current = null;
          setProcessRedirectError("");
          setOrder({ ...order });
        }} />;
      }
      return <LoadingState message="Открываем заказ в Smart Process..." />;
    }
    if (order.linked_process?.state === "pending" || order.linked_process?.state === "broken") {
      return (
        <ProcessLinkState
          error={order.linked_process.error}
          state={order.linked_process.state}
          onBack={() => navigate({ kind: "tab", tab: "orders" }, true)}
        />
      );
    }
    return <ProcurementOrderFormationApp bitrixUserName={bitrixUserName} focusLineId={route.focusLineId} initialOrder={order} onBack={() => navigate({ kind: "tab", tab: "orders" })} />;
  }

  return (
    <AppShell bitrixUserName={bitrixUserName} activeTab={route.tab} onNavigate={navigate}>
      {route.tab === "dashboard" && (
        dashboardError ? <ErrorState message={dashboardError} onRetry={() => void loadDashboard()} />
          : dashboard ? <Dashboard
              data={dashboard}
              onOpenLifecycle={(status, scope, options) => navigate({
                kind: "lifecycle",
                status,
                scope,
                readiness: options?.readiness || "all",
                proposalId: options?.proposalId,
              })}
              onOpenReview={(nomenclatureCode) => navigate({ kind: "review", nomenclatureCode })}
              onRefresh={loadDashboard}
            />
            : <LoadingState message="Загрузка витрины..." />
      )}
      {route.tab === "orders" && <OrdersRegistry onOpenOrder={(orderId) => navigate({ kind: "order", orderId })} />}
      {route.tab === "assistant" && <ProcurementOrderAssistant onOpenOrder={(orderId, focusLineId) => navigate({ kind: "order", orderId, focusLineId })} />}
      {route.tab === "properties" && <ClassificationQueue />}
      {route.tab === "history" && <EventHistory />}
    </AppShell>
  );
}
