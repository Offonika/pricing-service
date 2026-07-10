import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  approveProcurementClassification,
  approveProcurementLifecycleTransitions,
  fetchProcurementClassifications,
  fetchProcurementDashboard,
  fetchProcurementEvents,
  fetchProcurementLifecycleTransitions,
  fetchProcurementOrder,
  fetchProcurementOrders,
  type ProcurementClassificationQueue,
  type ProcurementDashboard,
  type ProcurementEventList,
  type ProcurementLifecycleApprovalResponse,
  type ProcurementLifecycleTransition,
  type ProcurementLifecycleTransitionList,
  type ProcurementOrderFormation,
  type ProcurementOrderList,
} from "../api/procurementAssortment";
import { ProcurementOrderFormationApp } from "./ProcurementOrderFormationApp";

interface Props {
  bitrixUserName?: string | null;
}

type WorkspaceTab = "dashboard" | "orders" | "properties" | "history";

type WorkspaceRoute =
  | { kind: "tab"; tab: WorkspaceTab }
  | { kind: "lifecycle"; status: string; scope: "action" | "all" }
  | { kind: "order"; orderId: number };

const TAB_LABELS: Record<WorkspaceTab, string> = {
  dashboard: "Витрина",
  orders: "Заказы",
  properties: "Свойства",
  history: "История",
};

const MANUAL_STATUS_LABELS: Record<string, string> = {
  matrix: "Матричный",
  on_demand: "Под заказ",
  replace_candidate: "Кандидат на замену",
  nonliquid: "Кандидат на неликвид",
  do_not_order: "Не закупать",
  review: "Review / разбор",
};

const ORDER_STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  review: "На проверке",
  approved: "Проверен",
  transmitting: "Передача в 1С",
  transmitted: "Передан в 1С",
  deferred: "Отложен",
  error: "Ошибка",
};

const LIFECYCLE_STATUS_LABELS: Record<string, string> = {
  fruit: "Плод",
  newborn: "Новорожденный",
  new_item: "Новинка",
  sales_start: "СП / Старт продаж",
  sale: "ПРОДАЖА",
  working: "Рабочий",
};

const LIFECYCLE_FACT_LABELS: Record<string, string> = {
  customer_order_count_1c: "Заказов покупателей",
  customer_order_qty_1c: "Количество в заказах",
  supplier_order_count_1c: "Заказов поставщику",
  supplier_order_qty_1c: "Количество у поставщика",
  cargo_handoff_count_1c: "Передач в груз",
  card_created_at_1c: "Карточка создана",
  model_birth_date: "Модель на рынке с",
};

const EVENT_LABELS: Record<string, string> = {
  order_conditions_changed: "Изменены условия заказа",
  order_line_changed: "Изменена строка заказа",
  order_checked_and_sent: "Заказ проверен и отправлен",
  classification_proposed: "Предложено изменение свойства",
  classification_approved: "Изменение свойства утверждено",
  lifecycle_transitions_approved: "Утверждён пакет переходов",
};

function errorText(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  if (detail) return detail;
  return error instanceof Error ? error.message : "Операция не выполнена";
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
    return {
      kind: "lifecycle",
      status: decodeURIComponent(lifecycleMatch[1]),
      scope: params.get("scope") === "all" ? "all" : "action",
    };
  }
  const orderMatch = relative.match(/^\/orders\/(\d+)$/);
  if (orderMatch) return { kind: "order", orderId: Number(orderMatch[1]) };
  if (relative === "/orders") return { kind: "tab", tab: "orders" };
  if (relative === "/properties") return { kind: "tab", tab: "properties" };
  if (relative === "/history") return { kind: "tab", tab: "history" };
  return { kind: "tab", tab: "dashboard" };
}

function routeUrl(route: WorkspaceRoute) {
  const root = "/bitrix/procurement-order-formation";
  if (route.kind === "lifecycle") {
    return `${root}/lifecycle/${encodeURIComponent(route.status)}?scope=${route.scope}`;
  }
  if (route.kind === "order") return `${root}/orders/${route.orderId}`;
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
  return (
    <div className="order-workspace">
      <header className="order-workspace__header">
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
        {bitrixUserName && <span className="order-workspace__user">{bitrixUserName}</span>}
      </header>
      {children}
    </div>
  );
}

function LoadingState({ message = "Загрузка..." }: { message?: string }) {
  return <div className="order-workspace__state">{message}</div>;
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

function Dashboard({
  data,
  onOpenLifecycle,
}: {
  data: ProcurementDashboard;
  onOpenLifecycle: (status: string, scope: "action" | "all") => void;
}) {
  return (
    <main className="order-workspace__content">
      <section className="order-workspace__section-heading">
        <div>
          <h2>Жизненные статусы</h2>
          <p>Общее количество открывает все товары, бейдж — очередь решения.</p>
        </div>
        <span>Обновлено {dateTime(data.updated_at)} · run {data.run_id || "—"}</span>
      </section>
      <section className="lifecycle-cards">
        {data.cards.map((card) => (
          <article className={`lifecycle-card lifecycle-card--${card.urgency}`} key={card.status}>
            <button
              className="lifecycle-card__total"
              onClick={() => onOpenLifecycle(card.status, "all")}
              type="button"
            >
              <span>{card.label}</span>
              <strong>{card.total_count}</strong>
            </button>
            <button
              className="lifecycle-card__action"
              disabled={card.action_count === 0}
              onClick={() => onOpenLifecycle(card.status, "action")}
              type="button"
            >
              {card.action_label} {card.action_count}
            </button>
          </article>
        ))}
      </section>

      <section className="order-workspace__section-heading order-workspace__section-heading--manual">
        <div>
          <h2>Ручные решения и очереди</h2>
          <p>Не меняют заказ автоматически и утверждаются отдельно.</p>
        </div>
      </section>
      <section className="manual-status-grid">
        {Object.entries(MANUAL_STATUS_LABELS).map(([status, label]) => (
          <article className={`manual-status-card manual-status-card--${status}`} key={status}>
            <span>{label}</span>
            <strong>{data.manual_status_counts[status] || 0}</strong>
          </article>
        ))}
      </section>

      <section className="attention-panel">
        <div className="order-workspace__section-heading">
          <div>
            <h2>Требует внимания сегодня</h2>
            <p>Короткая очередь блокеров и ручных проверок.</p>
          </div>
        </div>
        {data.attention.length === 0 ? (
          <div className="order-workspace__empty">Срочных решений нет.</div>
        ) : (
          <div className="order-workspace__table-wrap">
            <table className="order-workspace__table">
              <thead>
                <tr>
                  <th>Товар</th>
                  <th>Причина</th>
                  <th>Рекомендация</th>
                  <th>Срок</th>
                </tr>
              </thead>
              <tbody>
                {data.attention.map((item) => (
                  <tr key={`${item.nomenclature_code}-${item.current_status}`}>
                    <td>
                      <strong>{item.product_name}</strong>
                      <small>{item.nomenclature_code} · {item.current_status_label}</small>
                    </td>
                    <td>{item.reason}</td>
                    <td>{item.recommendation}</td>
                    <td><span className={`state-pill state-pill--${item.urgency}`}>{item.deadline_label}</span></td>
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

function LifecycleQueue({
  status,
  scope,
  onClose,
}: {
  status: string;
  scope: "action" | "all";
  onClose: () => void;
}) {
  const [data, setData] = useState<ProcurementLifecycleTransitionList | null>(null);
  const [search, setSearch] = useState("");
  const [readiness, setReadiness] = useState<"all" | "ready" | "blocked" | "stale">("all");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ProcurementLifecycleApprovalResponse | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchProcurementLifecycleTransitions({
        status,
        scope,
        readiness,
        search,
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
  }, [page, readiness, scope, search, status]);

  useEffect(() => {
    void load();
  }, [load]);

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
  const statusLabel = LIFECYCLE_STATUS_LABELS[status] || first?.current_status_label || status;
  const title = scope === "all"
    ? `${statusLabel}: все товары`
    : status === "working"
      ? "Рабочий: товары на пересмотр"
      : `${statusLabel}: требуется решение`;
  const subtitle = scope === "all"
    ? "Полный список товаров выбранного жизненного статуса"
    : "Только товары, где недостаточно автоматических фактов и нужен человек";

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
          <div><span>Нужна проверка</span><strong>{data?.blocked_count || 0}</strong></div>
          <div><span>Данные устарели</span><strong>{data?.stale_count || 0}</strong></div>
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
            <option value="blocked">Нужна проверка</option>
            <option value="stale">Обновить данные</option>
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
              ? "Решений не требуется: переходы выполнены автоматически или подходящих товаров нет."
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
                  return (
                    <tr className={!item.selectable && scope === "action" ? "is-blocked" : ""} key={`${proposalId}-${item.nomenclature_code}`}>
                      <td>
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
                      </td>
                      <td>
                        <strong>{item.product_name}</strong>
                        <small>{item.nomenclature_code} · папка Дисплеи</small>
                        <small>Ответственный: {item.responsible_name || "Омар"}</small>
                      </td>
                      <td>
                        <span className="transition-pill">
                          {item.action_kind === "review"
                            ? item.current_status === "newborn"
                              ? "ДН / Добор новорождённого"
                              : "Рабочий → Review"
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
                          item.blockers.map((blocker) => <span className="state-pill state-pill--blocked" key={blocker}>{blocker}</span>)
                        ) : item.stale ? (
                          <span className="state-pill state-pill--warning">данные изменились</span>
                        ) : (
                          <span className="state-pill state-pill--ready">Нет блокеров</span>
                        )}
                      </td>
                      <td>
                        {item.selectable ? (
                          <button className="btn btn--small" disabled={loading} onClick={() => void approve([item])} type="button">
                            Подтвердить
                          </button>
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

function OrdersRegistry({ onOpenOrder }: { onOpenOrder: (orderId: number) => void }) {
  const [data, setData] = useState<ProcurementOrderList | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [blockers, setBlockers] = useState<"all" | "with" | "without">("all");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      setData(await fetchProcurementOrders({ search, status, blockers, page_size: 100 }));
    } catch (requestError) {
      setError(errorText(requestError));
    }
  }, [blockers, search, status]);

  useEffect(() => {
    let cancelled = false;
    fetchProcurementOrders({ search, status, blockers, page_size: 100 })
      .then((response) => { if (!cancelled) setData(response); })
      .catch((requestError) => { if (!cancelled) setError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [blockers, search, status]);

  if (error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (!data) return <LoadingState message="Загрузка заказов..." />;
  return (
    <main className="order-workspace__content">
      <section className="registry-summary">
        <div><span>Черновиков</span><strong>{data.summary.orders}</strong></div>
        <div><span>Позиций</span><strong>{data.summary.lines}</strong></div>
        <div><span>Количество</span><strong>{number(data.summary.quantity)}</strong></div>
        <div><span>Сумма</span><strong>{money(data.summary.amount)}</strong></div>
      </section>
      <section className="registry-toolbar">
        <input onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по поставщику или товару" value={search} />
        <select onChange={(event) => setStatus(event.target.value)} value={status}>
          <option value="">Все статусы</option>
          {Object.entries(ORDER_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <select onChange={(event) => setBlockers(event.target.value as typeof blockers)} value={blockers}>
          <option value="all">Все проверки</option>
          <option value="without">Без блокеров</option>
          <option value="with">С блокерами</option>
        </select>
      </section>
      {data.items.length === 0 ? <div className="order-workspace__empty">Заказы не сформированы.</div> : (
        <div className="order-workspace__table-wrap">
          <table className="order-workspace__table order-registry__table">
            <thead><tr><th>Поставщик / условия</th><th>Поз.</th><th>Кол-во</th><th>Сумма</th><th>Проверка</th><th>Действие</th></tr></thead>
            <tbody>
              {data.items.map((order) => (
                <tr key={order.id}>
                  <td>
                    <strong>{order.supplier_name}</strong>
                    <small>{order.contract_name} · {order.warehouse_name}</small>
                    <small>{order.route} · партия {order.batch_id}</small>
                  </td>
                  <td>{order.line_count}</td>
                  <td>{number(order.total_quantity)}</td>
                  <td><strong>{money(order.total_amount, order.currency)}</strong></td>
                  <td>
                    <span className={`state-pill ${order.blockers.length ? "state-pill--blocked" : "state-pill--ready"}`}>
                      {order.blockers.length ? `${order.blockers.length} блокер` : "готов"}
                    </span>
                  </td>
                  <td><button className="btn btn--ghost btn--small" onClick={() => onOpenOrder(order.id)} type="button">Открыть</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
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
                  <td><strong>{item.effective_status || "Не задан"} → {item.proposal.proposed_status_label}</strong>{item.proposal.manual_minimum && <small>Минимум: {item.proposal.manual_minimum} · пересмотр {item.proposal.review_date}</small>}</td>
                  <td><strong>{item.proposal.reason}</strong><small>Предложил: {item.proposal.requested_by_name || item.proposal.requested_by_bitrix_user_id}</small></td>
                  <td>
                    {item.proposal.status === "proposed" ? (
                      <button className="btn btn--small" disabled={loadingId === item.proposal.id} onClick={() => void approve(item)} type="button">Принять</button>
                    ) : (
                      <span className={`state-pill ${item.proposal.onec_status === "conflict" ? "state-pill--blocked" : "state-pill--ready"}`}>{item.proposal.status}</span>
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

function EventHistory() {
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
              <small>{event.entity_type} #{event.entity_id}{event.order_id ? ` · заказ #${event.order_id}` : ""}</small>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}

export function ProcurementOrderFormationWorkspace({ bitrixUserName }: Props) {
  const [route, setRoute] = useState<WorkspaceRoute>(() => routeFromLocation());
  const [dashboard, setDashboard] = useState<ProcurementDashboard | null>(null);
  const [dashboardError, setDashboardError] = useState("");
  const [order, setOrder] = useState<ProcurementOrderFormation | null>(null);
  const [orderError, setOrderError] = useState("");

  const navigate = useCallback((next: WorkspaceRoute, replace = false) => {
    if (next.kind === "order") {
      setOrder(null);
      setOrderError("");
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
    if (route.kind !== "tab" || route.tab !== "dashboard") return;
    let cancelled = false;
    fetchProcurementDashboard()
      .then((response) => { if (!cancelled) setDashboard(response); })
      .catch((requestError) => {
        if (!cancelled) setDashboardError(errorText(requestError));
      });
    return () => { cancelled = true; };
  }, [route]);

  useEffect(() => {
    if (route.kind !== "order") return;
    let cancelled = false;
    fetchProcurementOrder(route.orderId)
      .then((response) => { if (!cancelled) setOrder(response); })
      .catch((requestError) => { if (!cancelled) setOrderError(errorText(requestError)); });
    return () => { cancelled = true; };
  }, [route]);

  if (route.kind === "lifecycle") {
    return <LifecycleQueue status={route.status} scope={route.scope} onClose={() => navigate({ kind: "tab", tab: "dashboard" })} />;
  }
  if (route.kind === "order") {
    if (orderError) return <ErrorState message={orderError} onRetry={() => navigate(route, true)} />;
    if (!order) return <LoadingState message="Загрузка карточки заказа..." />;
    return <ProcurementOrderFormationApp bitrixUserName={bitrixUserName} initialOrder={order} onBack={() => navigate({ kind: "tab", tab: "orders" })} />;
  }

  return (
    <AppShell bitrixUserName={bitrixUserName} activeTab={route.tab} onNavigate={navigate}>
      {route.tab === "dashboard" && (
        dashboardError ? <ErrorState message={dashboardError} onRetry={() => void loadDashboard()} />
          : dashboard ? <Dashboard data={dashboard} onOpenLifecycle={(status, scope) => navigate({ kind: "lifecycle", status, scope })} />
            : <LoadingState message="Загрузка витрины..." />
      )}
      {route.tab === "orders" && <OrdersRegistry onOpenOrder={(orderId) => navigate({ kind: "order", orderId })} />}
      {route.tab === "properties" && <ClassificationQueue />}
      {route.tab === "history" && <EventHistory />}
    </AppShell>
  );
}
