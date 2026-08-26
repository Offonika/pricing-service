import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isAxiosError } from "axios";
import { api } from "../api/client";
import type { BitrixLogisticsProfile } from "../api/bitrix";

type Warehouse = {
  id: number;
  external_id: string;
  name: string;
  kind: string;
  payload?: Record<string, unknown> | null;
};

type Driver = { id: number; full_name: string };

type Draft = {
  id: number;
  draft_type: "handoff" | "receipt";
  status: string;
  warehouse_id: number;
  driver_id: number | null;
  item_count: number;
  items: Array<{
    id: number;
    document_number: string;
    lookup_code?: string | null;
    barcode: string;
    final_recipient_name?: string | null;
    dropoff_warehouse_name?: string | null;
  }>;
};

type MonitorItem = {
  transfer_id: number;
  document_number: string;
  site_order_number?: string | null;
  status: string;
  current_warehouse_name?: string | null;
  dropoff_warehouse_name?: string | null;
  driver_name?: string | null;
  final_recipient_name?: string | null;
  last_event_at: string;
  manual_review_count: number;
};

type ExpectedItem = {
  transfer_id: number;
  document_number: string;
  site_order_number?: string | null;
  source_warehouse_name: string;
  dropoff_warehouse_name?: string | null;
  driver_name?: string | null;
  final_recipient_name?: string | null;
  last_event_at: string;
};

type HistoryEvent = {
  id: number;
  event_type: string;
  event_at: string;
  warehouse_name?: string | null;
  dropoff_warehouse_name?: string | null;
  driver_name?: string | null;
  user_name?: string | null;
  source: string;
  comment?: string | null;
};

type ManualReview = {
  id: number;
  review_type: string;
  reason: string;
  document_number?: string | null;
  source_external_id?: string | null;
  created_at: string;
};

type Bootstrap = {
  profile: BitrixLogisticsProfile;
  warehouses: Warehouse[];
  drivers: Driver[];
  capabilities: string[];
};

type Screen = "operation" | "expected" | "transit" | "history" | "errors";
type Operation = "handoff" | "receipt";

type BarcodeDetectorResult = { rawValue?: string };
type BarcodeDetectorInstance = {
  detect(source: HTMLVideoElement): Promise<BarcodeDetectorResult[]>;
};
type BarcodeDetectorConstructor = new (options?: {
  formats?: string[];
}) => BarcodeDetectorInstance;
type ScannerControls = { stop(): void };

const STATUS_LABELS: Record<string, string> = {
  at_warehouse: "На складе",
  in_transit: "В пути",
  accepted_at_point: "Принято",
  returned: "Возвращено",
};

function apiError(error: unknown) {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.message === "string") return detail.message;
  }
  return error instanceof Error ? error.message : "Неизвестная ошибка";
}

function newIdempotencyKey() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `logistics-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}

function CameraScanner({ onCode, onClose }: { onCode: (code: string) => void; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(true);

  useEffect(() => {
    let active = true;
    let stream: MediaStream | null = null;
    let intervalId: number | null = null;
    let controls: ScannerControls | null = null;
    const video = videoRef.current;
    if (!video) return;

    const finish = (rawValue: string) => {
      const normalized = rawValue.trim();
      if (!active || !normalized) return;
      active = false;
      onCode(normalized);
      onClose();
    };

    const start = async () => {
      const Detector = (window as unknown as { BarcodeDetector?: BarcodeDetectorConstructor })
        .BarcodeDetector;
      try {
        if (Detector && navigator.mediaDevices?.getUserMedia) {
          stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: "environment" } },
            audio: false,
          });
          video.srcObject = stream;
          await video.play();
          const detector = new Detector({
            formats: ["qr_code", "code_128", "code_39", "ean_13", "ean_8"],
          });
          intervalId = window.setInterval(async () => {
            if (!active || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
            try {
              const results = await detector.detect(video);
              if (results[0]?.rawValue) finish(results[0].rawValue);
            } catch {
              // A single undecodable frame is normal.
            }
          }, 300);
          setStarting(false);
          return;
        }

        const { BrowserMultiFormatReader } = await import("@zxing/browser");
        const reader = new BrowserMultiFormatReader();
        controls = await reader.decodeFromVideoDevice(undefined, video, (result) => {
          if (result?.getText()) finish(result.getText());
        });
        setStarting(false);
      } catch (cameraError) {
        if (active) {
          setStarting(false);
          setError(apiError(cameraError));
        }
      }
    };
    void start();
    return () => {
      active = false;
      if (intervalId !== null) window.clearInterval(intervalId);
      controls?.stop();
      stream?.getTracks().forEach((track) => track.stop());
      if (video) video.srcObject = null;
    };
  }, [onClose, onCode]);

  const decodeFile = async (file: File | undefined) => {
    if (!file) return;
    const url = URL.createObjectURL(file);
    try {
      const { BrowserMultiFormatReader } = await import("@zxing/browser");
      const result = await new BrowserMultiFormatReader().decodeFromImageUrl(url);
      onCode(result.getText());
      onClose();
    } catch (decodeError) {
      setError(`Код на фото не распознан: ${apiError(decodeError)}`);
    } finally {
      URL.revokeObjectURL(url);
    }
  };

  return (
    <div className="logistics-camera" role="dialog" aria-modal="true" aria-label="Сканер кода">
      <div className="logistics-camera__sheet">
        <div className="logistics-camera__header">
          <strong>Наведите камеру на код</strong>
          <button className="btn btn--ghost" type="button" onClick={onClose}>
            Закрыть
          </button>
        </div>
        <video ref={videoRef} className="logistics-camera__video" muted playsInline />
        {starting && <p className="logistics__message">Запускаем камеру…</p>}
        {error && <p className="logistics__message logistics__message--error">{error}</p>}
        <label className="btn btn--ghost logistics-camera__file">
          Распознать код с фото
          <input
            type="file"
            accept="image/*"
            capture="environment"
            onChange={(event) => void decodeFile(event.target.files?.[0])}
          />
        </label>
      </div>
    </div>
  );
}

export function LogisticsWorkspace() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [screen, setScreen] = useState<Screen>("operation");
  const [operation, setOperation] = useState<Operation>("receipt");
  const [driverId, setDriverId] = useState("");
  const [dropoffWarehouseId, setDropoffWarehouseId] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [comment, setComment] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [confirmKey, setConfirmKey] = useState("");
  const [expected, setExpected] = useState<ExpectedItem[]>([]);
  const [transit, setTransit] = useState<MonitorItem[]>([]);
  const [errors, setErrors] = useState<ManualReview[]>([]);
  const [history, setHistory] = useState<HistoryEvent[]>([]);
  const [historyTitle, setHistoryTitle] = useState("");
  const [message, setMessage] = useState("Загрузка…");
  const [busy, setBusy] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);

  const capabilities = useMemo(
    () => new Set(bootstrap?.capabilities || []),
    [bootstrap?.capabilities]
  );
  const warehouseId = bootstrap?.profile.default_warehouse_id ?? null;

  const loadLists = useCallback(
    async (target: Screen) => {
      if (!bootstrap) return;
      if (target === "expected" && capabilities.has("expected")) {
        const params = warehouseId ? { warehouse_id: warehouseId } : undefined;
        setExpected((await api.get<ExpectedItem[]>("/bitrix/logistics/expected-deliveries", { params })).data);
      }
      if (target === "transit") {
        const params = { status: "in_transit", ...(warehouseId ? { warehouse_id: warehouseId } : {}) };
        setTransit((await api.get<MonitorItem[]>("/bitrix/logistics/monitor", { params })).data);
      }
      if (target === "errors" && capabilities.has("errors")) {
        setErrors((await api.get<ManualReview[]>("/bitrix/logistics/errors")).data);
      }
    },
    [bootstrap, capabilities, warehouseId]
  );

  useEffect(() => {
    let cancelled = false;
    api
      .get<Bootstrap>("/bitrix/logistics/bootstrap")
      .then(({ data }) => {
        if (cancelled) return;
        setBootstrap(data);
        const initialOperation = data.capabilities.includes("handoff") ? "handoff" : "receipt";
        setOperation(initialOperation);
        if (!data.capabilities.includes("handoff") && !data.capabilities.includes("receipt")) {
          setScreen("transit");
        }
        setDriverId(String(data.drivers[0]?.id || ""));
        const firstDropoff = data.warehouses.find((item) => item.id !== data.profile.default_warehouse_id);
        setDropoffWarehouseId(String(firstDropoff?.id || data.warehouses[0]?.id || ""));
        setMessage("");
      })
      .catch((error: unknown) => {
        if (!cancelled) setMessage(apiError(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void loadLists(screen).catch((error: unknown) => setMessage(apiError(error)));
  }, [loadLists, screen]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      setBusy(false);
    }
  };

  const createDraft = () =>
    run(async () => {
      if (!warehouseId) throw new Error("Для профиля не назначен склад");
      const payload =
        operation === "handoff"
          ? {
              warehouse_id: warehouseId,
              driver_id: Number(driverId),
              default_dropoff_warehouse_id: Number(dropoffWarehouseId),
              comment: comment || null,
            }
          : { warehouse_id: warehouseId, comment: comment || null };
      const { data } = await api.post<Draft>(
        `/bitrix/logistics/${operation === "handoff" ? "handoffs" : "receipts"}/draft`,
        payload
      );
      setDraft(data);
      setConfirmKey(newIdempotencyKey());
      setMessage(`Черновик №${data.id} открыт`);
    });

  const scan = (overrideCode?: string) =>
    run(async () => {
      const code = (overrideCode || scanCode).trim();
      if (!draft || !code) return;
      const base = operation === "handoff" ? "handoffs" : "receipts";
      const { data } = await api.post<Draft>(
        `/bitrix/logistics/${base}/draft/${draft.id}/scan`,
        {
          lookup_code: code,
          dropoff_warehouse_id:
            operation === "handoff" ? Number(dropoffWarehouseId) : null,
        }
      );
      setDraft(data);
      setScanCode("");
      setMessage(`Добавлено: ${data.item_count}`);
    });

  const confirm = () =>
    run(async () => {
      if (!draft) return;
      const base = operation === "handoff" ? "handoffs" : "receipts";
      const { data } = await api.post<{ processed_count: number }>(
        `/bitrix/logistics/${base}/draft/${draft.id}/confirm`,
        { comment: comment || null, idempotency_key: confirmKey }
      );
      setDraft(null);
      setConfirmKey("");
      setMessage(`Подтверждено: ${data.processed_count}`);
      await loadLists("transit");
    });

  const openHistory = (transferId: number, title: string) =>
    run(async () => {
      const { data } = await api.get<HistoryEvent[]>(
        `/bitrix/logistics/transfers/${transferId}/history`
      );
      setHistory(data);
      setHistoryTitle(title);
      setScreen("history");
      setMessage("");
    });

  const openFallback = () =>
    run(async () => {
      const { data } = await api.post<{ url: string }>("/bitrix/logistics/fallback-link");
      const popup = window.open(data.url, "_blank", "noopener,noreferrer");
      if (!popup) window.location.assign(data.url);
    });

  if (!bootstrap) {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Логистика</h1>
          <p>{message}</p>
          <small>Откройте приложение из левого меню Bitrix24.</small>
        </div>
      </div>
    );
  }

  const nav: Array<{ id: Screen; label: string; show: boolean }> = [
    { id: "operation", label: "Сканирование", show: capabilities.has("handoff") || capabilities.has("receipt") },
    { id: "expected", label: "Ожидаются", show: capabilities.has("expected") },
    { id: "transit", label: "В пути", show: capabilities.has("monitor") },
    { id: "history", label: "История", show: capabilities.has("history") },
    { id: "errors", label: "Ошибки", show: capabilities.has("errors") },
  ];

  return (
    <div className="app logistics logistics--bitrix">
      <header className="logistics-header">
        <div>
          <span className="logistics-header__eyebrow">Внутренние перемещения</span>
          <h1>Логистика</h1>
          <p>{bootstrap.profile.default_warehouse_name || "Все склады"}</p>
        </div>
        <div className="logistics-header__user">
          <strong>{bootstrap.profile.full_name}</strong>
          <span>{bootstrap.profile.role}</span>
        </div>
      </header>

      <nav className="logistics-nav" aria-label="Разделы логистики">
        {nav.filter((item) => item.show).map((item) => (
          <button
            key={item.id}
            type="button"
            className={screen === item.id ? "logistics-nav__item is-active" : "logistics-nav__item"}
            onClick={() => setScreen(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="logistics-mobile-content">
        {screen === "operation" && (
          <section className="logistics-card">
            <div className="logistics-card__heading">
              <div>
                <span className="logistics-step">1</span>
                <h2>{operation === "handoff" ? "Передать водителю" : "Принять в магазине"}</h2>
              </div>
              {!draft && capabilities.has("handoff") && capabilities.has("receipt") && (
                <select value={operation} onChange={(event) => setOperation(event.target.value as Operation)}>
                  <option value="handoff">Передача</option>
                  <option value="receipt">Приёмка</option>
                </select>
              )}
            </div>

            {!draft ? (
              <div className="logistics-form">
                <div className="logistics-field logistics-field--fixed">
                  <span>Склад</span>
                  <strong>{bootstrap.profile.default_warehouse_name}</strong>
                </div>
                {operation === "handoff" && (
                  <>
                    <label className="logistics-field">
                      <span>Водитель</span>
                      <select value={driverId} onChange={(event) => setDriverId(event.target.value)}>
                        {bootstrap.drivers.map((driver) => (
                          <option key={driver.id} value={driver.id}>{driver.full_name}</option>
                        ))}
                      </select>
                    </label>
                    <label className="logistics-field">
                      <span>Магазин назначения</span>
                      <select value={dropoffWarehouseId} onChange={(event) => setDropoffWarehouseId(event.target.value)}>
                        {bootstrap.warehouses.filter((warehouse) => warehouse.id !== warehouseId).map((warehouse) => (
                          <option key={warehouse.id} value={warehouse.id}>{warehouse.name}</option>
                        ))}
                      </select>
                    </label>
                  </>
                )}
                <label className="logistics-field">
                  <span>Комментарий</span>
                  <input value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Необязательно" />
                </label>
                <button className="btn logistics-primary" type="button" disabled={busy} onClick={createDraft}>
                  Начать сканирование
                </button>
              </div>
            ) : (
              <div className="logistics-draft-mobile">
                <div className="logistics-draft-mobile__summary">
                  <div><span className="logistics-step">2</span><strong>Черновик №{draft.id}</strong></div>
                  <b>{draft.item_count}</b>
                </div>
                <div className="logistics-scan-row">
                  <input
                    autoFocus
                    inputMode="text"
                    autoComplete="off"
                    value={scanCode}
                    onChange={(event) => setScanCode(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") void scan();
                    }}
                    placeholder="QR, штрихкод или номер"
                  />
                  <button className="btn" type="button" onClick={() => setCameraOpen(true)} aria-label="Открыть камеру">
                    Камера
                  </button>
                </div>
                <button className="btn btn--ghost logistics-scan-submit" type="button" disabled={!scanCode.trim() || busy} onClick={() => void scan()}>
                  Добавить код
                </button>
                <div className="logistics-items">
                  {draft.items.map((item, index) => (
                    <article key={item.id}>
                      <span>{index + 1}</span>
                      <div>
                        <strong>{item.document_number}</strong>
                        <small>{item.lookup_code || item.barcode}</small>
                      </div>
                      <b>Принят</b>
                    </article>
                  ))}
                </div>
                <button className="btn logistics-primary" type="button" disabled={!draft.item_count || busy} onClick={confirm}>
                  Подтвердить {draft.item_count ? `(${draft.item_count})` : ""}
                </button>
              </div>
            )}
          </section>
        )}

        {screen === "expected" && (
          <LogisticsList
            title="Ожидаемые поступления"
            empty="На выбранный склад ничего не ожидается"
            items={expected.map((item) => ({
              id: item.transfer_id,
              title: item.document_number,
              subtitle: `${item.source_warehouse_name} → ${item.dropoff_warehouse_name || "магазин"}`,
              meta: `${item.driver_name || "Водитель не указан"} · ${formatDate(item.last_event_at)}`,
            }))}
            onHistory={(id, title) => void openHistory(id, title)}
          />
        )}

        {screen === "transit" && (
          <LogisticsList
            title="В пути"
            empty="Активных перемещений нет"
            items={transit.map((item) => ({
              id: item.transfer_id,
              title: item.document_number,
              subtitle: item.dropoff_warehouse_name || item.current_warehouse_name || "Точка не указана",
              meta: `${item.driver_name || STATUS_LABELS[item.status] || item.status} · ${formatDate(item.last_event_at)}`,
              warning: item.manual_review_count ? `Ошибок: ${item.manual_review_count}` : undefined,
            }))}
            onHistory={(id, title) => void openHistory(id, title)}
          />
        )}

        {screen === "history" && (
          <section className="logistics-card">
            <div className="logistics-card__heading"><h2>История {historyTitle}</h2></div>
            {!history.length && <p className="logistics-empty">Выберите документ в списке, чтобы увидеть его историю.</p>}
            <div className="logistics-timeline">
              {history.map((event) => (
                <article key={event.id}>
                  <i />
                  <div>
                    <strong>{event.event_type}</strong>
                    <span>{event.warehouse_name || event.dropoff_warehouse_name || "Без точки"}</span>
                    <small>{formatDate(event.event_at)} · {event.user_name || event.source}</small>
                    {event.comment && <p>{event.comment}</p>}
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        {screen === "errors" && (
          <section className="logistics-card">
            <div className="logistics-card__heading"><h2>Ошибки и ручная проверка</h2></div>
            {!errors.length && <p className="logistics-empty">Открытых ошибок нет</p>}
            <div className="logistics-errors">
              {errors.map((item) => (
                <article key={item.id}>
                  <strong>{item.document_number || item.source_external_id || `Ошибка №${item.id}`}</strong>
                  <p>{item.reason}</p>
                  <small>{item.review_type} · {formatDate(item.created_at)}</small>
                </article>
              ))}
            </div>
          </section>
        )}

        {message && <div className="logistics-toast" role="status">{message}</div>}
        <button className="logistics-fallback-link" type="button" disabled={busy} onClick={openFallback}>
          Открыть сканер в браузере
        </button>
      </main>
      {cameraOpen && (
        <CameraScanner
          onCode={(code) => {
            setScanCode(code);
            void scan(code);
          }}
          onClose={() => setCameraOpen(false)}
        />
      )}
    </div>
  );
}

function LogisticsList({
  title,
  empty,
  items,
  onHistory,
}: {
  title: string;
  empty: string;
  items: Array<{ id: number; title: string; subtitle: string; meta: string; warning?: string }>;
  onHistory: (id: number, title: string) => void;
}) {
  return (
    <section className="logistics-card">
      <div className="logistics-card__heading"><h2>{title}</h2><b>{items.length}</b></div>
      {!items.length && <p className="logistics-empty">{empty}</p>}
      <div className="logistics-mobile-list">
        {items.map((item) => (
          <article key={item.id}>
            <div>
              <strong>{item.title}</strong>
              <span>{item.subtitle}</span>
              <small>{item.meta}</small>
              {item.warning && <em>{item.warning}</em>}
            </div>
            <button className="btn btn--ghost" type="button" onClick={() => onHistory(item.id, item.title)}>
              История
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}
