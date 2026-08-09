import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  acceptItemMatch,
  bulkRejectItemMatches,
  fetchCandidates,
  fetchMatchHistory,
  fetchPropertyComparison,
  rejectItemMatch,
  revokeItemMatch,
} from "../api/matching";
import type { Candidate, MatchingDecisionReasonCode } from "../api/types";
import { useSelectedProduct } from "../store/useSelectionStore";
import { hasRequiredDecisionReason } from "./matchingDecisionReason";

interface SearchState {
  productId: number | null;
  value: string;
}

interface SelectedCandidateState {
  productId: number | null;
  value: number | null;
}

interface CandidatePageState {
  productId: number | null;
  value: number;
}

interface BulkSelectionState {
  scope: string;
  value: Set<number>;
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

const REJECT_REASON_OPTIONS: Array<{ value: MatchingDecisionReasonCode; label: string }> = [
  { value: "wrong_model", label: "Неверная модель" },
  { value: "wrong_item_type", label: "Другой тип товара" },
  { value: "wrong_quality", label: "Другое качество" },
  { value: "wrong_color", label: "Другой цвет" },
  { value: "wrong_frame", label: "Другая рамка/комплектация" },
  { value: "wrong_part_number", label: "Другой партномер" },
  { value: "wrong_capacity", label: "Другая ёмкость" },
  { value: "duplicate_or_irrelevant", label: "Дубль или нерелевантно" },
  { value: "auto_false_positive", label: "Ошибка автоматического сопоставления" },
  { value: "other", label: "Другая причина" },
];

const CANDIDATE_FILTER_PREFS_KEY = "pricing.matching.candidate-filters.v1";

interface CandidateFilterPrefs {
  onlyInStock: boolean;
  includeRejected: boolean;
  source: string;
  itemType: string;
  categoryGroup: string;
  brand: string;
  quality: string;
  color: string;
  candidateStatus: string;
  isWideList: boolean;
}

const DEFAULT_CANDIDATE_FILTER_PREFS: CandidateFilterPrefs = {
  onlyInStock: false,
  includeRejected: false,
  source: "",
  itemType: "",
  categoryGroup: "",
  brand: "",
  quality: "",
  color: "",
  candidateStatus: "",
  isWideList: false,
};

function stringPref(data: Record<string, unknown>, key: string) {
  const value = data[key];
  return typeof value === "string" ? value : "";
}

function booleanPref(data: Record<string, unknown>, key: string) {
  return data[key] === true;
}

function readCandidateFilterPrefs(): CandidateFilterPrefs {
  if (typeof window === "undefined") return DEFAULT_CANDIDATE_FILTER_PREFS;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(CANDIDATE_FILTER_PREFS_KEY) || "{}") as Record<string, unknown>;
    return {
      onlyInStock: booleanPref(parsed, "onlyInStock"),
      includeRejected: booleanPref(parsed, "includeRejected"),
      source: stringPref(parsed, "source"),
      itemType: stringPref(parsed, "itemType"),
      categoryGroup: stringPref(parsed, "categoryGroup"),
      brand: stringPref(parsed, "brand"),
      quality: stringPref(parsed, "quality"),
      color: stringPref(parsed, "color"),
      candidateStatus: stringPref(parsed, "candidateStatus"),
      isWideList: booleanPref(parsed, "isWideList"),
    };
  } catch {
    return DEFAULT_CANDIDATE_FILTER_PREFS;
  }
}

function writeCandidateFilterPrefs(prefs: CandidateFilterPrefs) {
  try {
    window.localStorage.setItem(CANDIDATE_FILTER_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage может быть недоступен; фильтры просто не будут запоминаться.
  }
}

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

function compatibilityHint(candidate: Candidate | undefined) {
  return candidate?.compatibility_hint;
}

function shouldShowCompatibilityBadge(candidate: Candidate) {
  const hint = compatibilityHint(candidate);
  return Boolean(hint && hint.status !== "not_required");
}

function compatibilityTitle(candidate: Candidate) {
  const hint = compatibilityHint(candidate);
  if (!hint) return "";
  const values = hint.matched_values?.length ? `: ${hint.matched_values.join(", ")}` : "";
  return `${hint.detail || hint.label}${values}`;
}

function compatibilitySummary(candidate: Candidate | undefined) {
  const hint = compatibilityHint(candidate);
  if (!hint || hint.status === "not_required") return "Не требуется";
  const values = hint.matched_values?.length ? `: ${hint.matched_values.join(", ")}` : "";
  return `${hint.label}${values}`;
}

function isAutoCompatibilityHint(candidate: Candidate | undefined) {
  const status = compatibilityHint(candidate)?.status;
  return status === "inferred_model" || status === "inferred_code";
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
    return "Не принято: не нашли общую модель или код. Отклоните этот вариант или передайте его на разбор совместимости.";
  }
  if (detail.error === "candidate_guardrail_blocked") {
    return `Не принято: конфликт проверки${detail.reason ? ` (${detail.reason})` : ""}`;
  }
  if (detail.error === "already_accepted") {
    return "Не принято: этот товар конкурента уже принят для другого товара";
  }
  return detail.reason || fallback;
}

function canAcceptCandidate(candidate: Candidate | undefined) {
  if (!candidate?.competitor_item_id) return false;
  return !["accepted", "current", "locked", "rejected"].includes(candidate.status || "");
}

const BULK_REJECTABLE_STATUSES = new Set(["available", "suggested", "needs_review", "ambiguous"]);

function canBulkRejectCandidate(candidate: Candidate | undefined) {
  if (!candidate?.competitor_item_id) return false;
  return BULK_REJECTABLE_STATUSES.has(candidate.status || "available");
}

interface CandidatePanelProps {
  onNextProduct?: () => void;
  onAfterDecision?: () => void;
}

export function CandidatePanel({ onNextProduct, onAfterDecision }: CandidatePanelProps) {
  const queryClient = useQueryClient();
  const { selectedProduct, selectedProductId, selectedProductName, selectedProductArticle, isPickerOpen, closePicker } =
    useSelectedProduct();
  const savedCandidatePrefs = useMemo(() => readCandidateFilterPrefs(), []);
  const [searchState, setSearchState] = useState<SearchState>({ productId: null, value: "" });
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedCandidateState, setSelectedCandidateState] = useState<SelectedCandidateState>({
    productId: null,
    value: null,
  });
  const [pageState, setPageState] = useState<CandidatePageState>({ productId: null, value: 1 });
  const [onlyInStock, setOnlyInStock] = useState(savedCandidatePrefs.onlyInStock);
  const [includeRejected, setIncludeRejected] = useState(savedCandidatePrefs.includeRejected);
  const [source, setSource] = useState(savedCandidatePrefs.source);
  const [itemType, setItemType] = useState(savedCandidatePrefs.itemType);
  const [categoryGroup, setCategoryGroup] = useState(savedCandidatePrefs.categoryGroup);
  const [brand, setBrand] = useState(savedCandidatePrefs.brand);
  const [quality, setQuality] = useState(savedCandidatePrefs.quality);
  const [color, setColor] = useState(savedCandidatePrefs.color);
  const [model, setModel] = useState("");
  const [priceMin, setPriceMin] = useState("");
  const [priceMax, setPriceMax] = useState("");
  const [candidateStatus, setCandidateStatus] = useState(savedCandidatePrefs.candidateStatus);
  const [isWideList, setIsWideList] = useState(savedCandidatePrefs.isWideList);
  const [compareTab, setCompareTab] = useState<CompareTab>("summary");
  const [decisionReasonCode, setDecisionReasonCode] = useState<MatchingDecisionReasonCode | "">("");
  const [bulkSelectionState, setBulkSelectionState] = useState<BulkSelectionState>(() => ({
    scope: "",
    value: new Set(),
  }));
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  const pageSize = 25;

  const search = searchState.productId === selectedProductId ? searchState.value : "";
  const selectedCandidateId = selectedCandidateState.productId === selectedProductId ? selectedCandidateState.value : null;
  const page = pageState.productId === selectedProductId ? pageState.value : 1;
  const bulkSelectionScope = useMemo(
    () =>
      [
        selectedProductId ?? "",
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
      ].join("|"),
    [
      brand,
      candidateStatus,
      categoryGroup,
      color,
      debouncedSearch,
      includeRejected,
      itemType,
      model,
      onlyInStock,
      page,
      priceMax,
      priceMin,
      selectedProductId,
      source,
      quality,
    ]
  );
  const setSelectedCandidateId = useCallback((candidateId: number | null) => {
    setSelectedCandidateState({ productId: selectedProductId, value: candidateId });
  }, [selectedProductId]);
  const setPage = useCallback((value: number | ((previous: number) => number)) => {
    setPageState((state) => {
      const previous = state.productId === selectedProductId ? state.value : 1;
      return {
        productId: selectedProductId,
        value: typeof value === "function" ? value(previous) : value,
      };
    });
  }, [selectedProductId]);

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
  }, [search, setPage]);

  useEffect(() => {
    writeCandidateFilterPrefs({
      onlyInStock,
      includeRejected,
      source,
      itemType,
      categoryGroup,
      brand,
      quality,
      color,
      candidateStatus,
      isWideList,
    });
  }, [
    brand,
    candidateStatus,
    categoryGroup,
    color,
    includeRejected,
    isWideList,
    itemType,
    onlyInStock,
    quality,
    source,
  ]);

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
  const canAcceptSelected = canAcceptCandidate(selectedCandidate);
  const canRevokeSelected = selectedCandidate?.status === "current" || selectedCandidate?.status === "rejected";
  const selectedCandidateItemId = selectedCandidate?.competitor_item_id;
  const visibleRejectableIds = useMemo(
    () =>
      items
        .filter(canBulkRejectCandidate)
        .map((candidate) => candidate.competitor_item_id)
        .filter((candidateId): candidateId is number => typeof candidateId === "number"),
    [items]
  );
  const bulkSelectedIds = useMemo(() => {
    if (bulkSelectionState.scope !== bulkSelectionScope) return new Set<number>();
    const visibleIds = new Set(visibleRejectableIds);
    return new Set([...bulkSelectionState.value].filter((candidateId) => visibleIds.has(candidateId)));
  }, [bulkSelectionScope, bulkSelectionState, visibleRejectableIds]);
  const selectedBulkCount = bulkSelectedIds.size;
  const allVisibleRejectableSelected =
    visibleRejectableIds.length > 0 && visibleRejectableIds.every((candidateId) => bulkSelectedIds.has(candidateId));
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

  useEffect(() => {
    if (!selectAllRef.current) return;
    selectAllRef.current.indeterminate = selectedBulkCount > 0 && !allVisibleRejectableSelected;
  }, [allVisibleRejectableSelected, selectedBulkCount]);

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
    mutationFn: (candidate: Candidate) =>
      acceptItemMatch(selectedProductId!, candidate.competitor_item_id!, "confirmed_attributes"),
    onSuccess: () => {
      setSelectedCandidateId(null);
      setDecisionReasonCode("");
      invalidateMatching();
      toast.success("Сопоставление принято");
      onAfterDecision?.();
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось принять сопоставление")),
  });

  const rejectMutation = useMutation({
    mutationFn: (candidate: Candidate) =>
      rejectItemMatch(selectedProductId!, candidate.competitor_item_id!, decisionReasonCode || "other"),
    onSuccess: () => {
      setSelectedCandidateId(null);
      setDecisionReasonCode("");
      setBulkSelectionState({ scope: bulkSelectionScope, value: new Set() });
      invalidateMatching();
      toast.success("Кандидат отклонен");
      onAfterDecision?.();
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось отклонить кандидата")),
  });

  const bulkRejectMutation = useMutation({
    mutationFn: (candidateIds: number[]) =>
      bulkRejectItemMatches(selectedProductId!, candidateIds, "duplicate_or_irrelevant", "bulk_ui_reject"),
    onSuccess: (response) => {
      setSelectedCandidateId(null);
      setBulkSelectionState({ scope: bulkSelectionScope, value: new Set() });
      invalidateMatching();
      const skippedText = response.skipped_count ? `, пропущено ${response.skipped_count}` : "";
      toast.success(`Отклонено ${response.rejected_count}${skippedText}`);
      onAfterDecision?.();
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось отклонить выбранных кандидатов")),
  });

  const revokeMutation = useMutation({
    mutationFn: (candidate: Candidate) =>
      revokeItemMatch(selectedProductId!, candidate.competitor_item_id!, decisionReasonCode || "other"),
    onSuccess: (_response, candidate) => {
      setSelectedCandidateId(null);
      setDecisionReasonCode("");
      setBulkSelectionState({ scope: bulkSelectionScope, value: new Set() });
      invalidateMatching();
      toast.success(candidate.status === "rejected" ? "Отклонение снято" : "Сопоставление снято");
    },
    onError: (error) => toast.error(matchingErrorMessage(error, "Не удалось снять действие")),
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
      if (!isTyping && e.key.toLowerCase() === "n") {
        e.preventDefault();
        onNextProduct?.();
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
  }, [
    acceptMutation,
    canAcceptSelected,
    closePicker,
    isPickerOpen,
    items,
    onNextProduct,
    selectedCandidate,
    setSelectedCandidateId,
  ]);

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
    if (!hasRequiredDecisionReason("reject", decisionReasonCode)) {
      toast.error("Выберите причину отклонения");
      return;
    }
    rejectMutation.mutate(selectedCandidate);
  };

  const toggleBulkCandidate = (candidateId: number, checked: boolean) => {
    setBulkSelectionState((previous) => {
      const next = previous.scope === bulkSelectionScope ? new Set(previous.value) : new Set<number>();
      if (checked) {
        next.add(candidateId);
      } else {
        next.delete(candidateId);
      }
      return { scope: bulkSelectionScope, value: next };
    });
  };

  const toggleAllVisibleRejectable = (checked: boolean) => {
    setBulkSelectionState((previous) => {
      const next = previous.scope === bulkSelectionScope ? new Set(previous.value) : new Set<number>();
      visibleRejectableIds.forEach((candidateId) => {
        if (checked) {
          next.add(candidateId);
        } else {
          next.delete(candidateId);
        }
      });
      return { scope: bulkSelectionScope, value: next };
    });
  };

  const clearBulkSelection = () => {
    setBulkSelectionState({ scope: bulkSelectionScope, value: new Set() });
  };

  const rejectBulkSelected = () => {
    if (!selectedBulkCount || bulkRejectMutation.isPending) return;
    bulkRejectMutation.mutate(Array.from(bulkSelectedIds));
  };

  const revokeSelected = () => {
    if (!selectedCandidate?.competitor_item_id) return;
    if (!hasRequiredDecisionReason("revoke", decisionReasonCode)) {
      toast.error("Выберите причину снятия решения");
      return;
    }
    const confirmText =
      selectedCandidate.status === "rejected" ? "Снять отклонение с кандидата?" : "Снять принятое сопоставление?";
    if (window.confirm(confirmText)) {
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
          <button className="btn btn--ghost" onClick={onNextProduct} title="Следующий товар (N)">
            Следующий
          </button>
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
          <span>Совм. 1С</span>
          <strong title={selectedProduct?.compatibility_models?.join(", ") || undefined}>
            {selectedProduct?.compatibility_models?.length
              ? selectedProduct.compatibility_models.join(", ")
              : "-"}
          </strong>
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

        <div className="picker__bulkbar">
          <span>Выбрано {selectedBulkCount}</span>
          <div className="picker__bulkbar-actions">
            <button
              className="btn btn--ghost"
              onClick={() => toggleAllVisibleRejectable(true)}
              disabled={!visibleRejectableIds.length || allVisibleRejectableSelected || bulkRejectMutation.isPending}
            >
              Выбрать все на странице
            </button>
            <button
              className="btn btn--ghost"
              onClick={clearBulkSelection}
              disabled={!selectedBulkCount || bulkRejectMutation.isPending}
            >
              Снять выбор
            </button>
            <button
              className="btn btn--danger"
              onClick={rejectBulkSelected}
              disabled={!selectedBulkCount || bulkRejectMutation.isPending}
            >
              Отклонить выбранные
            </button>
          </div>
        </div>

        <div className="candidate-table">
          <div className="candidate-table__head">
            <span className="candidate-table__select">
              <input
                ref={selectAllRef}
                className="candidate-table__checkbox"
                type="checkbox"
                checked={allVisibleRejectableSelected}
                disabled={!visibleRejectableIds.length || bulkRejectMutation.isPending}
                aria-label="Выбрать всех доступных для отклонения кандидатов на странице"
                onChange={(e) => toggleAllVisibleRejectable(e.target.checked)}
              />
            </span>
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
              <div
                key={candidate.competitor_item_id}
                role="button"
                tabIndex={0}
                className={`candidate-row ${
                  candidate.competitor_item_id === selectedCandidate?.competitor_item_id ? "candidate-row--selected" : ""
                }`}
                onClick={() => setSelectedCandidateId(candidate.competitor_item_id ?? null)}
                onKeyDown={(event) => {
                  const target = event.target as HTMLElement | null;
                  if (target?.tagName === "INPUT") return;
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setSelectedCandidateId(candidate.competitor_item_id ?? null);
                  }
                }}
              >
                <span className="candidate-row__select">
                  <input
                    className="candidate-table__checkbox"
                    type="checkbox"
                    checked={
                      typeof candidate.competitor_item_id === "number" &&
                      bulkSelectedIds.has(candidate.competitor_item_id)
                    }
                    disabled={!canBulkRejectCandidate(candidate) || bulkRejectMutation.isPending}
                    aria-label={`Выбрать кандидата ${candidate.name || candidate.sku || ""}`}
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => {
                      event.stopPropagation();
                      if (typeof candidate.competitor_item_id === "number") {
                        toggleBulkCandidate(candidate.competitor_item_id, event.target.checked);
                      }
                    }}
                  />
                </span>
                <span>{candidate.competitor_name || "-"}</span>
                <strong>{candidate.name || "Без названия"}</strong>
                <span>{candidate.sku || "-"}</span>
                <span>{candidate.price ?? "-"}</span>
                <span className="candidate-row__status">
                  <span className={`badge badge--${candidate.status || "none"}`}>
                    {STATUS_TEXT[candidate.status || "available"] || candidate.status}
                  </span>
                  {shouldShowCompatibilityBadge(candidate) && (
                    <span
                      className={`badge badge--compat-${candidate.compatibility_hint?.status}`}
                      title={compatibilityTitle(candidate)}
                    >
                      {candidate.compatibility_hint?.label}
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
              </div>
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
                  <div className="kv">
                    <span>Совместимость</span>
                    <strong title={selectedCandidate.compatibility_hint?.detail || undefined}>
                      {compatibilitySummary(selectedCandidate)}
                    </strong>
                  </div>
                  {selectedCandidate.compatibility_hint?.status === "required" && (
                    <p className="picker__note">
                      {selectedCandidate.compatibility_hint.detail ||
                        "Совместимость не заведена. Если модель или код в названии/SKU совпадает с нашим товаром, нажмите «Принять». Если не совпадает или система не принимает, нажмите «Отклонить»."}
                    </p>
                  )}
                  {isAutoCompatibilityHint(selectedCandidate) && (
                    <p className="picker__note picker__note--ok">
                      {selectedCandidate.compatibility_hint?.detail}
                    </p>
                  )}
                  {selectedCandidate.reason && <p className="picker__note">{selectedCandidate.reason}</p>}
                  {selectedCandidate.url && (
                    <a className="picker__link" href={selectedCandidate.url} target="_blank" rel="noreferrer">
                      Открыть у конкурента
                    </a>
                  )}
                  <div className="picker__actions">
                    <label>
                      <span className="sr-only">Причина решения</span>
                      <select
                        value={decisionReasonCode}
                        onChange={(event) =>
                          setDecisionReasonCode(event.target.value as MatchingDecisionReasonCode | "")
                        }
                        aria-label="Причина отклонения или снятия"
                      >
                        <option value="">Выберите причину</option>
                        {REJECT_REASON_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </label>
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
                        selectedCandidate.status === "rejected" ||
                        rejectMutation.isPending
                      }
                    >
                      Отклонить
                    </button>
                    <button
                      className="btn btn--danger"
                      onClick={revokeSelected}
                      disabled={!canRevokeSelected || revokeMutation.isPending}
                    >
                      {selectedCandidate.status === "rejected" ? "Снять отклонение" : "Снять"}
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
