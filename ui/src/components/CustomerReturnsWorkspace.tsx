import { useCallback, useEffect, useState } from "react";
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
  shipment: CustomerReturnShipment
) {
  return [shipment, ...current.filter((item) => item.id !== shipment.id)];
}

export function CustomerReturnsWorkspace() {
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
      setReturns((current) => upsertShipment(current, data.shipment));
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

  const openDetail = async (shipment: CustomerReturnShipment) => {
    if (busy) return;
    setBusy(true);
    try {
      const { data } = await api.get<CustomerReturnDetail>(
        `/bitrix/logistics/customer-returns/${shipment.id}`
      );
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
      setReturns((current) => upsertShipment(current, data));
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
              <button className="btn btn--ghost" type="button" disabled={busy} onClick={() => void openDetail(shipment)}>
                Открыть карточку
              </button>
            </article>
          ))}
        </div>
      </section>

      {detail && (
        <section className="logistics-card customer-returns__detail">
          <div className="logistics-card__heading">
            <div>
              <span className="logistics-step">3</span>
              <h2>Возврат {detail.tracking_number}</h2>
            </div>
            <button className="btn btn--ghost" type="button" onClick={() => setDetail(null)}>
              Закрыть
            </button>
          </div>
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
        </section>
      )}

      {message && <div className="logistics-toast" role="status">{message}</div>}
    </div>
  );
}
