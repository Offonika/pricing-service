import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isAxiosError } from "axios";
import { logisticsApi as api } from "../api/logistics";
import type { BitrixLogisticsProfile } from "../api/bitrix";
import { CustomerReturnsWorkspace } from "./CustomerReturnsWorkspace";

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
  document_number?: string | null;
  rtu_number?: string | null;
  onec_order_number?: string | null;
  site_order_number?: string | null;
  source_warehouse_name?: string | null;
  delivery_method?: string | null;
  created_at: string;
};

type ManualReviewPage = {
  items: ManualReview[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
};

type Bootstrap = {
  profile: BitrixLogisticsProfile;
  warehouses: Warehouse[];
  drivers: Driver[];
  capabilities: string[];
  open_draft?: Draft | null;
};

type Screen = "operation" | "expected" | "transit" | "history" | "errors" | "returns";
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

const EVENT_LABELS: Record<string, string> = {
  created: "Создано перемещение",
  ready_at_warehouse: "Готово к передаче",
  handed_to_driver: "Передано водителю",
  pickup_moving_to_point: "В пути в магазин",
  arrived_at_point: "Прибыло в магазин",
  accepted_at_point: "Принято в магазине",
  pickup_stored_at_point: "Размещено на хранение",
  returned: "Возвращено",
};

const SOURCE_LABELS: Record<string, string> = {
  bitrix: "Bitrix24",
  telegram: "Telegram",
  web_fallback: "Браузер",
  onec: "1С",
  system: "Система",
};

const ROLE_LABELS: Record<string, string> = {
  sender: "Отправитель",
  receiver: "Получатель",
  logist: "Логист",
  returns: "Онлайн-отдел",
  admin: "Администратор",
};

const REVIEW_PAGE_SIZE = 30;
const EMPTY_REVIEW_PAGE: ManualReviewPage = {
  items: [],
  total: 0,
  limit: REVIEW_PAGE_SIZE,
  offset: 0,
  counts: {},
};
const REVIEW_FILTERS = [
  { value: "", label: "Все причины" },
  { value: "rtu_target_warehouse_unresolved", label: "Не определён магазин" },
  { value: "rtu_external_carrier_unmapped", label: "Внешняя доставка" },
  { value: "rtu_external_carrier_state_conflict", label: "Конфликт внешней доставки" },
  { value: "rtu_without_site_order", label: "Нет номера интернет-заказа" },
  { value: "rtu_lookup_not_unique", label: "Неоднозначный код" },
  { value: "rtu_source_invalid", label: "Неполные данные РТУ" },
  { value: "rtu_source_warehouse_unresolved", label: "Не определён склад" },
  { value: "rtu_readiness_gate_failed", label: "Устаревшее ожидание готовности" },
  { value: "unknown_qr", label: "Код не найден" },
  { value: "ambiguous_qr", label: "Неоднозначный QR-код" },
  { value: "onec_reconciliation_conflict", label: "Расхождение с 1С" },
  { value: "site_order_stage_conflict", label: "Конфликт стадии сделки" },
  { value: "manual_ready_override", label: "Ручная отметка готовности" },
];

const REVIEW_COPY: Record<string, { title: string; description: string; action: string }> = {
  rtu_target_warehouse_unresolved: {
    title: "Не определён магазин",
    description: "Адрес самовывоза не сопоставился с одной торговой точкой.",
    action: "Нужно проверить адрес и выбрать точку",
  },
  rtu_external_carrier_unmapped: {
    title: "Внешняя доставка",
    description: "Для РТУ указан внешний перевозчик без подтверждённого маршрута.",
    action: "Нужно проверить способ доставки",
  },
  rtu_external_carrier_state_conflict: {
    title: "Конфликт внешней доставки",
    description: "Состояние документа не позволяет подтвердить передачу перевозчику.",
    action: "Нужно проверить состояние РТУ и перевозчика",
  },
  rtu_without_site_order: {
    title: "Нет номера интернет-заказа",
    description: "РТУ похожа на интернет-заказ, но номер заказа сайта не заполнен.",
    action: "Нужно проверить связанный заказ в 1С",
  },
  rtu_lookup_not_unique: {
    title: "Неоднозначный код",
    description: "Один код сканирования соответствует нескольким документам.",
    action: "Нужно проверить РТУ и её штрихкод",
  },
  rtu_source_warehouse_unresolved: {
    title: "Не определён склад",
    description: "Склад РТУ отсутствует в справочнике логистики.",
    action: "Нужно сопоставить склад 1С",
  },
  rtu_source_invalid: {
    title: "Неполные данные РТУ",
    description: "В источнике не хватает обязательных реквизитов документа.",
    action: "Нужно проверить документ в 1С",
  },
  rtu_readiness_gate_failed: {
    title: "Устаревшая запись ожидания",
    description: "РТУ ещё не была распечатана или собрана и не требует ручного исправления.",
    action: "Запись будет закрыта технической очисткой",
  },
  unknown_qr: {
    title: "Код не найден",
    description: "Отсканированный код не найден среди документов логистики.",
    action: "Нужно проверить документ и качество кода",
  },
  ambiguous_qr: {
    title: "Неоднозначный QR-код",
    description: "Отсканированный код связан с несколькими документами.",
    action: "Нужно проверить документы с этим кодом",
  },
  onec_reconciliation_conflict: {
    title: "Расхождение с 1С",
    description: "Текущее состояние логистики расходится с данными документа 1С.",
    action: "Нужно сверить документ и события логистики",
  },
  site_order_stage_conflict: {
    title: "Конфликт стадии сделки",
    description: "Сделку нельзя безопасно перевести на ожидаемую стадию.",
    action: "Нужно проверить сделку в Bitrix24",
  },
  manual_ready_override: {
    title: "Ручная отметка готовности",
    description: "Для документа зафиксировано ручное подтверждение готовности.",
    action: "Нужно проверить основание ручного подтверждения",
  },
};

function reviewCopy(reviewType: string) {
  return REVIEW_COPY[reviewType] || {
    title: "Требуется проверка",
    description: "Документ не прошёл автоматическую проверку логистики.",
    action: "Нужно проверить данные документа",
  };
}

const API_ERROR_COPY: Record<string, string> = {
  "transfer not found by lookup code":
    "QR распознан, но документ ещё не загружен. Повторите через минуту",
  "transfer is already accepted earlier": "Документ уже принят в этом магазине",
};

function apiError(error: unknown) {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return API_ERROR_COPY[detail] || detail;
    if (detail && typeof detail.message === "string") return detail.message;
  }
  return error instanceof Error ? error.message : "Неизвестная ошибка";
}

function cameraErrorMessage(error: unknown) {
  const name =
    typeof error === "object" && error !== null && "name" in error
      ? String((error as { name?: unknown }).name || "")
      : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "Нет доступа к камере. Разрешите камеру для Bitrix24 или откройте сканер в браузере.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "Камера не найдена. Используйте фото или ручной ввод кода.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "Камера занята другим приложением. Закройте его и повторите.";
  }
  return "Не удалось открыть камеру. Используйте фото, ручной ввод или внешний браузер.";
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

function formatReviewDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "long",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
}

export function CameraScanner({ onCode, onClose }: { onCode: (code: string) => void; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const onCodeRef = useRef(onCode);
  const onCloseRef = useRef(onClose);
  const mountedRef = useRef(true);
  const decodeRequestRef = useRef(0);
  const stopCameraRef = useRef<() => void>(() => undefined);
  const closedRef = useRef(false);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(true);

  const closeScanner = useCallback(() => {
    if (closedRef.current) return;
    closedRef.current = true;
    decodeRequestRef.current += 1;
    stopCameraRef.current();
    onCloseRef.current();
  }, []);

  useEffect(() => {
    onCodeRef.current = onCode;
    onCloseRef.current = onClose;
  }, [onClose, onCode]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      decodeRequestRef.current += 1;
    };
  }, []);

  useEffect(() => {
    let active = true;
    let stream: MediaStream | null = null;
    let intervalId: number | null = null;
    let controls: ScannerControls | null = null;
    const video = videoRef.current;
    if (!video) return;

    const stopCamera = () => {
      active = false;
      if (intervalId !== null) window.clearInterval(intervalId);
      intervalId = null;
      controls?.stop();
      controls = null;
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
      video.srcObject = null;
    };
    stopCameraRef.current = stopCamera;

    const finish = (rawValue: string) => {
      const normalized = rawValue.trim();
      if (!active || !normalized) return;
      onCodeRef.current(normalized);
      closeScanner();
    };

    const start = async () => {
      const Detector = (window as unknown as { BarcodeDetector?: BarcodeDetectorConstructor })
        .BarcodeDetector;
      try {
        if (Detector && navigator.mediaDevices?.getUserMedia) {
          const requestedStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: "environment" } },
            audio: false,
          });
          if (!active) {
            requestedStream.getTracks().forEach((track) => track.stop());
            return;
          }
          stream = requestedStream;
          video.srcObject = stream;
          await video.play();
          if (!active) {
            stream.getTracks().forEach((track) => track.stop());
            return;
          }
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
        const requestedControls = await reader.decodeFromConstraints(
          {
            video: { facingMode: { ideal: "environment" } },
            audio: false,
          },
          video,
          (result) => {
            if (result?.getText()) finish(result.getText());
          }
        );
        if (!active) {
          requestedControls.stop();
          return;
        }
        controls = requestedControls;
        setStarting(false);
      } catch (cameraError) {
        if (active) {
          setStarting(false);
          setError(cameraErrorMessage(cameraError));
        }
      }
    };
    void start();
    return () => {
      stopCamera();
      if (stopCameraRef.current === stopCamera) {
        stopCameraRef.current = () => undefined;
      }
    };
  }, [closeScanner]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeScanner();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [closeScanner]);

  const decodeFile = async (file: File | undefined) => {
    if (!file) return;
    const requestId = ++decodeRequestRef.current;
    const url = URL.createObjectURL(file);
    try {
      const { BrowserMultiFormatReader } = await import("@zxing/browser");
      if (!mountedRef.current || requestId !== decodeRequestRef.current) return;
      const result = await new BrowserMultiFormatReader().decodeFromImageUrl(url);
      if (!mountedRef.current || requestId !== decodeRequestRef.current) return;
      onCodeRef.current(result.getText());
      closeScanner();
    } catch (decodeError) {
      if (mountedRef.current && requestId === decodeRequestRef.current) {
        setError(`Код на фото не распознан: ${apiError(decodeError)}`);
      }
    } finally {
      URL.revokeObjectURL(url);
    }
  };

  return (
    <div className="logistics-camera" role="dialog" aria-modal="true" aria-label="Сканер кода">
      <div className="logistics-camera__sheet">
        <div className="logistics-camera__header">
          <strong>Наведите камеру на код</strong>
          <button className="btn btn--ghost" type="button" onClick={closeScanner}>
            Назад
          </button>
        </div>
        <video ref={videoRef} className="logistics-camera__video" muted playsInline />
        {starting && <p className="logistics__message">Запускаем камеру…</p>}
        {error && <p className="logistics__message logistics__message--error">{error}</p>}
        <div className="logistics-camera__actions">
          <label className="btn btn--ghost logistics-camera__file">
            Распознать код с фото
            <input
              type="file"
              accept="image/*"
              capture="environment"
              onChange={(event) => void decodeFile(event.target.files?.[0])}
            />
          </label>
          <button className="btn btn--ghost" type="button" onClick={closeScanner}>
            Отменить сканирование
          </button>
        </div>
      </div>
    </div>
  );
}

export function LogisticsWorkspace() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [screen, setScreen] = useState<Screen>("operation");
  const [operation, setOperation] = useState<Operation>("receipt");
  const [driverId, setDriverId] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [comment, setComment] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [confirmKey, setConfirmKey] = useState("");
  const [expected, setExpected] = useState<ExpectedItem[]>([]);
  const [transit, setTransit] = useState<MonitorItem[]>([]);
  const [reviewPage, setReviewPage] = useState<ManualReviewPage>(EMPTY_REVIEW_PAGE);
  const [reviewFilter, setReviewFilter] = useState("");
  const [reviewsLoading, setReviewsLoading] = useState(false);
  const [history, setHistory] = useState<HistoryEvent[]>([]);
  const [historyTitle, setHistoryTitle] = useState("");
  const [message, setMessage] = useState("Загрузка…");
  const [busy, setBusy] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const [selectedWarehouseId, setSelectedWarehouseId] = useState("");
  const reviewRequestId = useRef(0);
  const operationInFlight = useRef(false);

  const capabilities = useMemo(
    () => new Set(bootstrap?.capabilities || []),
    [bootstrap?.capabilities]
  );
  const warehouseId = selectedWarehouseId ? Number(selectedWarehouseId) : null;
  const listWarehouseId = warehouseId;
  const selectedWarehouse = bootstrap?.warehouses.find(
    (warehouse) => warehouse.id === warehouseId
  );

  const loadReviews = useCallback(
    async (offset = 0, append = false) => {
      if (!capabilities.has("errors")) return;
      const requestId = ++reviewRequestId.current;
      setReviewsLoading(true);
      try {
        const params: Record<string, string | number> = {
          limit: REVIEW_PAGE_SIZE,
          offset,
        };
        if (reviewFilter) params.review_type = reviewFilter;
        const { data } = await api.get<ManualReviewPage>("/bitrix/logistics/errors", {
          params,
        });
        if (requestId !== reviewRequestId.current) return;
        setReviewPage((current) =>
          append ? { ...data, items: [...current.items, ...data.items] } : data
        );
      } catch (error) {
        if (requestId !== reviewRequestId.current) return;
        throw error;
      } finally {
        if (requestId === reviewRequestId.current) setReviewsLoading(false);
      }
    },
    [capabilities, reviewFilter]
  );

  const changeReviewFilter = (value: string) => {
    reviewRequestId.current += 1;
    setReviewFilter(value);
    setReviewPage((current) => ({ ...EMPTY_REVIEW_PAGE, counts: current.counts }));
    setReviewsLoading(true);
  };

  const loadLists = useCallback(
    async (target: Screen) => {
      if (!bootstrap) return;
      if (target === "expected" && capabilities.has("expected")) {
        const params = listWarehouseId ? { warehouse_id: listWarehouseId } : undefined;
        setExpected((await api.get<ExpectedItem[]>("/bitrix/logistics/expected-deliveries", { params })).data);
      }
      if (target === "transit") {
        const params = { status: "in_transit", ...(listWarehouseId ? { warehouse_id: listWarehouseId } : {}) };
        setTransit((await api.get<MonitorItem[]>("/bitrix/logistics/monitor", { params })).data);
      }
      if (target === "errors" && capabilities.has("errors")) {
        await loadReviews();
      }
    },
    [bootstrap, capabilities, listWarehouseId, loadReviews]
  );

  useEffect(() => {
    let cancelled = false;
    api
      .get<Bootstrap>("/bitrix/logistics/bootstrap")
      .then(({ data }) => {
        if (cancelled) return;
        setBootstrap(data);
        const availableWarehouseIds = new Set(
          data.warehouses.map((warehouse) => warehouse.id)
        );
        const draftWarehouseId = data.open_draft?.warehouse_id;
        const defaultWarehouseId = data.profile.default_warehouse_id;
        const initialWarehouseId =
          (draftWarehouseId && availableWarehouseIds.has(draftWarehouseId)
            ? draftWarehouseId
            : null) ||
          (defaultWarehouseId && availableWarehouseIds.has(defaultWarehouseId)
            ? defaultWarehouseId
            : null) ||
          (["admin", "logist"].includes(data.profile.role)
            ? data.warehouses[0]?.id
            : null);
        setSelectedWarehouseId(String(initialWarehouseId || ""));
        const initialOperation =
          data.open_draft?.draft_type ||
          (data.capabilities.includes("handoff") ? "handoff" : "receipt");
        setOperation(initialOperation);
        if (!data.capabilities.includes("handoff") && !data.capabilities.includes("receipt")) {
          setScreen(data.capabilities.includes("customer_returns") ? "returns" : "transit");
        }
        setDraft(data.open_draft || null);
        setConfirmKey(data.open_draft ? newIdempotencyKey() : "");
        setDriverId(String(data.open_draft?.driver_id || data.drivers[0]?.id || ""));
        setMessage(data.open_draft ? `Черновик №${data.open_draft.id} восстановлен` : "");
      })
      .catch((error: unknown) => {
        if (!cancelled) setMessage(apiError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [bootstrapAttempt]);

  useEffect(() => {
    void loadLists(screen).catch((error: unknown) => setMessage(apiError(error)));
  }, [loadLists, screen]);

  const run = async (action: () => Promise<void>) => {
    if (operationInFlight.current) return;
    operationInFlight.current = true;
    setBusy(true);
    try {
      await action();
    } catch (error) {
      setMessage(apiError(error));
    } finally {
      operationInFlight.current = false;
      setBusy(false);
    }
  };

  const createDraft = () =>
    run(async () => {
      if (!warehouseId) throw new Error("Для профиля не назначен склад");
      if (operation === "handoff" && !driverId) {
        throw new Error("Нет активного водителя для передачи");
      }
      const payload =
        operation === "handoff"
          ? {
              warehouse_id: warehouseId,
              driver_id: Number(driverId),
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
      const base = draft.draft_type === "handoff" ? "handoffs" : "receipts";
      const { data } = await api.post<Draft>(
        `/bitrix/logistics/${base}/draft/${draft.id}/scan`,
        { lookup_code: code }
      );
      setDraft(data);
      setScanCode("");
      setMessage(`Добавлено: ${data.item_count}`);
    });

  const confirm = () =>
    run(async () => {
      if (!draft) return;
      const base = draft.draft_type === "handoff" ? "handoffs" : "receipts";
      const { data } = await api.post<{ processed_count: number }>(
        `/bitrix/logistics/${base}/draft/${draft.id}/confirm`,
        { comment: comment || null, idempotency_key: confirmKey }
      );
      setDraft(null);
      setConfirmKey("");
      setMessage(`Подтверждено: ${data.processed_count}`);
      await loadLists("transit");
    });

  const removeDraftItem = (itemId: number) =>
    run(async () => {
      if (!draft) return;
      const base = draft.draft_type === "handoff" ? "handoffs" : "receipts";
      const { data } = await api.post<Draft>(
        `/bitrix/logistics/${base}/draft/${draft.id}/items/${itemId}/remove`
      );
      setDraft(data);
      setMessage("Ошибочный скан удалён");
    });

  const cancelDraft = () => {
    if (!draft || !window.confirm("Отменить этот черновик и начать заново?")) return;
    void run(async () => {
      const base = draft.draft_type === "handoff" ? "handoffs" : "receipts";
      await api.post(`/bitrix/logistics/${base}/draft/${draft.id}/cancel`, {
        reason: "Отменено пользователем в Bitrix24",
      });
      setDraft(null);
      setConfirmKey("");
      setScanCode("");
      setMessage("Черновик отменён. Можно начать заново.");
    });
  };

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

  const openFallback = () => {
    if (operationInFlight.current) return;
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    return run(async () => {
      try {
        const { data } = await api.post<{ url: string }>("/bitrix/logistics/fallback-link");
        if (popup) popup.location.replace(data.url);
        else window.location.assign(data.url);
      } catch (error) {
        popup?.close();
        throw error;
      }
    });
  };

  const loadMoreReviews = () => {
    void loadReviews(reviewPage.items.length, true).catch((error: unknown) =>
      setMessage(apiError(error))
    );
  };

  if (!bootstrap) {
    const loading = message === "Загрузка…";
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Логистика</h1>
          <p>{message}</p>
          <small>Откройте приложение из левого меню Bitrix24.</small>
          {!loading && (
            <div className="app-state__actions">
              <button
                className="btn"
                type="button"
                onClick={() => {
                  setMessage("Загрузка…");
                  setBootstrapAttempt((attempt) => attempt + 1);
                }}
              >
                Повторить
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }

  const nav: Array<{ id: Screen; label: string; show: boolean }> = [
    { id: "operation", label: "Сканер", show: capabilities.has("handoff") || capabilities.has("receipt") },
    { id: "expected", label: "Ожидаются", show: capabilities.has("expected") },
    { id: "transit", label: "В пути", show: capabilities.has("monitor") },
    { id: "history", label: "История", show: capabilities.has("history") },
    { id: "errors", label: "Разбор", show: capabilities.has("errors") },
    { id: "returns", label: "Возвраты", show: capabilities.has("customer_returns") },
  ];

  return (
    <div className="app logistics logistics--bitrix">
      <header className="logistics-header">
        <div>
          <span className="logistics-header__eyebrow">
            {screen === "returns" ? "Клиентские возвраты" : "Внутренние перемещения"}
          </span>
          <h1>Логистика</h1>
          <p>
            {screen === "returns" ? "Единый реестр перевозчиков" : selectedWarehouse?.name ||
              bootstrap.profile.default_warehouse_name ||
              "Склад не выбран"}
          </p>
        </div>
        <div className="logistics-header__user">
          <strong>{bootstrap.profile.full_name}</strong>
          <span>{ROLE_LABELS[bootstrap.profile.role] || bootstrap.profile.role}</span>
        </div>
      </header>

      <nav className="logistics-nav" aria-label="Разделы логистики">
        {nav.filter((item) => item.show).map((item) => (
          <button
            key={item.id}
            type="button"
            className={screen === item.id ? "logistics-nav__item is-active" : "logistics-nav__item"}
            aria-current={screen === item.id ? "page" : undefined}
            onClick={() => setScreen(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <main className="logistics-mobile-content">
        {["admin", "logist"].includes(bootstrap.profile.role) &&
          !draft &&
          ["expected", "transit"].includes(screen) &&
          bootstrap.warehouses.length > 1 && (
            <section className="logistics-card">
              <label className="logistics-field">
                <span>Склад просмотра</span>
                <select
                  aria-label="Склад просмотра"
                  value={selectedWarehouseId}
                  onChange={(event) => setSelectedWarehouseId(event.target.value)}
                >
                  {bootstrap.warehouses.map((warehouse) => (
                    <option key={warehouse.id} value={warehouse.id}>
                      {warehouse.name}
                    </option>
                  ))}
                </select>
              </label>
            </section>
          )}
        {screen === "operation" && (
          <section className="logistics-card">
            <div className="logistics-card__heading">
              <div>
                <span className="logistics-step">1</span>
                <h2>{operation === "handoff" ? "Передать водителю" : "Принять в магазине"}</h2>
              </div>
              {!draft && capabilities.has("handoff") && capabilities.has("receipt") && (
                <select
                  aria-label="Операция"
                  value={operation}
                  onChange={(event) => setOperation(event.target.value as Operation)}
                >
                  <option value="handoff">Передача</option>
                  <option value="receipt">Приёмка</option>
                </select>
              )}
            </div>

            {!draft ? (
              <div className="logistics-form">
                <div className="logistics-field logistics-field--fixed">
                  <span>Склад</span>
                  {bootstrap.profile.role === "admin" && !draft ? (
                    <select
                      aria-label="Склад операции"
                      value={selectedWarehouseId}
                      onChange={(event) => setSelectedWarehouseId(event.target.value)}
                    >
                      {bootstrap.warehouses.map((warehouse) => (
                        <option key={warehouse.id} value={warehouse.id}>{warehouse.name}</option>
                      ))}
                    </select>
                  ) : (
                    <strong>
                      {bootstrap.warehouses.find((warehouse) => warehouse.id === warehouseId)?.name ||
                        bootstrap.profile.default_warehouse_name ||
                        "Не назначен"}
                    </strong>
                  )}
                  {!warehouseId && <small>Обратитесь к логисту для привязки склада</small>}
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
                      {!bootstrap.drivers.length && <small>Нет активных водителей</small>}
                    </label>
                    <div className="logistics-field logistics-field--fixed">
                      <span>Направление</span>
                      <strong>Определится автоматически после сканирования документа</strong>
                    </div>
                  </>
                )}
                <label className="logistics-field">
                  <span>Комментарий</span>
                  <input
                    value={comment}
                    maxLength={1000}
                    onChange={(event) => setComment(event.target.value)}
                    placeholder="Необязательно"
                  />
                </label>
                <button
                  className="btn logistics-primary"
                  type="button"
                  disabled={
                    busy ||
                    !warehouseId ||
                    (operation === "handoff" && !driverId)
                  }
                  onClick={createDraft}
                >
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
                    maxLength={255}
                    value={scanCode}
                    onChange={(event) => setScanCode(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") void scan();
                    }}
                    placeholder="QR, штрихкод или номер"
                  />
                  <button className="btn" type="button" disabled={busy} onClick={() => setCameraOpen(true)} aria-label="Открыть камеру">
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
                        <small>
                          {item.dropoff_warehouse_name
                            ? `Куда: ${item.dropoff_warehouse_name}`
                            : "Направление требует проверки"}
                        </small>
                      </div>
                      <button
                        className="btn btn--ghost"
                        type="button"
                        disabled={busy}
                        onClick={() => void removeDraftItem(item.id)}
                      >
                        Удалить
                      </button>
                    </article>
                  ))}
                </div>
                <button className="btn logistics-primary" type="button" disabled={!draft.item_count || busy} onClick={confirm}>
                  Подтвердить {draft.item_count ? `(${draft.item_count})` : ""}
                </button>
                <button className="btn btn--ghost" type="button" disabled={busy} onClick={cancelDraft}>
                  Отменить черновик
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
            <div className="logistics-card__heading">
              <h2>{historyTitle ? `История ${historyTitle}` : "История перемещения"}</h2>
            </div>
            {!history.length && <p className="logistics-empty">Выберите документ в списке, чтобы увидеть его историю.</p>}
            <div className="logistics-timeline">
              {history.map((event) => (
                <article key={event.id}>
                  <i />
                  <div>
                    <strong>{EVENT_LABELS[event.event_type] || event.event_type}</strong>
                    <span>
                      {event.warehouse_name && event.dropoff_warehouse_name
                        ? `${event.warehouse_name} → ${event.dropoff_warehouse_name}`
                        : event.warehouse_name || event.dropoff_warehouse_name || "Без точки"}
                    </span>
                    <small>
                      {formatDate(event.event_at)} · {event.user_name || SOURCE_LABELS[event.source] || event.source}
                    </small>
                    <small>
                      Событие №{event.id}
                      {event.driver_name ? ` · Водитель: ${event.driver_name}` : ""}
                    </small>
                    {event.comment && <p>{event.comment}</p>}
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        {screen === "errors" && (
          <section className="logistics-card">
            <div className="logistics-card__heading">
              <h2>Требуют разбора</h2>
              <b aria-label={`Открытых разборов: ${reviewPage.total}`}>{reviewPage.total}</b>
            </div>
            <div className="logistics-review-controls">
              <label className="logistics-field">
                <span>Причина</span>
                <select
                  value={reviewFilter}
                  onChange={(event) => changeReviewFilter(event.target.value)}
                >
                  {REVIEW_FILTERS.map((filter) => {
                    const count = filter.value
                      ? reviewPage.counts[filter.value] || 0
                      : Object.values(reviewPage.counts).reduce((sum, value) => sum + value, 0);
                    return (
                      <option key={filter.value || "all"} value={filter.value}>
                        {filter.label} ({count})
                      </option>
                    );
                  })}
                </select>
              </label>
              <button
                className="btn btn--ghost"
                type="button"
                disabled={reviewsLoading}
                onClick={() => void loadReviews().catch((error: unknown) => setMessage(apiError(error)))}
              >
                Обновить
              </button>
            </div>
            {reviewsLoading && !reviewPage.items.length && (
              <p className="logistics-empty" role="status">Загружаем очередь…</p>
            )}
            {!reviewsLoading && !reviewPage.items.length && (
              <p className="logistics-empty">Открытых разборов нет</p>
            )}
            <div className="logistics-errors">
              {reviewPage.items.map((item) => {
                const copy = reviewCopy(item.review_type);
                return (
                  <article key={item.id}>
                    <header>
                      <strong>{item.document_number || item.rtu_number || `Разбор №${item.id}`}</strong>
                      <span>{copy.title}</span>
                    </header>
                    <p>{copy.description}</p>
                    <div className="logistics-review-meta">
                      {item.onec_order_number && <span>Заказ 1С: {item.onec_order_number}</span>}
                      {item.site_order_number && <span>Заказ сайта: {item.site_order_number}</span>}
                      {item.source_warehouse_name && <span>Склад: {item.source_warehouse_name}</span>}
                      {item.delivery_method && <span>Доставка: {item.delivery_method}</span>}
                    </div>
                    <small>{copy.action} · {formatReviewDate(item.created_at)}</small>
                  </article>
                );
              })}
            </div>
            {reviewPage.items.length < reviewPage.total && (
              <button
                className="btn btn--ghost logistics-review-more"
                type="button"
                disabled={reviewsLoading}
                onClick={loadMoreReviews}
              >
                {reviewsLoading ? "Загружаем…" : `Показать ещё (${reviewPage.total - reviewPage.items.length})`}
              </button>
            )}
          </section>
        )}

        {screen === "returns" && (
          <CustomerReturnsWorkspace showTestingGuide={bootstrap.profile.role === "admin"} />
        )}

        {message && <div className="logistics-toast" role="status">{message}</div>}
        {(capabilities.has("handoff") || capabilities.has("receipt") || capabilities.has("monitor")) && (
          <button className="logistics-fallback-link" type="button" disabled={busy} onClick={openFallback}>
            {capabilities.has("handoff") || capabilities.has("receipt")
              ? "Открыть сканер в браузере"
              : "Открыть мониторинг в браузере"}
          </button>
        )}
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
