import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { isAxiosError } from "axios";

import { logisticsApi as api } from "../api/logistics";

type CustomerReturnCarrier = "russian_post" | "cdek";
type CustomerReturnStatus =
  | "registered"
  | "in_transit"
  | "arrived_at_pickup_point"
  | "picked_up"
  | "onec_return_confirmed"
  | "cancelled"
  | "exception";

type CustomerReturnShipment = {
  id: number;
  carrier: CustomerReturnCarrier;
  tracking_number: string;
  status: CustomerReturnStatus;
  source_ref?: string | null;
  bitrix_case_id?: string | null;
  onec_order_ref?: string | null;
  onec_return_ref?: string | null;
  carrier_last_status_text?: string | null;
  storage_deadline_at?: string | null;
  arrived_at?: string | null;
  picked_up_at?: string | null;
  onec_return_confirmed_at?: string | null;
  updated_at: string;
};

type CustomerReturnEvent = {
  id: number;
  event_type: string;
  source: string;
  normalized_status?: string | null;
  carrier_status_text?: string | null;
  actor_bitrix_user_id?: string | null;
  occurred_at: string;
};

type CustomerReturnDetail = CustomerReturnShipment & {
  events: CustomerReturnEvent[];
};

type CustomerReturnRegistration = {
  created: boolean;
  shipment: CustomerReturnDetail;
};

type CustomerReturnsWorkspaceProps = {
  showTestingGuide?: boolean;
};

type HelpTab = "work" | "testing";

type CustomerReturnsHelpProps = {
  activeTab: HelpTab;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  onTabChange: (tab: HelpTab) => void;
  showTestingGuide: boolean;
};

const CARRIER_LABELS: Record<CustomerReturnCarrier, string> = {
  russian_post: "Почта России",
  cdek: "СДЭК",
};

const STATUS_LABELS: Record<CustomerReturnStatus, string> = {
  registered: "Трек зарегистрирован",
  in_transit: "В пути",
  arrived_at_pickup_point: "Можно забирать",
  picked_up: "Забрали",
  onec_return_confirmed: "Возврат подтверждён в 1С",
  cancelled: "Отменён",
  exception: "Требует проверки",
};

const EVENT_LABELS: Record<string, string> = {
  registered: "Трек зарегистрирован",
  carrier_status: "Статус перевозчика",
  pickup_confirmed: "Сотрудник подтвердил получение",
  onec_return_confirmed: "Возврат найден в 1С",
};

const STATUS_HELP: Array<{ status: CustomerReturnStatus; description: string }> = [
  { status: "registered", description: "Трек сохранён, движение ещё не подтверждено." },
  { status: "in_transit", description: "Отправление принято перевозчиком и едет к нам." },
  { status: "arrived_at_pickup_point", description: "Возврат прибыл, его нужно забрать." },
  { status: "picked_up", description: "Сотрудник подтвердил получение возврата." },
  { status: "onec_return_confirmed", description: "Документ возврата найден при сверке с 1С." },
  { status: "cancelled", description: "Перевозка отменена или отправление возвращается клиенту." },
  { status: "exception", description: "Статус требует ручной проверки сотрудником." },
];

function CustomerReturnsHelp({
  activeTab,
  closeButtonRef,
  onClose,
  onTabChange,
  showTestingGuide,
}: CustomerReturnsHelpProps) {
  return (
    <div
      className="customer-returns-help__backdrop"
      role="presentation"
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        aria-describedby="customer-returns-help-description"
        aria-labelledby="customer-returns-help-title"
        aria-modal="true"
        className="customer-returns-help"
        role="dialog"
      >
        <header className="customer-returns-help__header">
          <div>
            <span className="logistics-step">?</span>
            <div>
              <h2 id="customer-returns-help-title">Справка по возвратам</h2>
              <p id="customer-returns-help-description">
                Регистрация, контроль движения и получение клиентского возврата.
              </p>
            </div>
          </div>
          <button
            aria-label="Закрыть справку"
            className="btn btn--ghost"
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >
            Закрыть
          </button>
        </header>

        {showTestingGuide && (
          <div aria-label="Разделы справки" className="customer-returns-help__tabs" role="tablist">
            <button
              aria-controls="customer-returns-help-work"
              aria-selected={activeTab === "work"}
              className={activeTab === "work" ? "is-active" : ""}
              id="customer-returns-help-work-tab"
              onClick={() => onTabChange("work")}
              role="tab"
              type="button"
            >
              Как работать
            </button>
            <button
              aria-controls="customer-returns-help-testing"
              aria-selected={activeTab === "testing"}
              className={activeTab === "testing" ? "is-active" : ""}
              id="customer-returns-help-testing-tab"
              onClick={() => onTabChange("testing")}
              role="tab"
              type="button"
            >
              Тестирование
            </button>
          </div>
        )}

        {activeTab === "work" ? (
          <div
            aria-labelledby={showTestingGuide ? "customer-returns-help-work-tab" : undefined}
            className="customer-returns-help__body"
            id="customer-returns-help-work"
            role={showTestingGuide ? "tabpanel" : undefined}
          >
            <section>
              <h3>Как зарегистрировать возврат</h3>
              <ol>
                <li>Выберите Почту России или СДЭК.</li>
                <li>Введите трек-номер и, если известен, номер заказа 1С.</li>
                <li>Нажмите «Зарегистрировать» и проверьте карточку и первое событие истории.</li>
              </ol>
              <p>
                Повторный ввод того же трека не создаёт дубликат: откроется уже существующий
                возврат.
              </p>
            </section>

            <section>
              <h3>Как контролировать возврат</h3>
              <ol>
                <li>Используйте фильтры по перевозчику и состоянию.</li>
                <li>Откройте карточку, чтобы увидеть срок хранения и историю событий.</li>
                <li>
                  Когда появится состояние «Можно забирать», получите отправление и нажмите
                  «Забрали».
                </li>
                <li>После получения дождитесь подтверждения документа возврата в 1С.</li>
              </ol>
            </section>

            <section>
              <h3>Что означают состояния</h3>
              <dl className="customer-returns-help__statuses">
                {STATUS_HELP.map((item) => (
                  <div key={item.status}>
                    <dt>{STATUS_LABELS[item.status]}</dt>
                    <dd>{item.description}</dd>
                  </div>
                ))}
              </dl>
            </section>
          </div>
        ) : (
          <div
            aria-labelledby="customer-returns-help-testing-tab"
            className="customer-returns-help__body"
            id="customer-returns-help-testing"
            role="tabpanel"
          >
            <div className="customer-returns-help__notice">
              Тест создаёт записи в Production. Используйте только указанные тестовые значения и
              не привязывайте их к реальному заказу клиента.
            </div>

            <section>
              <h3>Тестовые данные</h3>
              <div className="customer-returns-help__test-values">
                <p>
                  <strong>Почта России</strong>
                  <code>99999999999999</code>
                  <span>Заказ 1С: TEST-3507-RP</span>
                </p>
                <p>
                  <strong>СДЭК</strong>
                  <code>TEST-3507-CDEK</code>
                  <span>Заказ 1С: TEST-3507-CDEK</span>
                </p>
              </div>
            </section>

            <section>
              <h3>Проверка пилота</h3>
              <ol>
                <li>Зарегистрируйте оба возврата и проверьте карточки и историю.</li>
                <li>Проверьте фильтры по перевозчику и состоянию «Трек зарегистрирован».</li>
                <li>
                  Передайте ID возвратов техническому исполнителю для безопасной имитации
                  состояния «Можно забирать».
                </li>
                <li>Откройте карточку прибывшего возврата и нажмите «Забрали».</li>
                <li>
                  Убедитесь, что запись исчезла из фильтра «Можно забирать», а интерфейс сообщил о
                  контроле сверки с 1С.
                </li>
              </ol>
              <p>
                Зафиксируйте результат в задаче №3507: проверяющий, время, ID возвратов и скриншот
                ошибки, если она возникла.
              </p>
            </section>
          </div>
        )}
      </section>
    </div>
  );
}

function apiError(error: unknown) {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
  }
  return error instanceof Error ? error.message : "Не удалось выполнить операцию";
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusTone(status: CustomerReturnStatus) {
  if (status === "arrived_at_pickup_point") return "ready";
  if (status === "picked_up" || status === "onec_return_confirmed") return "done";
  if (status === "cancelled" || status === "exception") return "warning";
  return "active";
}

function upsertShipment(
  current: CustomerReturnShipment[],
  shipment: CustomerReturnShipment,
  carrierFilter: string,
  statusFilter: string
) {
  if (
    (carrierFilter && shipment.carrier !== carrierFilter) ||
    (statusFilter && shipment.status !== statusFilter)
  ) {
    return current.filter((item) => item.id !== shipment.id);
  }
  return [shipment, ...current.filter((item) => item.id !== shipment.id)];
}

export function CustomerReturnsWorkspace({
  showTestingGuide = false,
}: CustomerReturnsWorkspaceProps) {
  const [returns, setReturns] = useState<CustomerReturnShipment[]>([]);
  const [detail, setDetail] = useState<CustomerReturnDetail | null>(null);
  const [carrierFilter, setCarrierFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [carrier, setCarrier] = useState<CustomerReturnCarrier>("russian_post");
  const [trackingNumber, setTrackingNumber] = useState("");
  const [onecOrderRef, setOnecOrderRef] = useState("");
  const [pickupComment, setPickupComment] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [helpTab, setHelpTab] = useState<HelpTab>("work");
  const helpButtonRef = useRef<HTMLButtonElement>(null);
  const helpCloseButtonRef = useRef<HTMLButtonElement>(null);
  const registerButtonRef = useRef<HTMLButtonElement>(null);
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null);
  const detailCloseButtonRef = useRef<HTMLButtonElement>(null);
  const detailOpen = detail !== null;

  const openHelp = () => {
    setHelpTab("work");
    setHelpOpen(true);
  };

  const closeHelp = () => {
    setHelpOpen(false);
    helpButtonRef.current?.focus();
  };

  const closeDetail = () => {
    setDetail(null);
    detailTriggerRef.current?.focus();
  };

  useEffect(() => {
    if (helpOpen) helpCloseButtonRef.current?.focus();
  }, [helpOpen]);

  useEffect(() => {
    if (detailOpen) detailCloseButtonRef.current?.focus();
  }, [detailOpen]);

  useEffect(() => {
    if (!helpOpen && !detailOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [detailOpen, helpOpen]);

  const loadReturns = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number> = { limit: 100 };
      if (carrierFilter) params.carrier = carrierFilter;
      if (statusFilter) params.status = statusFilter;
      const { data } = await api.get<CustomerReturnShipment[]>(
        "/bitrix/logistics/customer-returns",
        { params }
      );
      setReturns(data);
      setMessage("");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setLoading(false);
    }
  }, [carrierFilter, statusFilter]);

  useEffect(() => {
    void loadReturns();
  }, [loadReturns]);

  const registerReturn = async (event: React.FormEvent) => {
    event.preventDefault();
    const normalizedTrack = trackingNumber.trim();
    if (!normalizedTrack || busy) return;
    setBusy(true);
    try {
      const { data } = await api.post<CustomerReturnRegistration>(
        "/bitrix/logistics/customer-returns",
        {
          carrier,
          tracking_number: normalizedTrack,
          onec_order_ref: onecOrderRef.trim() || null,
        }
      );
      setReturns((current) =>
        upsertShipment(current, data.shipment, carrierFilter, statusFilter)
      );
      detailTriggerRef.current = registerButtonRef.current;
      setDetail(data.shipment);
      setTrackingNumber("");
      setOnecOrderRef("");
      setMessage(data.created ? "Возврат зарегистрирован" : "Этот трек уже есть в реестре");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  const openDetail = async (
    shipment: CustomerReturnShipment,
    trigger: HTMLButtonElement,
  ) => {
    if (busy) return;
    setBusy(true);
    try {
      const { data } = await api.get<CustomerReturnDetail>(
        `/bitrix/logistics/customer-returns/${shipment.id}`
      );
      detailTriggerRef.current = trigger;
      setDetail(data);
      setPickupComment("");
      setMessage("");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  const confirmPickup = async () => {
    if (!detail || detail.status !== "arrived_at_pickup_point" || busy) return;
    setBusy(true);
    try {
      const { data } = await api.post<CustomerReturnDetail>(
        `/bitrix/logistics/customer-returns/${detail.id}/pickup`,
        {
          idempotency_key: `bitrix-ui-pickup-${detail.id}`,
          comment: pickupComment.trim() || null,
        }
      );
      setDetail(data);
      setReturns((current) => upsertShipment(current, data, carrierFilter, statusFilter));
      setPickupComment("");
      setMessage("Получение возврата подтверждено. Поставлен контроль сверки с 1С.");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="customer-returns">
      <section className="logistics-card">
        <div className="logistics-card__heading">
          <div>
            <span className="logistics-step">1</span>
            <h2>Добавить возврат</h2>
          </div>
          <button
            aria-label="Открыть справку по возвратам"
            className="btn btn--ghost customer-returns__help-trigger"
            onClick={openHelp}
            ref={helpButtonRef}
            type="button"
          >
            Справка
          </button>
        </div>
        <form className="customer-returns__registration" onSubmit={registerReturn}>
          <label className="logistics-field">
            <span>Перевозчик</span>
            <select
              aria-label="Перевозчик возврата"
              value={carrier}
              onChange={(event) => setCarrier(event.target.value as CustomerReturnCarrier)}
            >
              <option value="russian_post">Почта России</option>
              <option value="cdek">СДЭК</option>
            </select>
          </label>
          <label className="logistics-field customer-returns__track">
            <span>Трек-номер</span>
            <input
              aria-label="Трек-номер возврата"
              autoComplete="off"
              maxLength={64}
              placeholder={carrier === "russian_post" ? "14 цифр или международный трек" : "Номер отправления СДЭК"}
              value={trackingNumber}
              onChange={(event) => setTrackingNumber(event.target.value)}
            />
          </label>
          <label className="logistics-field">
            <span>Заказ 1С</span>
            <input
              aria-label="Заказ 1С"
              maxLength={64}
              placeholder="Необязательно"
              value={onecOrderRef}
              onChange={(event) => setOnecOrderRef(event.target.value)}
            />
          </label>
          <button
            className="btn logistics-primary"
            type="submit"
            disabled={busy || trackingNumber.trim().length < 5}
            ref={registerButtonRef}
          >
            Зарегистрировать
          </button>
        </form>
      </section>

      <section className="logistics-card">
        <div className="logistics-card__heading">
          <h2>Реестр возвратов</h2>
          <b aria-label={`Возвратов в списке: ${returns.length}`}>{returns.length}</b>
        </div>
        <div className="customer-returns__filters">
          <label className="logistics-field">
            <span>Перевозчик</span>
            <select
              aria-label="Фильтр по перевозчику"
              value={carrierFilter}
              onChange={(event) => setCarrierFilter(event.target.value)}
            >
              <option value="">Все</option>
              <option value="russian_post">Почта России</option>
              <option value="cdek">СДЭК</option>
            </select>
          </label>
          <label className="logistics-field">
            <span>Состояние</span>
            <select
              aria-label="Фильтр по состоянию"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
            >
              <option value="">Все</option>
              {Object.entries(STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <button className="btn btn--ghost" type="button" disabled={loading} onClick={() => void loadReturns()}>
            Обновить
          </button>
        </div>
        {loading && <p className="logistics-empty" role="status">Загружаем возвраты…</p>}
        {!loading && !returns.length && (
          <p className="logistics-empty">Возвраты с выбранными условиями не найдены</p>
        )}
        <div className="customer-returns__list">
          {returns.map((shipment) => (
            <article key={shipment.id}>
              <div className="customer-returns__return-heading">
                <div>
                  <strong>{shipment.tracking_number}</strong>
                  <span>{CARRIER_LABELS[shipment.carrier]}</span>
                </div>
                <span className={`customer-returns__status customer-returns__status--${statusTone(shipment.status)}`}>
                  {STATUS_LABELS[shipment.status]}
                </span>
              </div>
              <div className="customer-returns__meta">
                <span>Хранение: {shipment.storage_deadline_at ? `до ${formatDate(shipment.storage_deadline_at)}` : "срок не передан"}</span>
                <span>1С: {shipment.onec_return_confirmed_at ? `подтверждено ${formatDate(shipment.onec_return_confirmed_at)}` : "ожидает сверки"}</span>
                {shipment.onec_order_ref && <span>Заказ: {shipment.onec_order_ref}</span>}
              </div>
              <button
                className="btn btn--ghost"
                type="button"
                disabled={busy}
                onClick={(event) => void openDetail(shipment, event.currentTarget)}
              >
                Открыть карточку
              </button>
            </article>
          ))}
        </div>
      </section>

      {detail && (
        <div
          className="customer-return-detail__backdrop"
          role="presentation"
          onKeyDown={(event) => {
            if (event.key === "Escape") closeDetail();
          }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDetail();
          }}
        >
          <section
            aria-labelledby="customer-return-detail-title"
            aria-modal="true"
            className="logistics-card customer-returns__detail customer-return-detail"
            role="dialog"
          >
            <div className="logistics-card__heading customer-return-detail__header">
              <div>
                <span className="logistics-step">3</span>
                <h2 id="customer-return-detail-title">Возврат {detail.tracking_number}</h2>
              </div>
              <button
                aria-label="Закрыть карточку возврата"
                className="btn btn--ghost"
                onClick={closeDetail}
                ref={detailCloseButtonRef}
                type="button"
              >
                Закрыть
              </button>
            </div>

            <div className="customer-return-detail__body">
              {message && <div className="logistics-toast" role="status">{message}</div>}
              <div className="customer-returns__summary">
                <span className={`customer-returns__status customer-returns__status--${statusTone(detail.status)}`}>
                  {STATUS_LABELS[detail.status]}
                </span>
                <span>{CARRIER_LABELS[detail.carrier]}</span>
                <span>Срок хранения: {formatDate(detail.storage_deadline_at)}</span>
                <span>Заказ 1С: {detail.onec_order_ref || "не указан"}</span>
                <span>Документ возврата 1С: {detail.onec_return_ref || "ещё не найден"}</span>
              </div>

              {detail.status === "arrived_at_pickup_point" && (
                <div className="customer-returns__pickup">
                  <label className="logistics-field">
                    <span>Комментарий к получению</span>
                    <input
                      aria-label="Комментарий к получению"
                      maxLength={500}
                      placeholder="Необязательно"
                      value={pickupComment}
                      onChange={(event) => setPickupComment(event.target.value)}
                    />
                  </label>
                  <button className="btn logistics-primary" type="button" disabled={busy} onClick={() => void confirmPickup()}>
                    Забрали
                  </button>
                </div>
              )}

              <div className="logistics-timeline customer-returns__timeline">
                {detail.events.map((event) => (
                  <article key={event.id}>
                    <i />
                    <div>
                      <strong>{EVENT_LABELS[event.event_type] || event.event_type}</strong>
                      <span>{event.carrier_status_text || (event.normalized_status && STATUS_LABELS[event.normalized_status as CustomerReturnStatus]) || "Состояние сохранено"}</span>
                      <small>{formatDate(event.occurred_at)} · {event.source}</small>
                    </div>
                  </article>
                ))}
              </div>
            </div>
          </section>
        </div>
      )}

      {message && !detail && <div className="logistics-toast" role="status">{message}</div>}
      {helpOpen && (
        <CustomerReturnsHelp
          activeTab={helpTab}
          closeButtonRef={helpCloseButtonRef}
          onClose={closeHelp}
          onTabChange={setHelpTab}
          showTestingGuide={showTestingGuide}
        />
      )}
    </div>
  );
}
