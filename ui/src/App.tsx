import "./App.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import { CompatibilityMappingSettings } from "./components/CompatibilityMappingSettings";
import { MatchingLayout } from "./components/MatchingLayout";
import { LogisticsWorkspace } from "./components/LogisticsWorkspace";
import { ExecutiveDashboard } from "./components/ExecutiveDashboard";
import { ProcurementAssortmentWorkspace } from "./components/ProcurementAssortmentWorkspace";
import { ProcurementOrderFormationWorkspace } from "./components/ProcurementOrderFormationWorkspace";
import { ProcurementProductInsights } from "./components/ProcurementProductInsights";
import { ProcurementLabelsApp } from "./components/ProcurementLabelsApp";
import { PropertyMappingSettings } from "./components/PropertyMappingSettings";
import { ReceivablesWorkplace } from "./components/ReceivablesWorkplace";
import { CustomerPriceTypesWorkspace } from "./components/CustomerPriceTypesWorkspace";
import {
  bindBitrixProcurementLabelsPlacement,
  getProcurementAssortmentItemId,
  getProcurementLabelsItemId,
  getProcurementProductId,
  initializeBitrixCustomerPriceTypesSession,
  initializeBitrixExecutiveDashboardSession,
  initializeBitrixMatchingSession,
  initializeBitrixLogisticsSession,
  initializeBitrixProcurementAssortmentSession,
  initializeBitrixProcurementOrderFormationSession,
  initializeBitrixProcurementLabelsSession,
  initializeBitrixReceivablesSession,
  isBitrixCustomerPriceTypesRoute,
  isBitrixExecutiveDashboardRoute,
  isBitrixMatchingRoute,
  isBitrixLogisticsRoute,
  isBitrixProcurementAssortmentRoute,
  isBitrixProcurementOrderFormationRoute,
  isBitrixProductInsightsPlacement,
  isBitrixProcurementLabelsRoute,
  isBitrixReceivablesRoute,
  type BitrixCustomerPriceTypesSessionResponse,
  type BitrixReceivablesSessionResponse,
  type BitrixExecutiveDashboardSessionResponse,
} from "./api/bitrix";
import type { ProductFacets, ProductRow, ProductSort } from "./api/types";
import { useSelectedProduct } from "./store/useSelectionStore";

const STATUS_OPTIONS = [
  { value: "", label: "Все статусы" },
  { value: "matched", label: "Сопоставлены" },
  { value: "manual", label: "Приняты вручную" },
  { value: "auto", label: "Приняты автоматически" },
  { value: "candidates", label: "Есть кандидаты" },
  { value: "live_candidates", label: "Есть в живом поиске" },
  { value: "none", label: "Нет пары" },
  { value: "uncertain", label: "Низкая уверенность" },
  { value: "ambiguous", label: "Неоднозначно" },
  { value: "multiple", label: "Несколько связей" },
];

const PRODUCT_SORT_OPTIONS: Array<{ value: ProductSort; label: string }> = [
  { value: "default", label: "Обычная очередь" },
  { value: "name_asc", label: "Название A-Z" },
];

const PRODUCT_LIST_PREFS_KEY = "pricing.matching.product-list.v1";
const PRODUCT_PAGE_SIZES = [25, 50, 100, 200];

interface ProductListPrefs {
  search: string;
  status: string;
  category: string;
  compatibilityBrand: string;
  subject: string;
  productSort: ProductSort;
  pageSize: number;
}

const DEFAULT_PRODUCT_LIST_PREFS: ProductListPrefs = {
  search: "",
  status: "",
  category: "",
  compatibilityBrand: "",
  subject: "",
  productSort: "default",
  pageSize: 50,
};

function stringPref(data: Record<string, unknown>, key: string) {
  const value = data[key];
  return typeof value === "string" ? value : "";
}

function isProductSort(value: unknown): value is ProductSort {
  return value === "default" || value === "name_asc";
}

function readProductListPrefs(): ProductListPrefs {
  if (typeof window === "undefined") return DEFAULT_PRODUCT_LIST_PREFS;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(PRODUCT_LIST_PREFS_KEY) || "{}") as Record<string, unknown>;
    const pageSize = typeof parsed.pageSize === "number" && PRODUCT_PAGE_SIZES.includes(parsed.pageSize)
      ? parsed.pageSize
      : DEFAULT_PRODUCT_LIST_PREFS.pageSize;
    return {
      search: stringPref(parsed, "search"),
      status: stringPref(parsed, "status"),
      category: stringPref(parsed, "category"),
      compatibilityBrand: stringPref(parsed, "compatibilityBrand"),
      subject: stringPref(parsed, "subject"),
      productSort: isProductSort(parsed.productSort) ? parsed.productSort : DEFAULT_PRODUCT_LIST_PREFS.productSort,
      pageSize,
    };
  } catch {
    return DEFAULT_PRODUCT_LIST_PREFS;
  }
}

function writeProductListPrefs(prefs: ProductListPrefs) {
  try {
    window.localStorage.setItem(PRODUCT_LIST_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage может быть недоступен в приватном режиме; интерфейс продолжит работать без памяти.
  }
}

const isLogisticsFallbackRoute = () => window.location.pathname.startsWith("/logistics/fallback");
const isReceivablesWorkplaceRoute = () => window.location.pathname.startsWith("/receivables/workplace");
const isExecutiveDashboardRoute = () => window.location.pathname.startsWith("/executive-dashboard");

type LogisticsProfile = {
  id: number;
  full_name: string;
  role: string;
  default_warehouse_id: number | null;
  default_warehouse_name: string | null;
};

type LogisticsWarehouse = {
  id: number;
  name: string;
  kind: string;
};

type LogisticsDriver = {
  id: number;
  full_name: string;
};

type LogisticsDraft = {
  id: number;
  draft_type: string;
  status: string;
  item_count: number;
  items: Array<{
    id: number;
    barcode: string;
    lookup_code?: string | null;
    document_number: string;
    dropoff_warehouse_name?: string | null;
  }>;
};

type LogisticsMonitorItem = {
  transfer_id: number;
  source_document_type: string;
  document_number: string;
  lookup_code?: string | null;
  status: string;
  current_warehouse_name?: string | null;
  dropoff_warehouse_name?: string | null;
  driver_name?: string | null;
  route_name?: string | null;
  manual_review_count: number;
};

async function logisticsFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/logistics/web${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch {
      // keep status message
    }
    throw new Error(message);
  }
  return response.json();
}

let fallbackExchangePromise: Promise<void> | null = null;

function exchangeFallbackLaunchToken() {
  const token = new URLSearchParams(window.location.search).get("launch");
  if (!token) return Promise.resolve();
  if (!fallbackExchangePromise) {
    fallbackExchangePromise = fetch("/api/bitrix/logistics/fallback-session", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }).then(async (response) => {
      if (!response.ok) throw new Error("Одноразовая ссылка истекла или уже использована");
      const url = new URL(window.location.href);
      url.searchParams.delete("launch");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    });
  }
  return fallbackExchangePromise;
}

function LogisticsFallbackApp() {
  const [profile, setProfile] = useState<LogisticsProfile | null>(null);
  const [warehouses, setWarehouses] = useState<LogisticsWarehouse[]>([]);
  const [drivers, setDrivers] = useState<LogisticsDriver[]>([]);
  const [mode, setMode] = useState<"handoff" | "receipt">("receipt");
  const [warehouseId, setWarehouseId] = useState("");
  const [driverId, setDriverId] = useState("");
  const [dropoffWarehouseId, setDropoffWarehouseId] = useState("");
  const [scanCode, setScanCode] = useState("");
  const [comment, setComment] = useState("");
  const [draft, setDraft] = useState<LogisticsDraft | null>(null);
  const [monitor, setMonitor] = useState<LogisticsMonitorItem[]>([]);
  const [message, setMessage] = useState("Загрузка...");

  const refreshMonitorForWarehouse = useCallback(async (effectiveWarehouseId: string) => {
    const params = new URLSearchParams();
    if (effectiveWarehouseId) params.set("warehouse_id", effectiveWarehouseId);
    const data = await logisticsFetch<LogisticsMonitorItem[]>(`/monitor?${params.toString()}`);
    setMonitor(data);
  }, []);

  const refreshMonitor = useCallback(
    () => refreshMonitorForWarehouse(warehouseId),
    [refreshMonitorForWarehouse, warehouseId]
  );

  useEffect(() => {
    let cancelled = false;
    exchangeFallbackLaunchToken()
      .then(() =>
        Promise.all([
          logisticsFetch<LogisticsProfile>("/profile"),
          logisticsFetch<LogisticsWarehouse[]>("/warehouses"),
          logisticsFetch<LogisticsDriver[]>("/drivers"),
        ])
      )
      .then(([profileData, warehouseData, driverData]) => {
        if (cancelled) return;
        setProfile(profileData);
        setWarehouses(warehouseData);
        setDrivers(driverData);
        const defaultWarehouse = String(profileData.default_warehouse_id || warehouseData[0]?.id || "");
        setWarehouseId(defaultWarehouse);
        setDropoffWarehouseId(defaultWarehouse);
        setDriverId(String(driverData[0]?.id || ""));
        setMessage("");
        return refreshMonitorForWarehouse(defaultWarehouse);
      })
      .catch((error: unknown) => {
        if (!cancelled) setMessage(error instanceof Error ? error.message : "Нет web-сессии");
      });
    return () => {
      cancelled = true;
    };
  }, [refreshMonitorForWarehouse]);

  const createDraft = async () => {
    try {
      const path = mode === "handoff" ? "/handoffs/draft" : "/receipts/draft";
      const payload =
        mode === "handoff"
          ? {
              warehouse_id: Number(warehouseId),
              driver_id: driverId ? Number(driverId) : null,
              default_dropoff_warehouse_id: dropoffWarehouseId ? Number(dropoffWarehouseId) : null,
              comment,
            }
          : { warehouse_id: Number(warehouseId), comment };
      const data = await logisticsFetch<LogisticsDraft>(path, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setDraft(data);
      setMessage(`Черновик #${data.id}`);
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Ошибка черновика");
    }
  };

  const scanDraft = async () => {
    if (!draft || !scanCode.trim()) return;
    try {
      const base = mode === "handoff" ? "/handoffs" : "/receipts";
      const data = await logisticsFetch<LogisticsDraft>(`${base}/draft/${draft.id}/scan`, {
        method: "POST",
        body: JSON.stringify({
          lookup_code: scanCode.trim(),
          dropoff_warehouse_id: mode === "handoff" && dropoffWarehouseId ? Number(dropoffWarehouseId) : null,
        }),
      });
      setDraft(data);
      setScanCode("");
      setMessage("Скан принят");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Ошибка скана");
    }
  };

  const confirmDraft = async () => {
    if (!draft) return;
    try {
      const base = mode === "handoff" ? "/handoffs" : "/receipts";
      const data = await logisticsFetch<{ processed_count: number }>(`${base}/draft/${draft.id}/confirm`, {
        method: "POST",
        body: JSON.stringify({ comment }),
      });
      setDraft(null);
      setMessage(`Подтверждено: ${data.processed_count}`);
      await refreshMonitor();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Ошибка подтверждения");
    }
  };

  return (
    <div className="app logistics">
      <header className="app__header">
        <h1>Логистика</h1>
        {profile && <span className="app__user">{profile.full_name}</span>}
        <button className="btn btn--ghost" onClick={() => refreshMonitor()}>
          Обновить
        </button>
      </header>
      <main className="logistics__grid">
        <section className="logistics__panel">
          <div className="logistics__tabs">
            <button className={mode === "receipt" ? "btn" : "btn btn--ghost"} onClick={() => setMode("receipt")}>
              Приемка
            </button>
            <button className={mode === "handoff" ? "btn" : "btn btn--ghost"} onClick={() => setMode("handoff")}>
              Передача
            </button>
          </div>
          <select className="app__select" value={warehouseId} onChange={(e) => setWarehouseId(e.target.value)}>
            {warehouses.map((warehouse) => (
              <option key={warehouse.id} value={warehouse.id}>
                {warehouse.name}
              </option>
            ))}
          </select>
          {mode === "handoff" && (
            <>
              <select className="app__select" value={driverId} onChange={(e) => setDriverId(e.target.value)}>
                {drivers.map((driver) => (
                  <option key={driver.id} value={driver.id}>
                    {driver.full_name}
                  </option>
                ))}
              </select>
              <select
                className="app__select"
                value={dropoffWarehouseId}
                onChange={(e) => setDropoffWarehouseId(e.target.value)}
              >
                {warehouses.map((warehouse) => (
                  <option key={warehouse.id} value={warehouse.id}>
                    {warehouse.name}
                  </option>
                ))}
              </select>
            </>
          )}
          <input className="app__search" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Комментарий" />
          <button className="btn" onClick={createDraft}>
            Открыть
          </button>
          {draft && (
            <div className="logistics__draft">
              <strong>#{draft.id}</strong>
              <input
                className="app__search"
                value={scanCode}
                onChange={(e) => setScanCode(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") scanDraft();
                }}
                placeholder="QR или штрихкод"
              />
              <div className="logistics__actions">
                <button className="btn" onClick={scanDraft}>
                  Скан
                </button>
                <button className="btn btn--ghost" onClick={confirmDraft}>
                  Подтвердить
                </button>
              </div>
              <ul>
                {draft.items.map((item) => (
                  <li key={item.id}>
                    {item.document_number} · {item.lookup_code || item.barcode}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {message && <p className="logistics__message">{message}</p>}
        </section>
        <section className="logistics__panel logistics__monitor">
          <table>
            <thead>
              <tr>
                <th>Документ</th>
                <th>Статус</th>
                <th>Куда</th>
                <th>Рейс</th>
              </tr>
            </thead>
            <tbody>
              {monitor.map((item) => (
                <tr key={item.transfer_id}>
                  <td>{item.document_number}</td>
                  <td>{item.status}</td>
                  <td>{item.dropoff_warehouse_name || item.current_warehouse_name || ""}</td>
                  <td>{item.route_name || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </main>
    </div>
  );
}

function BitrixLogisticsApp() {
  const [authState, setAuthState] = useState<
    { status: "loading" } | { status: "ready" } | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    const previousTitle = document.title;
    document.title = "Логистика — Bitrix24";
    return () => {
      document.title = previousTitle;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    initializeBitrixLogisticsSession()
      .then(() => {
        if (!cancelled) setAuthState({ status: "ready" });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setAuthState({
            status: "error",
            message: error instanceof Error ? error.message : "Нет доступа к логистике",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (authState.status !== "ready") {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Логистика</h1>
          <p>{authState.status === "loading" ? "Подключение к Bitrix24…" : authState.message}</p>
          {authState.status === "error" && <small>Проверьте привязку пользователя к роли и складу.</small>}
        </div>
      </div>
    );
  }
  return <LogisticsWorkspace />;
}

function MatchingApp() {
  const bitrixMode = isBitrixMatchingRoute();
  const savedProductPrefs = useMemo(() => readProductListPrefs(), []);
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    userName?: string | null;
  }>(() => ({ status: bitrixMode ? "loading" : "ready" }));
  const [search, setSearch] = useState(savedProductPrefs.search);
  const [debouncedSearch, setDebouncedSearch] = useState(savedProductPrefs.search);
  const [status, setStatus] = useState<string>(savedProductPrefs.status);
  const [category, setCategory] = useState<string>(savedProductPrefs.category);
  const [compatibilityBrand, setCompatibilityBrand] = useState<string>(savedProductPrefs.compatibilityBrand);
  const [subject, setSubject] = useState<string>(savedProductPrefs.subject);
  const [productSort, setProductSort] = useState<ProductSort>(savedProductPrefs.productSort);
  const [productFacets, setProductFacets] = useState<ProductFacets | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(savedProductPrefs.pageSize);
  const [total, setTotal] = useState(0);
  const [productPage, setProductPage] = useState<{ page: number; items: ProductRow[] }>({ page: 0, items: [] });
  const pendingOpenPageRef = useRef<number | null>(null);
  const [isPropertySettingsOpen, setIsPropertySettingsOpen] = useState(false);
  const [isCompatibilitySettingsOpen, setIsCompatibilitySettingsOpen] = useState(false);
  const { selectedProductId, openPicker, closePicker } = useSelectedProduct();

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    writeProductListPrefs({
      search,
      status,
      category,
      compatibilityBrand,
      subject,
      productSort,
      pageSize,
    });
  }, [category, compatibilityBrand, pageSize, productSort, search, status, subject]);

  useEffect(() => {
    if (!bitrixMode) return;
    let cancelled = false;
    initializeBitrixMatchingSession()
      .then((user) => {
        if (!cancelled) {
          setAuthState({ status: "ready", userName: user.name });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) {
          setAuthState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bitrixMode]);

  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / pageSize)), [total, pageSize]);
  const handleProductRowsChange = useCallback(
    (items: ProductRow[], loadedPage: number) => {
      setProductPage({ page: loadedPage, items });
      if (pendingOpenPageRef.current !== loadedPage) return;
      pendingOpenPageRef.current = null;
      if (items.length) {
        openPicker(items[0]);
      } else {
        closePicker();
        toast("Очередь по текущим фильтрам закончилась");
      }
    },
    [closePicker, openPicker]
  );

  const openNextProduct = useCallback(() => {
    const rows = productPage.page === page ? productPage.items : [];
    const currentIndex = rows.findIndex((product) => product.id === selectedProductId);
    if (currentIndex === -1 && rows.length) {
      openPicker(rows[0]);
      return;
    }
    if (currentIndex >= 0 && currentIndex < rows.length - 1) {
      openPicker(rows[currentIndex + 1]);
      return;
    }
    if (page < totalPages) {
      const nextPage = page + 1;
      pendingOpenPageRef.current = nextPage;
      setPage(nextPage);
      return;
    }
    closePicker();
    toast("Очередь по текущим фильтрам закончилась");
  }, [closePicker, openPicker, page, productPage, selectedProductId, totalPages]);

  if (authState.status !== "ready") {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Сопоставление товаров</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>Нет доступа к интерфейсу сопоставления.</p>
              <small>{authState.message}</small>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>Сопоставление товаров</h1>
        <input
          className="app__search"
          placeholder="Поиск по названию или SKU"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(1);
          }}
        />
        <select
          className="app__select"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setPage(1);
          }}
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <select
          className="app__select"
          value={productSort}
          onChange={(e) => {
            setProductSort(e.target.value as ProductSort);
            setPage(1);
          }}
        >
          {PRODUCT_SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <select
          className="app__select"
          value={category}
          onChange={(e) => {
            setCategory(e.target.value);
            setPage(1);
          }}
        >
          <option value="">Все категории</option>
          {productFacets?.categories.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label} ({item.count})
            </option>
          ))}
        </select>
        <select
          className="app__select"
          value={subject}
          onChange={(e) => {
            setSubject(e.target.value);
            setPage(1);
          }}
        >
          <option value="">Все предметы</option>
          {productFacets?.subjects.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label} ({item.count})
            </option>
          ))}
        </select>
        <select
          className="app__select"
          value={compatibilityBrand}
          onChange={(e) => {
            setCompatibilityBrand(e.target.value);
            setPage(1);
          }}
        >
          <option value="">Все бренды совместимости</option>
          {productFacets?.compatibility_brands?.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label} ({item.count})
            </option>
          ))}
        </select>
        <button className="btn btn--ghost" onClick={() => setIsPropertySettingsOpen(true)}>
          Настройки свойств
        </button>
        <button className="btn btn--ghost" onClick={() => setIsCompatibilitySettingsOpen(true)}>
          Совместимость
        </button>
        {bitrixMode && authState.userName && <span className="app__user">{authState.userName}</span>}
      </header>
      <MatchingLayout
        search={debouncedSearch}
        status={status}
        category={category}
        compatibilityBrand={compatibilityBrand}
        subject={subject}
        sort={productSort}
        page={page}
        pageSize={pageSize}
        onTotalChange={setTotal}
        onFacetsChange={setProductFacets}
        onProductRowsChange={handleProductRowsChange}
        onNextProduct={openNextProduct}
      />
      <div className="app__pagination">
        <div>
          Стр. {page} / {totalPages} (всего {total})
        </div>
        <div className="app__pagination-actions">
          <button className="btn btn--ghost" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}>
            Назад
          </button>
          <button
            className="btn"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
          >
            Вперед
          </button>
          <select
            className="app__select"
            value={pageSize}
            onChange={(e) => {
              const nextPageSize = Number(e.target.value);
              setPageSize(PRODUCT_PAGE_SIZES.includes(nextPageSize) ? nextPageSize : 50);
              setPage(1);
            }}
          >
            {PRODUCT_PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n} на страницу
              </option>
            ))}
          </select>
        </div>
      </div>
      <PropertyMappingSettings
        open={isPropertySettingsOpen}
        onClose={() => setIsPropertySettingsOpen(false)}
        onOpenCompatibility={() => {
          setIsPropertySettingsOpen(false);
          setIsCompatibilitySettingsOpen(true);
        }}
      />
      <CompatibilityMappingSettings
        open={isCompatibilitySettingsOpen}
        onClose={() => setIsCompatibilitySettingsOpen(false)}
      />
    </div>
  );
}

function ReceivablesApp() {
  const bitrixMode = isBitrixReceivablesRoute();
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    session?: BitrixReceivablesSessionResponse;
  }>(() => ({ status: bitrixMode ? "loading" : "ready" }));

  useEffect(() => {
    if (!bitrixMode) return;
    let cancelled = false;
    initializeBitrixReceivablesSession()
      .then((session) => {
        if (!cancelled) {
          setAuthState({ status: "ready", session });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) {
          setAuthState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bitrixMode]);

  if (authState.status !== "ready") {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Дебиторка покупателей</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>Нет доступа к рабочему месту дебиторки.</p>
              <small>{authState.message}</small>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <ReceivablesWorkplace
      accessLevel={authState.session?.access_level}
      bitrixMode={bitrixMode}
      bitrixUserName={authState.session?.user.name}
      departmentRefs={authState.session?.department_refs}
    />
  );
}

function ExecutiveDashboardApp() {
  const bitrixMode = isBitrixExecutiveDashboardRoute();
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    session?: BitrixExecutiveDashboardSessionResponse;
  }>(() => ({ status: bitrixMode ? "loading" : "ready" }));

  useEffect(() => {
    if (!bitrixMode) return;
    let cancelled = false;
    initializeBitrixExecutiveDashboardSession()
      .then((session) => {
        if (!cancelled) {
          setAuthState({ status: "ready", session });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) {
          setAuthState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bitrixMode]);

  if (authState.status !== "ready") {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Управленческая витрина</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>Нет доступа к управленческой витрине.</p>
              <small>{authState.message}</small>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <ExecutiveDashboard
      accessLevel={authState.session?.access_level}
      bitrixMode={bitrixMode}
      bitrixUserName={authState.session?.user.name}
    />
  );
}

function CustomerPriceTypesApp() {
  const bitrixMode = isBitrixCustomerPriceTypesRoute();
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    session?: BitrixCustomerPriceTypesSessionResponse;
  }>(() => ({ status: bitrixMode ? "loading" : "ready" }));

  useEffect(() => {
    if (!bitrixMode) return;
    let cancelled = false;
    initializeBitrixCustomerPriceTypesSession()
      .then((session) => {
        if (!cancelled) {
          setAuthState({ status: "ready", session });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) {
          setAuthState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bitrixMode]);

  if (authState.status !== "ready") {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Управление типами цен</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>Нет доступа к витрине типов цен.</p>
              <small>Обновите страницу в Bitrix24. Если ошибка повторится, обратитесь к администратору.</small>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <CustomerPriceTypesWorkspace
      bitrixMode={bitrixMode}
      bitrixUserName={authState.session?.user.name}
      role={authState.session?.user.role}
      canViewMoney={authState.session?.user.can_view_money}
    />
  );
}

function ProcurementLabelsBitrixApp() {
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    userName?: string | null;
  }>({ status: "loading" });
  const [bindState, setBindState] = useState<{
    status: "idle" | "loading" | "done" | "error";
    message?: string;
  }>({ status: "idle" });
  const itemId = getProcurementLabelsItemId();

  useEffect(() => {
    let cancelled = false;
    initializeBitrixProcurementLabelsSession()
      .then((user) => {
        if (!cancelled) {
          setAuthState({ status: "ready", userName: user.name });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) {
          setAuthState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (authState.status !== "ready") {
    const openedOutsideBitrix =
      authState.status === "error" &&
      (authState.message?.includes("Bitrix24 SDK") || authState.message?.includes("OAuth"));

    return (
      <div className="app app--center">
        <div className="app-state app-state--wide">
          <h1>Этикетки ВЭД</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>
                {openedOutsideBitrix
                  ? "Эту страницу нужно открывать из карточки закупки в Bitrix24."
                  : "Нет доступа к генерации этикеток."}
              </p>
              {openedOutsideBitrix && (
                <div className="app-state__hint">
                  <span>Кнопка должна быть во вкладке карточки:</span>
                  <strong>Закупка/Заказ - Сформировать этикетки</strong>
                  <span>Прямая ссылка без Bitrix24 не передает ID карточки и сессию пользователя.</span>
                </div>
              )}
              <small>{authState.message}</small>
            </>
          )}
        </div>
      </div>
    );
  }

  if (!itemId) {
    const bindPlacement = async () => {
      setBindState({ status: "loading", message: "Закрепляем вкладку в карточке закупки..." });
      try {
        await bindBitrixProcurementLabelsPlacement();
        setBindState({
          status: "done",
          message: "Вкладка закреплена. Откройте карточку заказа заново и найдите ее рядом с Общие / Товары / Еще.",
        });
      } catch (error: unknown) {
        setBindState({
          status: "error",
          message: error instanceof Error ? error.message : "Bitrix24 не дал закрепить вкладку",
        });
      }
    };

    return (
      <div className="app app--center">
        <div className="app-state app-state--wide">
          <h1>Этикетки ВЭД</h1>
          <p>Приложение открыто из общего меню Bitrix24.</p>
          <div className="app-state__hint">
            <span>Для работы из заказа нужна вкладка в карточке:</span>
            <strong>Закупка/Заказ - Сформировать этикетки</strong>
            <span>После закрепления откройте карточку закупки, а не это общее меню приложений.</span>
          </div>
          <button
            className="btn"
            disabled={bindState.status === "loading"}
            onClick={bindPlacement}
            type="button"
          >
            {bindState.status === "loading" ? "Закрепляем..." : "Закрепить вкладку в карточке"}
          </button>
          {bindState.message && (
            <small className={bindState.status === "error" ? "app-state__error" : "app-state__note"}>
              {bindState.message}
            </small>
          )}
        </div>
      </div>
    );
  }

  return <ProcurementLabelsApp bitrixUserName={authState.userName} itemId={itemId} />;
}

function ProcurementAssortmentBitrixApp() {
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    userName?: string | null;
  }>({ status: "loading" });
  const itemId = getProcurementAssortmentItemId();

  useEffect(() => {
    let cancelled = false;
    initializeBitrixProcurementAssortmentSession()
      .then((user) => {
        if (!cancelled) {
          setAuthState({ status: "ready", userName: user.name });
        }
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) {
          setAuthState({ status: "error", message });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (authState.status !== "ready") {
    const openedOutsideBitrix =
      authState.status === "error" &&
      (authState.message?.includes("Bitrix24 SDK") || authState.message?.includes("OAuth"));

    return (
      <div className="app app--center">
        <div className="app-state app-state--wide">
          <h1>Формирование заказа</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>
                {openedOutsideBitrix
                  ? "Эту страницу нужно открывать из Bitrix24."
                  : "Нет доступа к формированию заказа."}
              </p>
              {openedOutsideBitrix && (
                <div className="app-state__hint">
                  <span>Рабочая вкладка в карточке заказа:</span>
                  <strong>Заказ и классификация</strong>
                  <span>Прямая ссылка без Bitrix24 не передает сессию пользователя.</span>
                </div>
              )}
              <small>{authState.message}</small>
            </>
          )}
        </div>
      </div>
    );
  }

  if (!itemId) {
    return (
      <div className="app app--center">
        <div className="app-state app-state--wide">
          <h1>Legacy-карточка заказа</h1>
          <p>Этот маршрут сохранён только для отката пилота.</p>
          <div className="app-state__hint">
            <span>Рабочий интерфейс находится в отдельном приложении Bitrix24:</span>
            <strong>Формирование заказа</strong>
          </div>
        </div>
      </div>
    );
  }

  return <ProcurementAssortmentWorkspace bitrixUserName={authState.userName} itemId={itemId} />;
}

function ProcurementOrderFormationBitrixApp() {
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    userName?: string | null;
  }>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    initializeBitrixProcurementOrderFormationSession()
      .then((user) => {
        if (!cancelled) setAuthState({ status: "ready", userName: user.name });
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Не удалось открыть Bitrix24-сессию";
        if (!cancelled) setAuthState({ status: "error", message });
      });
    return () => { cancelled = true; };
  }, []);

  if (authState.status !== "ready") {
    const openedOutsideBitrix =
      authState.status === "error" &&
      (authState.message?.includes("Bitrix24 SDK") || authState.message?.includes("OAuth"));
    return (
      <div className="app app--center">
        <div className="app-state app-state--wide">
          <h1>Формирование заказа</h1>
          {authState.status === "loading" && <p>Подключение к Bitrix24...</p>}
          {authState.status === "error" && (
            <>
              <p>{openedOutsideBitrix ? "Откройте приложение из меню Bitrix24." : "Нет доступа к формированию заказа."}</p>
              <small>{authState.message}</small>
            </>
          )}
        </div>
      </div>
    );
  }

  if (isBitrixProductInsightsPlacement()) {
    return <ProcurementProductInsights productId={getProcurementProductId()} />;
  }
  return <ProcurementOrderFormationWorkspace bitrixUserName={authState.userName} />;
}

function App() {
  if (isBitrixLogisticsRoute()) return <BitrixLogisticsApp />;
  if (isLogisticsFallbackRoute()) return <LogisticsFallbackApp />;
  if (isBitrixProcurementOrderFormationRoute()) return <ProcurementOrderFormationBitrixApp />;
  if (isBitrixProcurementAssortmentRoute()) {
    return new URLSearchParams(window.location.search).get("legacy") === "1"
      ? <ProcurementAssortmentBitrixApp />
      : <ProcurementOrderFormationBitrixApp />;
  }
  if (isBitrixProcurementLabelsRoute()) return <ProcurementLabelsBitrixApp />;
  if (isBitrixExecutiveDashboardRoute() || isExecutiveDashboardRoute()) return <ExecutiveDashboardApp />;
  if (isBitrixReceivablesRoute() || isReceivablesWorkplaceRoute()) return <ReceivablesApp />;
  if (isBitrixCustomerPriceTypesRoute()) return <CustomerPriceTypesApp />;
  return <MatchingApp />;
}

export default App;
