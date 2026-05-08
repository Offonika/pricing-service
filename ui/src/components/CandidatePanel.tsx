import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  acceptItemMatch,
  fetchCandidates,
  fetchMatchHistory,
  fetchPropertyComparison,
  rejectItemMatch,
  revokeItemMatch,
} from "../api/matching";
import type { Candidate } from "../api/types";
import { useSelectedProduct } from "../store/useSelectionStore";

interface SearchState {
  productId: number | null;
  value: string;
}

type CompareTab = "summary" | "properties" | "history";

const STATUS_TEXT: Record<string, string> = {
  available: "Свободен",
  suggested: "Кандидат",
  current: "Текущая связь",
  accepted: "Принят",
  rejected: "Отклонен",
  needs_review: "На проверку",
  ambiguous: "Спорно",
  locked: "Занят",
};

const PRODUCT_STATUS_TEXT: Record<string, string> = {
  none: "Нет пары",
  candidates: "Есть кандидаты",
  auto: "Сопоставлен авто",
  manual: "Сопоставлен вручную",
  matched: "Сопоставлен",
  ambiguous: "Неоднозначно",
  uncertain: "На проверку",
  multiple: "Несколько связей",
};

const PROPERTY_STATUS_TEXT: Record<string, string> = {
  match: "совпадает",
  missing: "не хватает значения",
  conflict: "конфликт",
  unmapped: "правило не настроено",
};

const CANDIDATE_STATUS_OPTIONS = [
  { value: "", label: "Все состояния" },
  { value: "free", label: "Свободные и кандидаты" },
  { value: "current", label: "Текущая связь" },
  { value: "locked", label: "Заняты у других" },
  { value: "suggested", label: "Кандидаты" },
  { value: "needs_review", label: "На проверку" },
  { value: "ambiguous", label: "Спорные" },
  { value: "rejected", label: "Отклоненные" },
];

function numberFilter(value: string) {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed.replace(",", "."));
  return Number.isFinite(parsed) ? parsed : undefined;
}

function valueText(value: string | number | boolean | null | undefined) {
  if (typeof value === "boolean") return value ? "да" : "нет";
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

interface MatchingApiError {
  detail?: string | { error?: string; reason?: string };
}

function matchingErrorMessage(error: unknown, fallback: string) {
  const axiosError = error as AxiosError<MatchingApiError>;
  const detail = axiosError.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (!detail?.error) return fallback;
  if (detail.error === "compatibility_required") {
    return "Не принято: у нового товара конкурента не указана совместимость с моделью";
  }
  if (detail.error === "candidate_guardrail_blocked") {
    return `Не принято: конфликт проверки${detail.reason ? ` (${detail.reason})` : ""}`;
  }
  if (detail.error === "already_accepted") {
    return "Не принято: этот товар конкурента уже принят для другого товара";
  }
  return detail.reason || fallback;
}

export function CandidatePanel() {
  const queryClient = useQueryClient();
  const { selectedProduct, selectedProductId, selectedProductName, selectedProductArticle, isPickerOpen, closePicker } =
    useSelectedProduct();
  const [searchState, setSearchState] = useState<SearchState>({ productId: null, value: "" });
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedCandidateId, setSelectedCandidateId] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [onlyInStock, setOnlyInStock] = useState(false);
  const [includeRejected, setIncludeRejected] = useState(false);
  const [source, setSource] = useState("");
  const [itemType, setItemType] = useState("");
  const [categoryGroup, setCategoryGroup] = useState("");
  const [brand, setBrand] = useState("");
  const [quality, setQuality] = useState("");
  const [color, setColor] = useState("");
  const [model, setModel] = useState("");
  const [priceMin, setPriceMin] = useState("");
  const [priceMax, setPriceMax] = useState("");
  const [candidateStatus, setCandidateStatus] = useState("");
  const [isWideList, setIsWideList] = useState(false);
  const [compareTab, setCompareTab] = useState<CompareTab>("summary");
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pageSize = 25;

  const search = searchState.productId === selectedProductId ? searchState.value : "";

  useEffect(() => {
    if (searchDebounceRef.current) {
      clearTimeout(searchDebounceRef.current);
    }
    searchDebounceRef.current = setTimeout(() => {
      setDebouncedSearch(search);
      setPage(1);
    }, 250);
    return () => {
      if (searchDebounceRef.current) {
        clearTimeout(searchDebounceRef.current);
        searchDebounceRef.current = null;
      }
    };
  }, [search]);

  const { data, isError, isLoading } = useQuery({
    queryKey: [
      "candidate-search",
      selectedProductId,
      page,
      pageSize,
      debouncedSearch,
      onlyInStock,
      includeRejected,
      source,
      itemType,
      categoryGroup,
      brand,
      quality,
      color,
      model,
      priceMin,
      priceMax,
      candidateStatus,
      "property-summary",
    ],
    queryFn: () =>
      fetchCandidates(selectedProductId!, {
        offset: (page - 1) * pageSize,
        limit: pageSize,
        q: debouncedSearch || undefined,
        in_stock: onlyInStock || undefined,
        include_rejected: includeRejected || candidateStatus === "rejected" || undefined,
        source: source || undefined,
        item_type: itemType || undefined,
        category_group: categoryGroup || undefined,
        brand: brand || undefined,
        model: model || undefined,
        quality: quality || undefined,
        color: color || undefined,
        candidate_status: candidateStatus || undefined,
        price_min: numberFilter(priceMin),
        price_max: numberFilter(priceMax),
        include_property_summary: true,
      }),
    enabled: isPickerOpen && !!selectedProductId,
  });

  const { data: historyData } = useQuery({
    queryKey: ["match-history", selectedProductId],
    queryFn: () => fetchMatchHistory(selectedProductId!),
    enabled: isPickerOpen && !!selectedProductId,
  });

  const items = useMemo(() => data?.items ?? [], [data?.items]);
  const selectedCandidate = useMemo(
    () => items.find((candidate) => candidate.competitor_item_id === selectedCandidateId) || items[0],
    [items, selectedCandidateId]
  );
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const canAcceptSelected = Boolean(
    selectedCandidate?.competitor_item_id && selectedCandidate.status !== "locked" && !selectedCandidate.needs_compat_review
  );
  const selectedCandidateItemId = selectedCandidate?.competitor_item_id;
  const {
    data: propertyData,
    isError: isPropertyError,
    isLoading: isPropertyLoading,
  } = useQuery({
    queryKey: ["property-comparison", selectedProductId, selectedCandidateItemId],
    queryFn: () => fetchPropertyComparison(selectedProductId!, selectedCandidateItemId!),
    enabled:
      isPickerOpen &&
      compareTab === "properties" &&
      Boolean(selectedProductId && selectedCandidateItemId),
  });
  const hasSearch = Boolean(search.trim() || debouncedSearch.trim());
  const hasActiveFilters = Boolean(
    hasSearch ||
      onlyInStock ||
      includeRejected ||
      source ||
      itemType ||
      categoryGroup ||
      brand ||
      quality ||
      color ||
      model ||
      priceMin ||
      priceMax ||
      candidateStatus
  );

  const invalidateMatching = () => {
    queryClient.invalidateQueries({ queryKey: ["products"] });
    queryClient.invalidateQueries({ queryKey: ["candidate-search", selectedProductId] });
    queryClient.invalidateQueries({ queryKey: ["match-history", selectedProductId] });
    queryClient.invalidateQueries({ queryKey: ["property-comparison", selectedProductId] });
  };

  const acceptMutation = useMutation({
    mutationFn: (candidate: Candidate) => acceptItemMatch(selectedProductId!, candidate.competitor_item_id!),
    onSuccess: () => {
      invalidateMatching();
      toast.success("Сопоставление принято");
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось принять сопоставление")),
  });

  const rejectMutation = useMutation({
    mutationFn: (candidate: Candidate) => rejectItemMatch(selectedProductId!, candidate.competitor_item_id!),
    onSuccess: () => {
      invalidateMatching();
      toast.success("Кандидат отклонен");
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось отклонить кандидата")),
  });

  const revokeMutation = useMutation({
    mutationFn: (candidate: Candidate) => revokeItemMatch(selectedProductId!, candidate.competitor_item_id!),
    onSuccess: () => {
      invalidateMatching();
      toast.success("Сопоставление снято");
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось снять сопоставление")),
  });

  useEffect(() => {
    if (!isPickerOpen) return;
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const isTyping = target?.tagName === "INPUT" || target?.tagName === "SELECT" || target?.tagName === "TEXTAREA";
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        document.getElementById("candidate-search")?.focus();
        return;
      }
      if (isTyping || !items.length) return;
      const currentIndex = Math.max(
        0,
        items.findIndex((candidate) => candidate.competitor_item_id === selectedCandidate?.competitor_item_id)
      );
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedCandidateId(items[Math.min(items.length - 1, currentIndex + 1)]?.competitor_item_id ?? null);
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedCandidateId(items[Math.max(0, currentIndex - 1)]?.competitor_item_id ?? null);
      }
      if (e.key === "Enter" && canAcceptSelected) {
        e.preventDefault();
        acceptMutation.mutate(selectedCandidate);
      }
      if (e.key === "Escape") {
        e.preventDefault();
        closePicker();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [acceptMutation, canAcceptSelected, closePicker, isPickerOpen, items, selectedCandidate]);

  if (!isPickerOpen || !selectedProductId) return null;

  const updateSearch = (value: string) => {
    setSearchState({ productId: selectedProductId, value });
  };

  const resetFilterPage = () => setPage(1);

  const clearFilters = () => {
    if (searchDebounceRef.current) {
      clearTimeout(searchDebounceRef.current);
      searchDebounceRef.current = null;
    }
    setSearchState({ productId: selectedProductId, value: "" });
    setDebouncedSearch("");
    setOnlyInStock(false);
    setIncludeRejected(false);
    setSource("");
    setItemType("");
    setCategoryGroup("");
    setBrand("");
    setQuality("");
    setColor("");
    setModel("");
    setPriceMin("");
    setPriceMax("");
    setCandidateStatus("");
    setSelectedCandidateId(null);
    setPage(1);
  };

  const acceptSelected = () => {
    if (!canAcceptSelected || !selectedCandidate) return;
    acceptMutation.mutate(selectedCandidate);
  };

  const rejectSelected = () => {
    if (!selectedCandidate?.competitor_item_id) return;
    rejectMutation.mutate(selectedCandidate);
  };

  const revokeSelected = () => {
    if (!selectedCandidate?.competitor_item_id) return;
    if (window.confirm("Снять принятое сопоставление?")) {
      revokeMutation.mutate(selectedCandidate);
    }
  };

  return (
    <div className={`picker ${isWideList ? "picker--wide-list" : ""}`} role="dialog" aria-modal="true">
      <header className="picker__topbar">
        <div>
          <div className="picker__eyebrow">Подбор товара конкурента</div>
          <h2>{selectedProductName}</h2>
        </div>
        <div className="picker__topbar-actions">
          <button className="btn btn--ghost" onClick={() => setIsWideList((value) => !value)} aria-pressed={isWideList}>
            {isWideList ? "Показать сравнение" : "Шире список"}
          </button>
          <button className="btn btn--ghost" onClick={closePicker}>
            Закрыть
          </button>
        </div>
      </header>

      <aside className="picker__product">
        <div className="picker__section-title">Наш товар</div>
        <h3>{selectedProductName}</h3>
        <div className="kv">
          <span>Артикул</span>
          <strong>{selectedProductArticle || "-"}</strong>
        </div>
        <div className="kv">
          <span>Бренд</span>
          <strong>{selectedProduct?.brand || "-"}</strong>
        </div>
        <div className="kv">
          <span>Категория</span>
          <strong>{selectedProduct?.category || "-"}</strong>
        </div>
        <div className="kv">
          <span>Предмет</span>
          <strong>{selectedProduct?.subject || "-"}</strong>
        </div>
        <div className="kv">
          <span>Статус</span>
          <strong>
            {selectedProduct?.status ? PRODUCT_STATUS_TEXT[selectedProduct.status] || selectedProduct.status : "-"}
          </strong>
        </div>
      </aside>

      <main className="picker__results">
        <div className="picker__searchbar">
          <input
            id="candidate-search"
            className="app__search app__search--wide"
            placeholder="Поиск по названию, SKU, конкуренту, модели"
            value={search}
            onChange={(e) => updateSearch(e.target.value)}
            autoFocus
          />
          <label className="checkbox">
            <input
              type="checkbox"
              checked={onlyInStock}
              onChange={(e) => {
                setOnlyInStock(e.target.checked);
                resetFilterPage();
              }}
            />
            В наличии
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={includeRejected}
              onChange={(e) => {
                setIncludeRejected(e.target.checked);
                resetFilterPage();
              }}
            />
            Отклоненные
          </label>
        </div>

        <div className="picker__filters picker__filters--dense">
          <select
            className="app__select"
            value={candidateStatus}
            onChange={(e) => {
              setCandidateStatus(e.target.value);
              resetFilterPage();
            }}
          >
            {CANDIDATE_STATUS_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <select
            className="app__select"
            value={source}
            onChange={(e) => {
              setSource(e.target.value);
              resetFilterPage();
            }}
          >
            <option value="">Все конкуренты</option>
            {data?.facets?.sources.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <select
            className="app__select"
            value={itemType}
            onChange={(e) => {
              setItemType(e.target.value);
              resetFilterPage();
            }}
          >
            <option value="">Все типы</option>
            {data?.facets?.item_types.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <select
            className="app__select"
            value={categoryGroup}
            onChange={(e) => {
              setCategoryGroup(e.target.value);
              resetFilterPage();
            }}
          >
            <option value="">Все группы</option>
            {data?.facets?.category_groups.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <select
            className="app__select"
            value={brand}
            onChange={(e) => {
              setBrand(e.target.value);
              resetFilterPage();
            }}
          >
            <option value="">Все бренды</option>
            {data?.facets?.brands.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <select
            className="app__select"
            value={quality}
            onChange={(e) => {
              setQuality(e.target.value);
              resetFilterPage();
            }}
          >
            <option value="">Все качества</option>
            {data?.facets?.qualities.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <select
            className="app__select"
            value={color}
            onChange={(e) => {
              setColor(e.target.value);
              resetFilterPage();
            }}
          >
            <option value="">Все цвета</option>
            {data?.facets?.colors.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label} ({item.count})
              </option>
            ))}
          </select>
          <input
            className="app__search"
            placeholder="Модель"
            value={model}
            onChange={(e) => {
              setModel(e.target.value);
              resetFilterPage();
            }}
          />
          <input
            className="app__search"
            inputMode="decimal"
            placeholder="Цена от"
            value={priceMin}
            onChange={(e) => {
              setPriceMin(e.target.value);
              resetFilterPage();
            }}
          />
          <input
            className="app__search"
            inputMode="decimal"
            placeholder="Цена до"
            value={priceMax}
            onChange={(e) => {
              setPriceMax(e.target.value);
              resetFilterPage();
            }}
          />
          <button className="btn btn--ghost" onClick={clearFilters} disabled={!hasActiveFilters}>
            Сбросить
          </button>
        </div>

        <div className="candidate-table">
          <div className="candidate-table__head">
            <span>Конкурент</span>
            <span>Товар</span>
            <span>SKU</span>
            <span>Цена</span>
            <span className="candidate-table__status">Статус</span>
          </div>
          {isLoading && <div className="panel__loading">Загрузка...</div>}
          {isError && <div className="panel__empty">Не удалось загрузить кандидатов</div>}
          {!isLoading &&
            !isError &&
            items.map((candidate) => (
              <button
                key={candidate.competitor_item_id}
                className={`candidate-row ${
                  candidate.competitor_item_id === selectedCandidate?.competitor_item_id ? "candidate-row--selected" : ""
                }`}
                onClick={() => setSelectedCandidateId(candidate.competitor_item_id ?? null)}
              >
                <span>{candidate.competitor_name || "-"}</span>
                <strong>{candidate.name || "Без названия"}</strong>
                <span>{candidate.sku || "-"}</span>
                <span>{candidate.price ?? "-"}</span>
                <span className="candidate-row__status">
                  <span className={`badge badge--${candidate.status || "none"}`}>
                    {STATUS_TEXT[candidate.status || "available"] || candidate.status}
                  </span>
                  {candidate.needs_compat_review && (
                    <span className="badge badge--compat-review" title="Нет разобранной совместимости">
                      Совместимость
                    </span>
                  )}
                  {candidate.property_summary && (
                    <span
                      className={`badge badge--property-${candidate.property_summary.status || "unmapped"}`}
                      title={candidate.property_summary.conflicts.join(", ") || candidate.property_summary.label}
                    >
                      {candidate.property_summary.label}
                    </span>
                  )}
                </span>
              </button>
            ))}
          {!isLoading && !isError && !items.length && <div className="panel__empty">Ничего не найдено</div>}
        </div>

        <div className="app__pagination">
          <span>
            Стр. {page} / {totalPages} (всего {total})
          </span>
          <div className="app__pagination-actions">
            <button className="btn btn--ghost" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}>
              Назад
            </button>
            <button className="btn" onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page >= totalPages}>
              Вперед
            </button>
          </div>
        </div>
      </main>

      {!isWideList && (
        <aside className="picker__compare">
          <div className="picker__section-title">Сравнение</div>
          {selectedCandidate ? (
            <>
              <h3>{selectedCandidate.name || "Без названия"}</h3>
              <div className="compare-tabs" role="tablist" aria-label="Сравнение кандидата">
                <button
                  className={`compare-tabs__tab ${compareTab === "summary" ? "compare-tabs__tab--active" : ""}`}
                  onClick={() => setCompareTab("summary")}
                  role="tab"
                  aria-selected={compareTab === "summary"}
                >
                  Сводка
                </button>
                <button
                  className={`compare-tabs__tab ${compareTab === "properties" ? "compare-tabs__tab--active" : ""}`}
                  onClick={() => setCompareTab("properties")}
                  role="tab"
                  aria-selected={compareTab === "properties"}
                >
                  Свойства
                </button>
                <button
                  className={`compare-tabs__tab ${compareTab === "history" ? "compare-tabs__tab--active" : ""}`}
                  onClick={() => setCompareTab("history")}
                  role="tab"
                  aria-selected={compareTab === "history"}
                >
                  История
                </button>
              </div>

              {compareTab === "summary" && (
                <div className="compare-tab-panel">
                  <div className="kv">
                    <span>Конкурент</span>
                    <strong>{selectedCandidate.competitor_name || "-"}</strong>
                  </div>
                  <div className="kv">
                    <span>SKU</span>
                    <strong>{selectedCandidate.sku || "-"}</strong>
                  </div>
                  <div className="kv">
                    <span>Цена</span>
                    <strong>{selectedCandidate.price ?? "-"}</strong>
                  </div>
                  <div className="kv">
                    <span>Наличие</span>
                    <strong>{selectedCandidate.in_stock ? "да" : "нет"}</strong>
                  </div>
                  <div className="kv">
                    <span>Совпадение</span>
                    <strong>
                      {typeof selectedCandidate.score === "number"
                        ? `${Math.round(selectedCandidate.score * 100)}%`
                        : "-"}
                    </strong>
                  </div>
                  <div className="kv">
                    <span>Свойства</span>
                    <strong>{selectedCandidate.property_summary?.label || "-"}</strong>
                  </div>
                  {selectedCandidate.needs_compat_review && (
                    <p className="picker__note">Нужно разобрать совместимость перед принятием сопоставления.</p>
                  )}
                  {selectedCandidate.reason && <p className="picker__note">{selectedCandidate.reason}</p>}
                  {selectedCandidate.url && (
                    <a className="picker__link" href={selectedCandidate.url} target="_blank" rel="noreferrer">
                      Открыть у конкурента
                    </a>
                  )}
                  <div className="picker__actions">
                    <button
                      className="btn"
                      onClick={acceptSelected}
                      disabled={!canAcceptSelected || acceptMutation.isPending}
                    >
                      Принять
                    </button>
                    <button
                      className="btn btn--ghost"
                      onClick={rejectSelected}
                      disabled={
                        !selectedCandidate.competitor_item_id ||
                        selectedCandidate.status === "current" ||
                        rejectMutation.isPending
                      }
                    >
                      Отклонить
                    </button>
                    <button
                      className="btn btn--danger"
                      onClick={revokeSelected}
                      disabled={selectedCandidate.status !== "current" || revokeMutation.isPending}
                    >
                      Снять
                    </button>
                  </div>
                </div>
              )}

              {compareTab === "properties" && (
                <div className="compare-tab-panel">
                  {isPropertyLoading && <div className="panel__loading">Загрузка свойств...</div>}
                  {isPropertyError && <div className="panel__empty">Не удалось загрузить сравнение свойств</div>}
                  {!isPropertyLoading && !isPropertyError && propertyData && (
                    <>
                      <div className="property-summary">
                        <span>{propertyData.profile_name}</span>
                        <strong>{propertyData.summary.label}</strong>
                      </div>
                      <div className="property-table">
                        <div className="property-table__head">
                          <span>Характеристика</span>
                          <span>Наш товар</span>
                          <span>Конкурент</span>
                          <span>После мапинга</span>
                          <span>Статус</span>
                        </div>
                        {propertyData.items.map((item) => (
                          <div key={item.property_key} className={`property-row property-row--${item.status}`}>
                            <strong>{item.label}</strong>
                            <span>{valueText(item.product_value)}</span>
                            <span>{valueText(item.competitor_value)}</span>
                            <span>{valueText(item.mapped_value)}</span>
                            <span>
                              <span className={`badge badge--property-${item.status}`}>
                                {PROPERTY_STATUS_TEXT[item.status] || item.status}
                              </span>
                            </span>
                          </div>
                        ))}
                      </div>
                      {!propertyData.items.length && <div className="panel__empty">Правила профиля не настроены</div>}
                    </>
                  )}
                </div>
              )}

              {compareTab === "history" && (
                <div className="compare-tab-panel picker__history picker__history--plain">
                  {historyData?.items.slice(0, 8).map((item) => (
                    <div key={item.id} className="history-row">
                      <strong>{item.action}</strong>
                      <span>{item.competitor_name || item.sku || item.competitor_item_id}</span>
                      <small>{new Date(item.created_at).toLocaleString("ru-RU")}</small>
                    </div>
                  ))}
                  {!historyData?.items.length && <div className="panel__empty">Пока нет решений</div>}
                </div>
              )}
            </>
          ) : (
            <div className="panel__empty">Выберите кандидата</div>
          )}
        </aside>
      )}
    </div>
  );
}
