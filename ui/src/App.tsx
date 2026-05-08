import "./App.css";
import { useEffect, useMemo, useState } from "react";
import { CompatibilityMappingSettings } from "./components/CompatibilityMappingSettings";
import { MatchingLayout } from "./components/MatchingLayout";
import { PropertyMappingSettings } from "./components/PropertyMappingSettings";
import { initializeBitrixMatchingSession, isBitrixMatchingRoute } from "./api/bitrix";
import type { ProductFacets } from "./api/types";

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

function App() {
  const bitrixMode = isBitrixMatchingRoute();
  const [authState, setAuthState] = useState<{
    status: "ready" | "loading" | "error";
    message?: string;
    userName?: string | null;
  }>(() => ({ status: bitrixMode ? "loading" : "ready" }));
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [status, setStatus] = useState<string>("");
  const [category, setCategory] = useState<string>("");
  const [compatibilityBrand, setCompatibilityBrand] = useState<string>("");
  const [subject, setSubject] = useState<string>("");
  const [productFacets, setProductFacets] = useState<ProductFacets | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [isPropertySettingsOpen, setIsPropertySettingsOpen] = useState(false);
  const [isCompatibilitySettingsOpen, setIsCompatibilitySettingsOpen] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

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
        page={page}
        pageSize={pageSize}
        onTotalChange={setTotal}
        onFacetsChange={setProductFacets}
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
              setPageSize(Number(e.target.value));
              setPage(1);
            }}
          >
            {[25, 50, 100, 200].map((n) => (
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

export default App;
