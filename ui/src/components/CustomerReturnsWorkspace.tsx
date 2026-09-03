import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { isAxiosError } from "axios";

import { logisticsApi as api } from "../api/logistics";
import {
  openBitrixCustomerReturnDeal,
  openBitrixCustomerReturnServiceRequest,
} from "../api/bitrix";

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
  bitrix_deal_id?: number | null;
  bitrix_deal_title?: string | null;
  bitrix_order_ref?: string | null;
  bitrix_deal_stage_id?: string | null;
  bitrix_deal_stage_name?: string | null;
  bitrix_deal_closed?: boolean | null;
  bitrix_contact_id?: number | null;
  bitrix_contact_name?: string | null;
  bitrix_company_id?: number | null;
  bitrix_company_name?: string | null;
  bitrix_responsible_user_id?: number | null;
  bitrix_responsible_name?: string | null;
  service_request_item_id?: number | null;
  serviceRequest?: CustomerReturnServiceRequest | null;
  expertiseCases?: CustomerReturnExpertise[];
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

type CustomerReturnDeal = {
  deal_id: number;
  title: string;
  order_ref?: string | null;
  stage_id?: string | null;
  stage_name?: string | null;
  closed: boolean;
  created_at?: string | null;
  contact_id?: number | null;
  contact_name?: string | null;
  company_id?: number | null;
  company_name?: string | null;
  responsible_user_id?: number | null;
  responsible_name?: string | null;
};

type CustomerReturnServiceRequest = {
  item_id: number;
  title?: string | null;
  stage_id?: string | null;
  stage_name?: string | null;
  closed: boolean;
  category_id?: number | null;
  deal_id?: number | null;
  order_ref?: string | null;
  responsible_user_id?: number | null;
  responsible_name?: string | null;
  site_ticket_id?: string | null;
};

type CustomerReturnExpertise = {
  id: number;
  external_id?: string;
  onec_expertise_number?: string | null;
  current_status: string;
  linked_customer_order_number?: string | null;
  problem_summary?: string | null;
  service_request_item_id?: number | null;
};

type CustomerReturnsWorkspaceProps = {
  serviceLinksEnabled?: boolean;
  showTestingGuide?: boolean;
};

type HelpTab = "work" | "testing";

type CustomerReturnsHelpProps = {
  activeTab: HelpTab;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  onTabChange: (tab: HelpTab) => void;
  serviceLinksEnabled: boolean;
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
  deal_link_changed: "Привязка сделки изменена",
  service_request_link_changed: "Привязка сервисного обращения изменена",
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
  serviceLinksEnabled,
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
                <li>Введите трек-номер и найдите сделку по ID, названию или номеру заказа.</li>
                <li>Если сделку пока найти не удалось, зарегистрируйте возврат без неё.</li>
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

            {serviceLinksEnabled ? (
              <section>
                <h3>Обращения и экспертизы</h3>
                <ol>
                  <li>
                    После выбора сделки выберите связанное сервисное обращение. Закрытые
                    обращения остаются в поиске и помечаются как закрытые.
                  </li>
                  <li>
                    Если обращения ещё нет, зарегистрируйте трек без него: возврат появится с
                    меткой «Обращение не привязано».
                  </li>
                  <li>
                    В карточке возврата можно привязать, заменить или убрать обращение, открыть
                    обращение и сделку в Bitrix24.
                  </li>
                  <li>
                    Экспертизу добавляйте через поиск по номеру экспертизы или заказа. Разные
                    известные номера заказа связать нельзя.
                  </li>
                </ol>
                <p>
                  Удаление обращения не удаляет сделку. Для поиска непривязанных записей
                  используйте фильтр «Без обращения».
                </p>
              </section>
            ) : null}

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
                  <span>Сделку не выбирайте</span>
                </p>
                <p>
                  <strong>СДЭК</strong>
                  <code>TEST-3507-CDEK</code>
                  <span>Сделку не выбирайте</span>
                </p>
              </div>
            </section>

            <section>
              <h3>Проверка пилота</h3>
              <ol>
                <li>Зарегистрируйте оба возврата и проверьте карточки и историю.</li>
                {serviceLinksEnabled ? (
                  <li>
                    Один возврат оставьте без обращения, затем привяжите существующее обращение
                    из карточки и проверьте фильтр «Без обращения».
                  </li>
                ) : null}
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
  statusFilter: string,
  serviceRequestFilter: string,
) {
  if (
    (carrierFilter && shipment.carrier !== carrierFilter) ||
    (statusFilter && shipment.status !== statusFilter) ||
    (serviceRequestFilter === "missing" && shipment.service_request_item_id) ||
    (serviceRequestFilter === "linked" && !shipment.service_request_item_id)
  ) {
    return current.filter((item) => item.id !== shipment.id);
  }
  return [shipment, ...current.filter((item) => item.id !== shipment.id)];
}

function dealFromShipment(shipment: CustomerReturnShipment): CustomerReturnDeal | null {
  if (!shipment.bitrix_deal_id) return null;
  return {
    deal_id: shipment.bitrix_deal_id,
    title: shipment.bitrix_deal_title || `Сделка #${shipment.bitrix_deal_id}`,
    order_ref: shipment.bitrix_order_ref,
    stage_id: shipment.bitrix_deal_stage_id,
    stage_name: shipment.bitrix_deal_stage_name,
    closed: Boolean(shipment.bitrix_deal_closed),
    contact_id: shipment.bitrix_contact_id,
    contact_name: shipment.bitrix_contact_name,
    company_id: shipment.bitrix_company_id,
    company_name: shipment.bitrix_company_name,
    responsible_user_id: shipment.bitrix_responsible_user_id,
    responsible_name: shipment.bitrix_responsible_name,
  };
}

function dealClientLabel(deal: CustomerReturnDeal) {
  return deal.company_name || deal.contact_name || null;
}

type CustomerReturnDealPickerProps = {
  disabled?: boolean;
  idPrefix: string;
  label: string;
  onSelect: (deal: CustomerReturnDeal | null) => void;
  selected: CustomerReturnDeal | null;
};

function CustomerReturnDealPicker({
  disabled = false,
  idPrefix,
  label,
  onSelect,
  selected,
}: CustomerReturnDealPickerProps) {
  const [query, setQuery] = useState(selected?.title || "");
  const [options, setOptions] = useState<CustomerReturnDeal[]>([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listId = `${idPrefix}-deal-options`;

  useEffect(() => {
    setQuery(selected?.title || "");
  }, [selected?.deal_id, selected?.title]);

  useEffect(() => {
    const normalized = query.trim();
    if (selected && normalized === selected.title) {
      setOptions([]);
      setOpen(false);
      setSearchError("");
      return undefined;
    }
    if (normalized.length < 2) {
      setOptions([]);
      setOpen(false);
      setSearching(false);
      setSearchError("");
      return undefined;
    }

    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setSearching(true);
      setSearchError("");
      try {
        const { data } = await api.get<CustomerReturnDeal[]>(
          "/bitrix/logistics/customer-return-deals",
          { params: { search: normalized, limit: 20 }, signal: controller.signal }
        );
        setOptions(data);
        setActiveIndex(0);
        setOpen(true);
      } catch (error) {
        if (isAxiosError(error) && error.code === "ERR_CANCELED") return;
        setOptions([]);
        setOpen(true);
        setSearchError(apiError(error));
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, 350);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [query, selected]);

  const choose = (deal: CustomerReturnDeal) => {
    onSelect(deal);
    setQuery(deal.title);
    setOptions([]);
    setOpen(false);
    setSearchError("");
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || !options.length) {
      if (event.key === "ArrowDown" && query.trim().length >= 2) setOpen(true);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => Math.min(current + 1, options.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => Math.max(current - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(options[activeIndex]);
    } else if (event.key === "Escape") {
      event.stopPropagation();
      setOpen(false);
    }
  };

  return (
    <div className="logistics-field customer-return-deal-picker">
      <span>{label}</span>
      <div className="customer-return-deal-picker__control">
        <input
          aria-activedescendant={open && options.length ? `${idPrefix}-deal-${options[activeIndex].deal_id}` : undefined}
          aria-autocomplete="list"
          aria-controls={listId}
          aria-expanded={open}
          aria-label={label}
          autoComplete="off"
          disabled={disabled}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          onChange={(event) => {
            setQuery(event.target.value);
            onSelect(null);
          }}
          onFocus={() => {
            if (options.length || searchError) setOpen(true);
          }}
          onKeyDown={onKeyDown}
          placeholder="ID, название или номер заказа"
          role="combobox"
          value={query}
        />
        {selected && (
          <button
            aria-label="Очистить выбранную сделку"
            className="customer-return-deal-picker__clear"
            disabled={disabled}
            onClick={() => {
              onSelect(null);
              setQuery("");
            }}
            type="button"
          >
            ×
          </button>
        )}
      </div>
      {selected && (
        <small className="customer-return-deal-picker__selected">
          #{selected.deal_id}{selected.order_ref ? ` · заказ ${selected.order_ref}` : ""}
          {selected.closed ? " · закрыта" : ""}
        </small>
      )}
      {searching && <small role="status">Ищем сделки…</small>}
      {open && (
        <div className="customer-return-deal-picker__options" id={listId} role="listbox">
          {searchError ? (
            <p role="alert">{searchError}. Возврат можно зарегистрировать без сделки.</p>
          ) : options.length ? (
            options.map((deal, index) => (
              <button
                aria-selected={index === activeIndex}
                className={index === activeIndex ? "is-active" : ""}
                id={`${idPrefix}-deal-${deal.deal_id}`}
                key={deal.deal_id}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(deal)}
                role="option"
                type="button"
              >
                <strong>#{deal.deal_id} · {deal.title}</strong>
                <span>
                  {deal.order_ref ? `Заказ ${deal.order_ref}` : "Номер заказа не заполнен"}
                  {deal.stage_name ? ` · ${deal.stage_name}` : ""}
                  {deal.closed ? " · Закрыта" : ""}
                </span>
                {(dealClientLabel(deal) || deal.responsible_name) && (
                  <small>
                    {dealClientLabel(deal) ? `Клиент: ${dealClientLabel(deal)}` : ""}
                    {deal.responsible_name ? ` · Ответственный: ${deal.responsible_name}` : ""}
                  </small>
                )}
              </button>
            ))
          ) : (
            <p>Сделки не найдены. Возврат можно зарегистрировать без привязки.</p>
          )}
        </div>
      )}
    </div>
  );
}

type ServiceRequestPickerProps = {
  dealId?: number | null;
  disabled?: boolean;
  idPrefix: string;
  label: string;
  onSelect: (request: CustomerReturnServiceRequest | null) => void;
  selected: CustomerReturnServiceRequest | null;
};

function CustomerReturnServiceRequestPicker({
  dealId,
  disabled = false,
  idPrefix,
  label,
  onSelect,
  selected,
}: ServiceRequestPickerProps) {
  const [query, setQuery] = useState(selected?.title || "");
  const [options, setOptions] = useState<CustomerReturnServiceRequest[]>([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const listId = `${idPrefix}-service-request-options`;

  useEffect(() => {
    setQuery(selected?.title || "");
  }, [selected?.item_id, selected?.title]);

  useEffect(() => {
    const normalized = query.trim();
    if (selected && normalized === selected.title) return undefined;
    if (!dealId && normalized.length < 2) {
      setOptions([]);
      setOpen(false);
      setError("");
      return undefined;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setSearching(true);
      setError("");
      try {
        const params: Record<string, string | number> = { limit: 20 };
        if (dealId) params.deal_id = dealId;
        if (normalized.length >= 2) params.search = normalized;
        const { data } = await api.get<CustomerReturnServiceRequest[]>(
          "/bitrix/logistics/customer-return-service-requests",
          { params, signal: controller.signal },
        );
        setOptions(data);
        setActiveIndex(0);
        setOpen(true);
      } catch (searchError) {
        if (isAxiosError(searchError) && searchError.code === "ERR_CANCELED") return;
        setOptions([]);
        setOpen(true);
        setError(apiError(searchError));
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, 300);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [dealId, query, selected]);

  const choose = (request: CustomerReturnServiceRequest) => {
    onSelect(request);
    setQuery(request.title || `Обращение #${request.item_id}`);
    setOpen(false);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || !options.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => Math.min(current + 1, options.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => Math.max(current - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(options[activeIndex]);
    } else if (event.key === "Escape") {
      event.stopPropagation();
      setOpen(false);
    }
  };

  return (
    <div className="logistics-field customer-return-deal-picker">
      <span>{label}</span>
      <div className="customer-return-deal-picker__control">
        <input
          aria-activedescendant={open && options.length ? `${idPrefix}-service-request-${options[activeIndex].item_id}` : undefined}
          aria-autocomplete="list"
          aria-controls={listId}
          aria-expanded={open}
          aria-label={label}
          autoComplete="off"
          disabled={disabled}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          onChange={(event) => {
            setQuery(event.target.value);
            onSelect(null);
          }}
          onFocus={() => {
            if (options.length || error) setOpen(true);
          }}
          onKeyDown={onKeyDown}
          placeholder="ID, заголовок или ID тикета"
          role="combobox"
          value={query}
        />
        {selected ? (
          <button
            aria-label="Очистить выбранное обращение"
            className="customer-return-deal-picker__clear"
            disabled={disabled}
            onClick={() => {
              onSelect(null);
              setQuery("");
            }}
            type="button"
          >
            ×
          </button>
        ) : null}
      </div>
      {selected ? (
        <small className="customer-return-deal-picker__selected">
          #{selected.item_id}{selected.stage_name ? ` · ${selected.stage_name}` : ""}
          {selected.closed ? " · закрыто" : ""}
        </small>
      ) : null}
      {searching ? <small role="status">Ищем обращения…</small> : null}
      {open ? (
        <div className="customer-return-deal-picker__options" id={listId} role="listbox">
          {error ? (
            <p role="alert">{error}. Возврат можно оставить без обращения.</p>
          ) : options.length ? (
            options.map((request, index) => (
              <button
                aria-selected={index === activeIndex}
                className={index === activeIndex ? "is-active" : ""}
                id={`${idPrefix}-service-request-${request.item_id}`}
                key={request.item_id}
                onClick={() => choose(request)}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                role="option"
                type="button"
              >
                <strong>#{request.item_id} · {request.title || "Без названия"}</strong>
                <span>
                  {request.order_ref ? `Заказ ${request.order_ref}` : "Заказ не указан"}
                  {request.stage_name ? ` · ${request.stage_name}` : ""}
                  {request.closed ? " · Закрыто" : ""}
                </span>
                {request.responsible_name ? (
                  <small>Ответственный: {request.responsible_name}</small>
                ) : null}
              </button>
            ))
          ) : (
            <p>Обращения не найдены. Трек можно зарегистрировать без связи.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function CustomerReturnExpertisePicker({
  disabled,
  onSelect,
}: {
  disabled: boolean;
  onSelect: (item: CustomerReturnExpertise) => void;
}) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<CustomerReturnExpertise[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    const normalized = query.trim();
    if (normalized.length < 2) {
      setOptions([]);
      setError("");
      return undefined;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      try {
        const { data } = await api.get<CustomerReturnExpertise[]>(
          "/bitrix/logistics/customer-return-expertise",
          { params: { search: normalized, limit: 20 }, signal: controller.signal },
        );
        setOptions(data);
        setError("");
      } catch (searchError) {
        if (isAxiosError(searchError) && searchError.code === "ERR_CANCELED") return;
        setOptions([]);
        setError(apiError(searchError));
      }
    }, 300);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [query]);

  return (
    <div className="customer-return-expertise-picker">
      <label className="logistics-field">
        <span>Добавить экспертизу</span>
        <input
          aria-label="Поиск экспертизы"
          autoComplete="off"
          disabled={disabled}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Номер экспертизы или заказа"
          value={query}
        />
      </label>
      {error ? <small role="alert">{error}</small> : null}
      {options.length ? (
        <div className="customer-return-expertise-picker__options">
          {options.map((item) => (
            <button
              className="btn btn--ghost"
              disabled={disabled}
              key={item.id}
              onClick={() => {
                onSelect(item);
                setQuery("");
                setOptions([]);
              }}
              type="button"
            >
              Экспертиза {item.onec_expertise_number || `#${item.id}`}
              {item.linked_customer_order_number ? ` · заказ ${item.linked_customer_order_number}` : ""}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function CustomerReturnsWorkspace({
  serviceLinksEnabled = false,
  showTestingGuide = false,
}: CustomerReturnsWorkspaceProps) {
  const [returns, setReturns] = useState<CustomerReturnShipment[]>([]);
  const [detail, setDetail] = useState<CustomerReturnDetail | null>(null);
  const [carrierFilter, setCarrierFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [serviceRequestFilter, setServiceRequestFilter] = useState("");
  const [carrier, setCarrier] = useState<CustomerReturnCarrier>("russian_post");
  const [trackingNumber, setTrackingNumber] = useState("");
  const [selectedDeal, setSelectedDeal] = useState<CustomerReturnDeal | null>(null);
  const [selectedServiceRequest, setSelectedServiceRequest] =
    useState<CustomerReturnServiceRequest | null>(null);
  const [detailDeal, setDetailDeal] = useState<CustomerReturnDeal | null>(null);
  const [detailServiceRequest, setDetailServiceRequest] =
    useState<CustomerReturnServiceRequest | null>(null);
  const [editingDealLink, setEditingDealLink] = useState(false);
  const [editingServiceRequestLink, setEditingServiceRequestLink] = useState(false);
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
    setDetailDeal(null);
    setDetailServiceRequest(null);
    setEditingDealLink(false);
    setEditingServiceRequestLink(false);
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
      if (serviceRequestFilter === "missing") params.without_service_request = "true";
      if (serviceRequestFilter === "linked") params.without_service_request = "false";
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
  }, [carrierFilter, serviceRequestFilter, statusFilter]);

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
          bitrix_deal_id: selectedDeal?.deal_id || null,
          ...(serviceLinksEnabled
            ? { serviceRequestItemId: selectedServiceRequest?.item_id || null }
            : {}),
        }
      );
      setReturns((current) =>
        upsertShipment(
          current,
          data.shipment,
          carrierFilter,
          statusFilter,
          serviceRequestFilter,
        )
      );
      detailTriggerRef.current = registerButtonRef.current;
      setDetail(data.shipment);
      setDetailDeal(dealFromShipment(data.shipment));
      setDetailServiceRequest(data.shipment.serviceRequest || null);
      setTrackingNumber("");
      setSelectedDeal(null);
      setSelectedServiceRequest(null);
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
      setDetailDeal(dealFromShipment(data));
      setDetailServiceRequest(data.serviceRequest || null);
      setEditingDealLink(false);
      setEditingServiceRequestLink(false);
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
      setReturns((current) =>
        upsertShipment(current, data, carrierFilter, statusFilter, serviceRequestFilter)
      );
      setPickupComment("");
      setMessage("Получение возврата подтверждено. Поставлен контроль сверки с 1С.");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  const saveDealLink = async (deal: CustomerReturnDeal | null = detailDeal) => {
    if (!detail || busy) return;
    setBusy(true);
    try {
      const { data } = await api.put<CustomerReturnDetail>(
        `/bitrix/logistics/customer-returns/${detail.id}/deal-link`,
        { bitrix_deal_id: deal?.deal_id || null }
      );
      setDetail(data);
      setDetailDeal(dealFromShipment(data));
      setReturns((current) =>
        upsertShipment(current, data, carrierFilter, statusFilter, serviceRequestFilter)
      );
      setEditingDealLink(false);
      setMessage(deal ? "Сделка привязана к возврату" : "Привязка сделки удалена");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  const saveServiceRequestLink = async (
    request: CustomerReturnServiceRequest | null = detailServiceRequest,
  ) => {
    if (!detail || busy) return;
    setBusy(true);
    try {
      const { data } = await api.put<CustomerReturnDetail>(
        `/bitrix/logistics/customer-returns/${detail.id}/service-request-link`,
        { serviceRequestItemId: request?.item_id || null },
      );
      setDetail(data);
      setDetailDeal(dealFromShipment(data));
      setDetailServiceRequest(data.serviceRequest || null);
      setReturns((current) =>
        upsertShipment(current, data, carrierFilter, statusFilter, serviceRequestFilter)
      );
      setEditingServiceRequestLink(false);
      setMessage(request ? "Сервисное обращение привязано" : "Привязка обращения удалена");
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  const changeExpertiseLink = async (
    expertise: CustomerReturnExpertise,
    serviceRequestItemId: number | null,
  ) => {
    if (!detail || busy) return;
    setBusy(true);
    try {
      await api.put(
        `/bitrix/logistics/expertise/${expertise.id}/service-request-link`,
        { serviceRequestItemId },
      );
      const { data } = await api.get<CustomerReturnDetail>(
        `/bitrix/logistics/customer-returns/${detail.id}`,
      );
      setDetail(data);
      setReturns((current) =>
        upsertShipment(current, data, carrierFilter, statusFilter, serviceRequestFilter)
      );
      setMessage(serviceRequestItemId ? "Экспертиза привязана" : "Привязка экспертизы удалена");
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
          <CustomerReturnDealPicker
            disabled={busy}
            idPrefix="customer-return-registration"
            label="Сделка Bitrix24 (необязательно)"
            onSelect={(deal) => {
              setSelectedDeal(deal);
              if (
                selectedServiceRequest?.deal_id &&
                deal?.deal_id !== selectedServiceRequest.deal_id
              ) {
                setSelectedServiceRequest(null);
              }
            }}
            selected={selectedDeal}
          />
          {serviceLinksEnabled ? (
            <CustomerReturnServiceRequestPicker
              dealId={selectedDeal?.deal_id}
              disabled={busy}
              idPrefix="customer-return-registration"
              label="Сервисное обращение (необязательно)"
              onSelect={setSelectedServiceRequest}
              selected={selectedServiceRequest}
            />
          ) : null}
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
          {serviceLinksEnabled ? (
            <label className="logistics-field">
              <span>Сервисное обращение</span>
              <select
                aria-label="Фильтр по сервисному обращению"
                onChange={(event) => setServiceRequestFilter(event.target.value)}
                value={serviceRequestFilter}
              >
                <option value="">Все</option>
                <option value="linked">Привязано</option>
                <option value="missing">Без обращения</option>
              </select>
            </label>
          ) : null}
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
                {shipment.bitrix_deal_id ? (
                  <span>
                    Сделка #{shipment.bitrix_deal_id}
                    {shipment.bitrix_order_ref ? ` · заказ ${shipment.bitrix_order_ref}` : ""}
                  </span>
                ) : shipment.onec_order_ref ? (
                  <span>Ранее указанный заказ: {shipment.onec_order_ref}</span>
                ) : null}
                {serviceLinksEnabled ? (
                  shipment.service_request_item_id ? (
                    <span>Обращение #{shipment.service_request_item_id}</span>
                  ) : (
                    <span className="customer-returns__missing-link">Обращение не привязано</span>
                  )
                ) : null}
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
                <span>
                  Сделка: {detail.bitrix_deal_id
                    ? `#${detail.bitrix_deal_id} · ${detail.bitrix_deal_title || "без названия"}`
                    : "не привязана"}
                </span>
                <span>
                  Заказ: {detail.bitrix_order_ref || (!detail.bitrix_deal_id && detail.onec_order_ref) || "не указан"}
                </span>
                {(detail.bitrix_company_name || detail.bitrix_contact_name) && (
                  <span>Клиент: {detail.bitrix_company_name || detail.bitrix_contact_name}</span>
                )}
                {detail.bitrix_responsible_name && (
                  <span>Ответственный: {detail.bitrix_responsible_name}</span>
                )}
                <span>Документ возврата 1С: {detail.onec_return_ref || "ещё не найден"}</span>
              </div>

              <div className="customer-return-deal-link">
                <div>
                  <strong>Связь с заказом и клиентом</strong>
                  <span>
                    {detail.bitrix_deal_id
                      ? `${detail.bitrix_deal_stage_name || detail.bitrix_deal_stage_id || "Стадия не указана"}${detail.bitrix_deal_closed ? " · закрыта" : ""}`
                      : "Возврат зарегистрирован без сделки"}
                  </span>
                </div>
                {!editingDealLink ? (
                  <button
                    className="btn btn--ghost"
                    disabled={busy}
                    onClick={() => {
                      setDetailDeal(dealFromShipment(detail));
                      setEditingDealLink(true);
                    }}
                    type="button"
                  >
                    {detail.bitrix_deal_id ? "Изменить связь" : "Привязать сделку"}
                  </button>
                ) : (
                  <div className="customer-return-deal-link__editor">
                    <CustomerReturnDealPicker
                      disabled={busy}
                      idPrefix={`customer-return-detail-${detail.id}`}
                      label="Новая сделка Bitrix24"
                      onSelect={setDetailDeal}
                      selected={detailDeal}
                    />
                    <div className="customer-return-deal-link__actions">
                      <button
                        className="btn logistics-primary"
                        disabled={busy || !detailDeal}
                        onClick={() => void saveDealLink()}
                        type="button"
                      >
                        Сохранить связь
                      </button>
                      {detail.bitrix_deal_id && (
                        <button
                          className="btn btn--ghost"
                          disabled={busy}
                          onClick={() => void saveDealLink(null)}
                          type="button"
                        >
                          Убрать связь
                        </button>
                      )}
                      <button
                        className="btn btn--ghost"
                        disabled={busy}
                        onClick={() => {
                          setDetailDeal(dealFromShipment(detail));
                          setEditingDealLink(false);
                        }}
                        type="button"
                      >
                        Отмена
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {detail.bitrix_deal_id ? (
                <button
                  className="btn btn--ghost customer-return-open-link"
                  onClick={() => {
                    void openBitrixCustomerReturnDeal(detail.bitrix_deal_id!).catch((error) =>
                      setMessage(apiError(error)),
                    );
                  }}
                  type="button"
                >
                  Открыть сделку
                </button>
              ) : null}

              {serviceLinksEnabled ? (
                <div className="customer-return-service-links">
                  <div className="customer-return-deal-link">
                    <div>
                      <strong>Сервисное обращение</strong>
                      <span>
                        {detail.serviceRequest
                          ? `#${detail.serviceRequest.item_id} · ${detail.serviceRequest.title || "Без названия"}${detail.serviceRequest.closed ? " · закрыто" : ""}`
                          : "Обращение не привязано"}
                      </span>
                    </div>
                    {!editingServiceRequestLink ? (
                      <div className="customer-return-deal-link__actions">
                        {detail.serviceRequest ? (
                          <button
                            className="btn btn--ghost"
                            disabled={busy}
                            onClick={() => {
                              void openBitrixCustomerReturnServiceRequest(
                                detail.serviceRequest!.item_id,
                              ).catch((error) => setMessage(apiError(error)));
                            }}
                            type="button"
                          >
                            Открыть обращение
                          </button>
                        ) : null}
                        <button
                          className="btn btn--ghost"
                          disabled={busy}
                          onClick={() => {
                            setDetailServiceRequest(detail.serviceRequest || null);
                            setEditingServiceRequestLink(true);
                          }}
                          type="button"
                        >
                          {detail.serviceRequest ? "Изменить обращение" : "Привязать обращение"}
                        </button>
                      </div>
                    ) : (
                      <div className="customer-return-deal-link__editor">
                        <CustomerReturnServiceRequestPicker
                          dealId={detail.bitrix_deal_id}
                          disabled={busy}
                          idPrefix={`customer-return-service-${detail.id}`}
                          label="Сервисное обращение"
                          onSelect={setDetailServiceRequest}
                          selected={detailServiceRequest}
                        />
                        <div className="customer-return-deal-link__actions">
                          <button
                            className="btn logistics-primary"
                            disabled={busy || !detailServiceRequest}
                            onClick={() => void saveServiceRequestLink()}
                            type="button"
                          >
                            Сохранить обращение
                          </button>
                          {detail.serviceRequest ? (
                            <button
                              className="btn btn--ghost"
                              disabled={busy}
                              onClick={() => void saveServiceRequestLink(null)}
                              type="button"
                            >
                              Убрать связь
                            </button>
                          ) : null}
                          <button
                            className="btn btn--ghost"
                            disabled={busy}
                            onClick={() => {
                              setDetailServiceRequest(detail.serviceRequest || null);
                              setEditingServiceRequestLink(false);
                            }}
                            type="button"
                          >
                            Отмена
                          </button>
                        </div>
                      </div>
                    )}
                  </div>

                  {detail.serviceRequest ? (
                    <div className="customer-return-expertise">
                      <div>
                        <strong>Экспертизы</strong>
                        <span>{detail.expertiseCases?.length || 0}</span>
                      </div>
                      {detail.expertiseCases?.length ? (
                        <div className="customer-return-expertise__list">
                          {detail.expertiseCases.map((item) => (
                            <article key={item.id}>
                              <div>
                                <strong>
                                  Экспертиза {item.onec_expertise_number || `#${item.id}`}
                                </strong>
                                <span>
                                  {item.current_status}
                                  {item.linked_customer_order_number
                                    ? ` · заказ ${item.linked_customer_order_number}`
                                    : ""}
                                </span>
                              </div>
                              <button
                                className="btn btn--ghost"
                                disabled={busy}
                                onClick={() => void changeExpertiseLink(item, null)}
                                type="button"
                              >
                                Убрать
                              </button>
                            </article>
                          ))}
                        </div>
                      ) : (
                        <p>Связанных экспертиз пока нет</p>
                      )}
                      <CustomerReturnExpertisePicker
                        disabled={busy}
                        onSelect={(item) =>
                          void changeExpertiseLink(item, detail.serviceRequest!.item_id)
                        }
                      />
                    </div>
                  ) : null}
                </div>
              ) : null}

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
          serviceLinksEnabled={serviceLinksEnabled}
          showTestingGuide={showTestingGuide}
        />
      )}
    </div>
  );
}
