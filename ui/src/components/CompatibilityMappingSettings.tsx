import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  applyCompatibilityMapping,
  blockCompatibilityMapping,
  createCompatibilityBrand,
  createCompatibilityBrandAlias,
  createCompatibilityModel,
  fetchCompatibilityBrandAliases,
  fetchCompatibilityBrands,
  fetchCompatibilityHistory,
  fetchCompatibilityModels,
  fetchCompatibilitySummary,
  fetchCompatibilityUnresolvedGroups,
  patchCompatibilityBrandAlias,
  previewCompatibilityMapping,
} from "../api/matching";
import type {
  CompatibilityBrandPayload,
  CompatibilityPhoneModel,
  CompatibilityPreviewResponse,
  CompatibilityUnresolvedGroup,
} from "../api/types";
import { DisplayFamilyRegistryPanel } from "./DisplayFamilyRegistryPanel";

interface CompatibilityMappingSettingsProps {
  open: boolean;
  onClose: () => void;
}

type CompatibilityTab = "queue" | "families" | "models" | "aliases" | "history";
type EntityFilter = "" | "product" | "competitor_item";
type BrandFilter = "all" | "none" | number;
type BlockReason = "noise" | "not_phone" | "bad_1c_value" | "not_supported" | "other";
type SuggestionKind = NonNullable<CompatibilityPhoneModel["suggestion_kind"]>;

const EMPTY_BRAND_FORM: CompatibilityBrandPayload = {
  code: "",
  name: "",
  display_name: "",
  group_code: "",
};

const BLOCK_REASONS: Array<{ value: BlockReason; label: string }> = [
  { value: "noise", label: "шум" },
  { value: "not_phone", label: "не телефон" },
  { value: "bad_1c_value", label: "мусор 1С" },
  { value: "not_supported", label: "не поддерживаем" },
  { value: "other", label: "другое" },
];

const SUGGESTION_KIND_META: Record<SuggestionKind, { label: string; tone: string }> = {
  exact_base: { label: "точное", tone: "ok" },
  exact_variant: { label: "вариант", tone: "ok" },
  hardware_variant: { label: "аппаратный", tone: "muted" },
  related_family: { label: "похожая", tone: "warn" },
};

function entityLabel(value: string) {
  if (value === "product") return "Наши";
  if (value === "competitor_item") return "Конкуренты";
  return value;
}

function modelLabel(model: CompatibilityPhoneModel) {
  return [model.brand_display_name || model.brand, model.model_name, model.variant].filter(Boolean).join(" ");
}

function findSafeAutoselectModel(group: CompatibilityUnresolvedGroup) {
  if (group.is_noise_candidate || !group.safe_auto_model_id) return null;
  return group.suggested_phone_models.find((model) => model.id === group.safe_auto_model_id) ?? null;
}

function suggestionKindMeta(model?: CompatibilityPhoneModel | null) {
  if (!model?.suggestion_kind) return null;
  return SUGGESTION_KIND_META[model.suggestion_kind] ?? null;
}

function stripBrandPrefix(value: string, group: CompatibilityUnresolvedGroup) {
  const candidates = [group.raw_brand, group.brand_display_name].filter(Boolean) as string[];
  return candidates.reduce((current, brand) => {
    const pattern = new RegExp(`^\\s*${brand.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s+`, "i");
    return current.replace(pattern, "").trim();
  }, value.trim());
}

function modelSearchValueForGroup(group: CompatibilityUnresolvedGroup) {
  const rawModel = group.raw_model?.trim();
  if (rawModel) return stripBrandPrefix(rawModel, group) || rawModel;
  return stripBrandPrefix(group.raw_value, group) || group.raw_value;
}

function groupStatus(group: CompatibilityUnresolvedGroup) {
  if (group.is_noise_candidate) return { label: "шум", tone: "noise" };
  if (group.safe_auto_model_id) return { label: "авто", tone: "ok" };
  if (group.suggested_phone_models.some((model) => model.suggestion_kind === "exact_base" || model.suggestion_kind === "exact_variant")) {
    return { label: "точная", tone: "ok" };
  }
  if (group.suggested_phone_models.length > 1) return { label: "несколько", tone: "warn" };
  return { label: "нет подсказки", tone: "muted" };
}

function groupSuggestionLabel(group: CompatibilityUnresolvedGroup) {
  if (group.is_noise_candidate) return "Скрыть как шум";
  const firstSuggestion = findSafeAutoselectModel(group) ?? group.suggested_phone_models[0];
  if (!firstSuggestion) return "-";
  return modelLabel(firstSuggestion);
}

function groupPayload(group: CompatibilityUnresolvedGroup, brandId: number | null, modelIds: number[]) {
  return {
    group_key: group.group_key,
    entity_type: group.entity_type,
    source: group.source ?? null,
    raw_value: group.raw_value,
    raw_brand: group.raw_brand ?? null,
    raw_model: group.raw_model ?? null,
    raw_variant: group.raw_variant ?? null,
    brand_id: brandId,
    target_phone_model_ids: modelIds,
  };
}

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function CompatibilityMappingSettings({ open, onClose }: CompatibilityMappingSettingsProps) {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<CompatibilityTab>("queue");
  const [brandSearch, setBrandSearch] = useState("");
  const [modelSearch, setModelSearch] = useState("");
  const [queueSearch, setQueueSearch] = useState("");
  const [aliasSearch, setAliasSearch] = useState("");
  const [entityFilter, setEntityFilter] = useState<EntityFilter>("");
  const [brandFilter, setBrandFilter] = useState<BrandFilter>("all");
  const [onlyBrandsWithQueue, setOnlyBrandsWithQueue] = useState(true);
  const [selectedGroup, setSelectedGroup] = useState<CompatibilityUnresolvedGroup | null>(null);
  const [selectedModelsById, setSelectedModelsById] = useState<Record<number, CompatibilityPhoneModel>>({});
  const [autoSelectedModelId, setAutoSelectedModelId] = useState<number | null>(null);
  const [preview, setPreview] = useState<CompatibilityPreviewResponse | null>(null);
  const [showBlockConfirm, setShowBlockConfirm] = useState(false);
  const [blockReason, setBlockReason] = useState<BlockReason>("noise");
  const [blockNotes, setBlockNotes] = useState("");
  const [brandForm, setBrandForm] = useState<CompatibilityBrandPayload>(EMPTY_BRAND_FORM);
  const [modelForm, setModelForm] = useState({ model_name: "", variant: "" });
  const [aliasForm, setAliasForm] = useState({ raw_value: "", source: "manual" });

  const selectedBrandId = typeof brandFilter === "number" ? brandFilter : null;
  const selectedModelIds = useMemo(
    () => Object.keys(selectedModelsById).map((value) => Number(value)),
    [selectedModelsById]
  );
  const selectedModels = useMemo(() => Object.values(selectedModelsById), [selectedModelsById]);
  const autoSelectedModel = autoSelectedModelId ? selectedModelsById[autoSelectedModelId] : null;
  const selectedModelBrandId = selectedModels.find((model) => model.brand_id)?.brand_id ?? null;
  const mappingBrandId = selectedGroup?.brand_id ?? selectedBrandId ?? selectedModelBrandId ?? null;
  const modelBrandId = selectedGroup?.brand_id ?? selectedBrandId ?? null;
  const aliasBrandId = selectedBrandId ?? selectedGroup?.brand_id ?? null;
  const needsExplicitBrand = Boolean(selectedGroup && !selectedGroup.brand_id && !mappingBrandId);
  const selectedGroupStatus = selectedGroup ? groupStatus(selectedGroup) : null;

  const { data: summary } = useQuery({
    queryKey: ["compatibility-summary"],
    queryFn: fetchCompatibilitySummary,
    enabled: open,
  });

  const { data: brandsData, isLoading: isBrandsLoading } = useQuery({
    queryKey: ["compatibility-brands", brandSearch],
    queryFn: () => fetchCompatibilityBrands({ q: brandSearch || undefined, limit: 300 }),
    enabled: open,
  });
  const brands = useMemo(() => brandsData ?? [], [brandsData]);
  const selectedBrand = brands.find((brand) => brand.id === selectedBrandId) ?? null;
  const visibleBrands = useMemo(
    () =>
      brands
        .filter((brand) => !onlyBrandsWithQueue || brand.unresolved_count > 0)
        .sort((left, right) => right.unresolved_count - left.unresolved_count || left.display_name.localeCompare(right.display_name)),
    [brands, onlyBrandsWithQueue]
  );

  const { data: groupsData, isLoading: isGroupsLoading } = useQuery({
    queryKey: ["compatibility-unresolved-groups", brandFilter, entityFilter, queueSearch],
    queryFn: () =>
      fetchCompatibilityUnresolvedGroups({
        brand_id: selectedBrandId ?? undefined,
        without_brand: brandFilter === "none" || undefined,
        entity_type: entityFilter || undefined,
        q: queueSearch || undefined,
        limit: 200,
      }),
    enabled: open,
  });
  const groups = useMemo(() => groupsData ?? [], [groupsData]);

  const { data: modelsData, isLoading: isModelsLoading } = useQuery({
    queryKey: ["compatibility-models", modelBrandId, modelSearch],
    queryFn: () =>
      fetchCompatibilityModels({
        brand_id: modelBrandId ?? undefined,
        q: modelSearch || (selectedGroup ? modelSearchValueForGroup(selectedGroup) : undefined),
        limit: 200,
      }),
    enabled: open,
  });
  const models = useMemo(() => modelsData ?? [], [modelsData]);

  const { data: aliasesData, isLoading: isAliasesLoading } = useQuery({
    queryKey: ["compatibility-brand-aliases", aliasBrandId, aliasSearch],
    queryFn: () =>
      fetchCompatibilityBrandAliases({
        brand_id: aliasBrandId ?? undefined,
        q: aliasSearch || undefined,
        include_inactive: true,
        limit: 200,
      }),
    enabled: open && Boolean(aliasBrandId),
  });
  const aliases = useMemo(() => aliasesData ?? [], [aliasesData]);

  const { data: historyData, isLoading: isHistoryLoading } = useQuery({
    queryKey: ["compatibility-history"],
    queryFn: () => fetchCompatibilityHistory({ limit: 80 }),
    enabled: open,
  });
  const history = useMemo(() => historyData ?? [], [historyData]);

  const invalidateCompatibility = () => {
    queryClient.invalidateQueries({ queryKey: ["compatibility-summary"] });
    queryClient.invalidateQueries({ queryKey: ["compatibility-brands"] });
    queryClient.invalidateQueries({ queryKey: ["compatibility-unresolved"] });
    queryClient.invalidateQueries({ queryKey: ["compatibility-unresolved-groups"] });
    queryClient.invalidateQueries({ queryKey: ["compatibility-models"] });
    queryClient.invalidateQueries({ queryKey: ["compatibility-brand-aliases"] });
    queryClient.invalidateQueries({ queryKey: ["compatibility-history"] });
    queryClient.invalidateQueries({ queryKey: ["products"] });
    queryClient.invalidateQueries({ queryKey: ["candidate-search"] });
  };

  const previewMutation = useMutation({
    mutationFn: () => {
      if (!selectedGroup || !selectedModelIds.length || needsExplicitBrand) {
        throw new Error("Выберите группу, бренд и модели");
      }
      return previewCompatibilityMapping(groupPayload(selectedGroup, mappingBrandId, selectedModelIds));
    },
    onSuccess: (result) => setPreview(result),
    onError: () => toast.error("Не удалось построить предпросмотр"),
  });

  const applyMutation = useMutation({
    mutationFn: () => {
      if (!selectedGroup || !preview || needsExplicitBrand) {
        throw new Error("Сначала выполните предпросмотр");
      }
      return applyCompatibilityMapping({
        ...groupPayload(selectedGroup, mappingBrandId, selectedModelIds),
        preview_token: preview.preview_token,
        scope: "group",
      });
    },
    onSuccess: (result) => {
      toast.success(`Применено: ${result.affected_count}`);
      setSelectedGroup(null);
      setSelectedModelsById({});
      setAutoSelectedModelId(null);
      setPreview(null);
      setShowBlockConfirm(false);
      invalidateCompatibility();
    },
    onError: () => toast.error("Не удалось применить мапинг"),
  });

  const blockMutation = useMutation({
    mutationFn: () => {
      if (!selectedGroup) {
        throw new Error("Выберите группу");
      }
      return blockCompatibilityMapping({
        group_key: selectedGroup.group_key,
        entity_type: selectedGroup.entity_type,
        source: selectedGroup.source ?? null,
        raw_value: selectedGroup.raw_value,
        raw_brand: selectedGroup.raw_brand ?? null,
        raw_model: selectedGroup.raw_model ?? null,
        raw_variant: selectedGroup.raw_variant ?? null,
        reason: blockReason,
        notes: blockNotes || null,
      });
    },
    onSuccess: (result) => {
      toast.success(`Скрыто из очереди: ${result.affected_count}`);
      setSelectedGroup(null);
      setSelectedModelsById({});
      setAutoSelectedModelId(null);
      setPreview(null);
      setShowBlockConfirm(false);
      setBlockNotes("");
      invalidateCompatibility();
    },
    onError: () => toast.error("Не удалось скрыть значение"),
  });

  const createBrandMutation = useMutation({
    mutationFn: () => createCompatibilityBrand(brandForm),
    onSuccess: (brand) => {
      toast.success("Бренд сохранен");
      setBrandFilter(brand.id);
      setBrandForm(EMPTY_BRAND_FORM);
      invalidateCompatibility();
    },
    onError: () => toast.error("Не удалось сохранить бренд"),
  });

  const createModelMutation = useMutation({
    mutationFn: () => {
      if (!modelBrandId) {
        throw new Error("Бренд не выбран");
      }
      return createCompatibilityModel({
        brand_id: modelBrandId,
        model_name: modelForm.model_name,
        variant: modelForm.variant || null,
      });
    },
    onSuccess: (model) => {
      toast.success("Модель сохранена");
      setSelectedModelsById((current) => ({ ...current, [model.id]: model }));
      setAutoSelectedModelId(null);
      setPreview(null);
      setModelForm({ model_name: "", variant: "" });
      invalidateCompatibility();
    },
    onError: () => toast.error("Не удалось сохранить модель"),
  });

  const createAliasMutation = useMutation({
    mutationFn: () => {
      if (!aliasBrandId) {
        throw new Error("Бренд не выбран");
      }
      return createCompatibilityBrandAlias({
        brand_id: aliasBrandId,
        raw_value: aliasForm.raw_value,
        source: aliasForm.source || "manual",
      });
    },
    onSuccess: () => {
      toast.success("Алиас сохранен");
      setAliasForm({ raw_value: "", source: "manual" });
      invalidateCompatibility();
    },
    onError: () => toast.error("Не удалось сохранить алиас"),
  });

  const toggleAliasMutation = useMutation({
    mutationFn: ({ aliasId, isActive }: { aliasId: number; isActive: boolean }) =>
      patchCompatibilityBrandAlias(aliasId, { is_active: isActive }),
    onSuccess: () => {
      toast.success("Алиас обновлен");
      invalidateCompatibility();
    },
    onError: () => toast.error("Не удалось обновить алиас"),
  });

  const toggleModel = (model: CompatibilityPhoneModel) => {
    setAutoSelectedModelId(null);
    setSelectedModelsById((current) => {
      const next = { ...current };
      if (next[model.id]) {
        delete next[model.id];
      } else {
        next[model.id] = model;
      }
      return next;
    });
    setPreview(null);
  };

  const selectGroup = (group: CompatibilityUnresolvedGroup) => {
    const autoModel = findSafeAutoselectModel(group);
    setSelectedGroup(group);
    setSelectedModelsById(autoModel ? { [autoModel.id]: autoModel } : {});
    setAutoSelectedModelId(autoModel?.id ?? null);
    setPreview(null);
    setShowBlockConfirm(false);
    setBlockReason(group.is_noise_candidate ? "noise" : "other");
    setBlockNotes("");
    setModelSearch(modelSearchValueForGroup(group));
  };

  const selectBrandFilter = (value: BrandFilter) => {
    setBrandFilter(value);
    setSelectedGroup(null);
    setSelectedModelsById({});
    setAutoSelectedModelId(null);
    setPreview(null);
    setShowBlockConfirm(false);
  };

  if (!open) return null;

  return (
    <div className="settings-overlay" role="dialog" aria-modal="true">
      <div className={`settings-shell compatibility-shell ${activeTab === "families" ? "compatibility-shell--families" : ""}`}>
        <header className="settings-shell__topbar">
          <div>
            <span>BITRIX MATCHING</span>
            <h2>Совместимость</h2>
          </div>
          <button className="btn btn--ghost" onClick={onClose} type="button">
            Закрыть
          </button>
        </header>

        {activeTab !== "families" && <aside className="settings-profiles compatibility-brands">
          <div className="picker__section-title">Бренды</div>
          <input
            className="app__search"
            placeholder="Поиск бренда"
            value={brandSearch}
            onChange={(event) => setBrandSearch(event.target.value)}
          />
          <div className="compatibility-summary">
            <span>{summary?.phone_models ?? 0} моделей</span>
            <span>{(summary?.unresolved_product_values ?? 0) + (summary?.unresolved_competitor_values ?? 0)} в очереди</span>
          </div>
          <label className="compatibility-toggle">
            <input
              type="checkbox"
              checked={onlyBrandsWithQueue}
              onChange={(event) => setOnlyBrandsWithQueue(event.target.checked)}
            />
            <span>Только с очередью</span>
          </label>
          <button
            className={`settings-profile ${brandFilter === "all" ? "settings-profile--active" : ""}`}
            type="button"
            onClick={() => selectBrandFilter("all")}
          >
            <strong>Все бренды</strong>
            <span>общая очередь</span>
            <small>{(summary?.unresolved_product_values ?? 0) + (summary?.unresolved_competitor_values ?? 0)} значений</small>
          </button>
          <button
            className={`settings-profile ${brandFilter === "none" ? "settings-profile--active" : ""}`}
            type="button"
            onClick={() => selectBrandFilter("none")}
          >
            <strong>Без бренда</strong>
            <span>ручной разбор</span>
            <small>шум и неизвестные значения</small>
          </button>
          {isBrandsLoading && <div className="panel__loading">Загрузка...</div>}
          {visibleBrands.map((brand) => (
            <button
              key={brand.id}
              className={`settings-profile ${brand.id === selectedBrandId ? "settings-profile--active" : ""}`}
              type="button"
              onClick={() => selectBrandFilter(brand.id)}
            >
              <strong>{brand.display_name}</strong>
              <span>{brand.group_code || brand.code}</span>
              <small>{brand.models_count} моделей · {brand.unresolved_count} в очереди</small>
            </button>
          ))}
        </aside>}

        <main className={`settings-rules compatibility-main ${activeTab === "families" ? "compatibility-main--families" : ""}`}>
          <div className="settings-editor-tabs compatibility-tabs" role="tablist">
            {[
              ["queue", "Очередь"],
              ["families", "Семьи дисплеев"],
              ["models", "Модели"],
              ["aliases", "Алиасы"],
              ["history", "История"],
            ].map(([value, label]) => (
              <button
                key={value}
                className={`settings-editor-tab ${activeTab === value ? "settings-editor-tab--active" : ""}`}
                type="button"
                onClick={() => setActiveTab(value as CompatibilityTab)}
              >
                {label}
              </button>
            ))}
          </div>

          {activeTab === "families" && <DisplayFamilyRegistryPanel />}

          {activeTab === "queue" && (
            <>
              <div className="compatibility-filters">
                <select
                  className="app__select"
                  value={entityFilter}
                  onChange={(event) => setEntityFilter(event.target.value as EntityFilter)}
                >
                  <option value="">Все</option>
                  <option value="product">Наши</option>
                  <option value="competitor_item">Конкуренты</option>
                </select>
                <input
                  className="app__search"
                  placeholder="Поиск в очереди"
                  value={queueSearch}
                  onChange={(event) => setQueueSearch(event.target.value)}
                />
              </div>
              <div className="settings-value-map-list compatibility-list">
                <div className="settings-suggestion-head compatibility-row compatibility-row--group">
                  <span>Значение</span>
                  <span>Бренд</span>
                  <span>Источник</span>
                  <span>Затронет</span>
                  <span>Подсказка</span>
                </div>
                {isGroupsLoading && <div className="panel__loading">Загрузка...</div>}
                {groups.map((group) => {
                  const status = groupStatus(group);
                  const hiddenSuggestionsCount = Math.max(group.suggested_phone_models.length - 1, 0);
                  const primarySuggestion = findSafeAutoselectModel(group) ?? group.suggested_phone_models[0];
                  const primarySuggestionMeta = suggestionKindMeta(primarySuggestion);
                  return (
                    <button
                      key={group.group_key}
                      className={`settings-suggestion-row compatibility-row compatibility-row--group ${
                        selectedGroup?.group_key === group.group_key ? "settings-rule-row--active" : ""
                      }`}
                      type="button"
                      onClick={() => selectGroup(group)}
                    >
                      <strong className="compatibility-value-cell">
                        <span>{group.raw_value}</span>
                        <span className={`compatibility-badge compatibility-badge--${status.tone}`}>{status.label}</span>
                      </strong>
                      <span>{group.brand_display_name || group.raw_brand || "-"}</span>
                      <span>{group.source || "-"}</span>
                      <span className="compatibility-badge-row">
                        <span className="compatibility-badge compatibility-badge--strong">{group.affected_count}</span>
                        <span className="compatibility-badge">{group.product_count} наших</span>
                        <span className="compatibility-badge">{group.competitor_count} конк.</span>
                      </span>
                      <span className="compatibility-suggestion-cell">
                        <span>{groupSuggestionLabel(group)}</span>
                        {primarySuggestionMeta && (
                          <span className={`compatibility-badge compatibility-badge--${primarySuggestionMeta.tone}`}>
                            {primarySuggestionMeta.label}
                          </span>
                        )}
                        {hiddenSuggestionsCount > 0 && <span className="compatibility-badge">+{hiddenSuggestionsCount}</span>}
                      </span>
                    </button>
                  );
                })}
                {!isGroupsLoading && !groups.length && <div className="panel__empty">Очередь пуста</div>}
              </div>
            </>
          )}

          {activeTab === "models" && (
            <>
              <div className="compatibility-filters">
                <input
                  className="app__search"
                  placeholder="Поиск модели"
                  value={modelSearch}
                  onChange={(event) => setModelSearch(event.target.value)}
                />
              </div>
              <div className="settings-value-map-list compatibility-list">
                {isModelsLoading && <div className="panel__loading">Загрузка...</div>}
                {models.map((model) => (
                  <label key={model.id} className="compatibility-model-option">
                    <input
                      type="checkbox"
                      checked={Boolean(selectedModelsById[model.id])}
                      onChange={() => toggleModel(model)}
                    />
                    <strong>{modelLabel(model)}</strong>
                    <span className="compatibility-model-meta">
                      {suggestionKindMeta(model) && (
                        <span className={`compatibility-badge compatibility-badge--${suggestionKindMeta(model)?.tone}`}>
                          {suggestionKindMeta(model)?.label}
                        </span>
                      )}
                      <span>{model.product_links_count} наших · {model.competitor_links_count} конкурентов</span>
                    </span>
                  </label>
                ))}
                {!isModelsLoading && !models.length && <div className="panel__empty">Модели не найдены</div>}
              </div>
            </>
          )}

          {activeTab === "aliases" && (
            <>
              <div className="compatibility-filters">
                <input
                  className="app__search"
                  placeholder="Поиск алиаса"
                  value={aliasSearch}
                  onChange={(event) => setAliasSearch(event.target.value)}
                />
              </div>
              <div className="settings-value-map-list compatibility-list">
                <div className="settings-suggestion-head compatibility-alias-row">
                  <span>Raw</span>
                  <span>Источник</span>
                  <span>Тип</span>
                  <span>Статус</span>
                  <span></span>
                </div>
                {isAliasesLoading && <div className="panel__loading">Загрузка...</div>}
                {aliases.map((alias) => (
                  <div key={alias.id} className="settings-suggestion-row compatibility-alias-row">
                    <strong>{alias.raw_value}</strong>
                    <span>{alias.source}</span>
                    <span>{alias.is_manual ? "manual" : "system"} · {alias.confidence ?? "-"}</span>
                    <span>{alias.is_active ? "активен" : "отключен"}</span>
                    <button
                      className="btn btn--ghost btn--compact"
                      type="button"
                      onClick={() => toggleAliasMutation.mutate({ aliasId: alias.id, isActive: !alias.is_active })}
                    >
                      {alias.is_active ? "Отключить" : "Включить"}
                    </button>
                  </div>
                ))}
                {!aliasBrandId && <div className="panel__empty">Выберите бренд</div>}
                {aliasBrandId && !isAliasesLoading && !aliases.length && <div className="panel__empty">Алиасы не найдены</div>}
              </div>
              <div className="settings-form compatibility-inline-form">
                <div className="picker__section-title">Новый алиас бренда</div>
                <label>
                  <span>Бренд</span>
                  <input className="app__search" value={selectedBrand?.display_name || selectedGroup?.brand_display_name || ""} readOnly />
                </label>
                <label>
                  <span>Сырое значение</span>
                  <input
                    className="app__search"
                    value={aliasForm.raw_value}
                    onChange={(event) => setAliasForm((form) => ({ ...form, raw_value: event.target.value }))}
                  />
                </label>
                <label>
                  <span>Источник</span>
                  <input
                    className="app__search"
                    value={aliasForm.source}
                    onChange={(event) => setAliasForm((form) => ({ ...form, source: event.target.value }))}
                  />
                </label>
                <button
                  className="btn"
                  type="button"
                  disabled={!aliasBrandId || !aliasForm.raw_value || createAliasMutation.isPending}
                  onClick={() => createAliasMutation.mutate()}
                >
                  Сохранить алиас
                </button>
              </div>
            </>
          )}

          {activeTab === "history" && (
            <div className="settings-value-map-list compatibility-list">
              {isHistoryLoading && <div className="panel__loading">Загрузка...</div>}
              {history.map((item) => (
                <div key={`${item.created_at}-${item.action}-${item.normalized_key}`} className="history-row">
                  <strong>{item.action === "map" ? "Мапинг" : "Скрыто"} · {item.raw_value}</strong>
                  <span>
                    {item.affected_count} строк · {item.brand_display_name || "-"} · {item.phone_model_labels.join(", ") || item.reason || "-"}
                  </span>
                  <small>{formatDateTime(item.created_at)} · {item.actor || "system"}</small>
                </div>
              ))}
              {!isHistoryLoading && !history.length && <div className="panel__empty">Истории пока нет</div>}
            </div>
          )}
        </main>

        {activeTab !== "families" && <aside className="settings-editor compatibility-editor">
          <div className="settings-panel__header">
            <div>
              <div className="picker__section-title">Редактор</div>
              <strong>{selectedGroup ? selectedGroup.raw_value : "Выберите группу"}</strong>
            </div>
          </div>

          {!selectedGroup && <div className="panel__empty">Выберите значение в очереди, чтобы открыть предпросмотр и действия.</div>}

          {selectedGroup && (
            <>
              <div className="compatibility-editor-summary">
                <span className={`compatibility-badge compatibility-badge--${selectedGroupStatus?.tone}`}>{selectedGroupStatus?.label}</span>
                {autoSelectedModel && <span className="compatibility-badge compatibility-badge--auto">автовыбрано</span>}
                <span>{entityLabel(selectedGroup.entity_type)} · {selectedGroup.source || "-"}</span>
                <span>{selectedGroup.affected_count} строк</span>
                <span>{selectedGroup.brand_display_name || selectedGroup.raw_brand || "без бренда"}</span>
              </div>

              {selectedGroup.examples.length ? (
                <div className="compatibility-preview compatibility-preview--compact">
                  <strong>Примеры</strong>
                  {selectedGroup.examples.map((item) => (
                    <span key={`${item.entity_type}-${item.entity_id}`}>{item.sample_name || item.raw_value}</span>
                  ))}
                </div>
              ) : null}

              {selectedGroup.suggested_phone_models.length ? (
                <div className="compatibility-suggestions">
                  <div className="picker__section-title">Подсказки</div>
                  {selectedGroup.suggested_phone_models.map((model, index) => (
                    <button
                      key={model.id}
                      className={`btn ${
                        model.id === selectedGroup.safe_auto_model_id || (index === 0 && selectedGroup.suggested_phone_models.length === 1)
                          ? ""
                          : "btn--ghost"
                      } btn--compact compatibility-suggestion-button ${
                        model.suggestion_kind === "related_family" ? "compatibility-suggestion-button--related" : ""
                      }`}
                      type="button"
                      onClick={() => toggleModel(model)}
                    >
                      <span>{selectedModelsById[model.id] ? "Убрать" : "Выбрать"} · {modelLabel(model)}</span>
                      {suggestionKindMeta(model) && (
                        <span className={`compatibility-badge compatibility-badge--${suggestionKindMeta(model)?.tone}`}>
                          {suggestionKindMeta(model)?.label}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              ) : null}

              <div className="compatibility-chip-list">
                {selectedModels.map((model) => (
                  <button
                    key={model.id}
                    className={`compatibility-chip ${model.id === autoSelectedModelId ? "compatibility-chip--auto" : ""}`}
                    type="button"
                    onClick={() => toggleModel(model)}
                  >
                    {model.id === autoSelectedModelId ? "авто · " : ""}
                    {modelLabel(model)}
                  </button>
                ))}
                {!selectedModels.length && <span className="compatibility-placeholder">Модели не выбраны</span>}
              </div>

              {needsExplicitBrand && <div className="settings-warning">Для значения без бренда выберите бренд или модель.</div>}

              <div className="compatibility-actions">
                <button
                  className="btn btn--ghost"
                  type="button"
                  disabled={!selectedModelIds.length || needsExplicitBrand || previewMutation.isPending}
                  onClick={() => previewMutation.mutate()}
                >
                  Предпросмотр
                </button>
                <button
                  className="btn"
                  type="button"
                  disabled={!preview || applyMutation.isPending}
                  onClick={() => applyMutation.mutate()}
                >
                  Применить
                </button>
                <button
                  className="btn btn--ghost"
                  type="button"
                  disabled={blockMutation.isPending}
                  onClick={() => setShowBlockConfirm((value) => !value)}
                >
                  Скрыть
                </button>
              </div>

              {showBlockConfirm && (
                <div className="settings-form compatibility-inline-form">
                  <div className="picker__section-title">Скрыть из очереди</div>
                  <label>
                    <span>Причина</span>
                    <select className="app__select" value={blockReason} onChange={(event) => setBlockReason(event.target.value as BlockReason)}>
                      {BLOCK_REASONS.map((reason) => (
                        <option key={reason.value} value={reason.value}>
                          {reason.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Заметка</span>
                    <input className="app__search" value={blockNotes} onChange={(event) => setBlockNotes(event.target.value)} />
                  </label>
                  <button className="btn btn--ghost" type="button" onClick={() => blockMutation.mutate()}>
                    Скрыть {selectedGroup.affected_count}
                  </button>
                </div>
              )}

              {preview && (
                <div className="settings-test__result compatibility-preview">
                  <strong>Будет затронуто: {preview.affected_count}</strong>
                  <span>Наши: {preview.affected_product_count} · конкуренты: {preview.affected_competitor_count}</span>
                  {preview.target_phone_models.length ? (
                    <span>{preview.target_phone_models.map(modelLabel).join(", ")}</span>
                  ) : null}
                  {preview.items.map((item) => (
                    <span key={`${item.entity_type}-${item.entity_id}`}>{item.sample_name || item.raw_value}</span>
                  ))}
                  {preview.warnings.map((warning) => (
                    <em key={warning}>{warning}</em>
                  ))}
                </div>
              )}

              <details className="compatibility-details">
                <summary>Новая модель</summary>
                <div className="settings-form compatibility-inline-form">
                  <label>
                    <span>Бренд</span>
                    <input className="app__search" value={selectedBrand?.display_name || selectedGroup.brand_display_name || ""} readOnly />
                  </label>
                  <label>
                    <span>Модель</span>
                    <input
                      className="app__search"
                      value={modelForm.model_name}
                      onChange={(event) => setModelForm((form) => ({ ...form, model_name: event.target.value }))}
                    />
                  </label>
                  <label>
                    <span>Вариант</span>
                    <input
                      className="app__search"
                      value={modelForm.variant}
                      onChange={(event) => setModelForm((form) => ({ ...form, variant: event.target.value }))}
                    />
                  </label>
                  <button
                    className="btn btn--ghost"
                    type="button"
                    disabled={!modelBrandId || !modelForm.model_name || createModelMutation.isPending}
                    onClick={() => createModelMutation.mutate()}
                  >
                    Создать модель
                  </button>
                </div>
              </details>

              <details className="compatibility-details">
                <summary>Новый бренд</summary>
                <form
                  className="settings-form compatibility-inline-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    createBrandMutation.mutate();
                  }}
                >
                  <label>
                    <span>Код</span>
                    <input
                      className="app__search"
                      value={brandForm.code}
                      onChange={(event) => setBrandForm((form) => ({ ...form, code: event.target.value }))}
                    />
                  </label>
                  <label>
                    <span>Название</span>
                    <input
                      className="app__search"
                      value={brandForm.display_name ?? ""}
                      onChange={(event) => setBrandForm((form) => ({ ...form, display_name: event.target.value }))}
                    />
                  </label>
                  <label>
                    <span>Группа</span>
                    <input
                      className="app__search"
                      value={brandForm.group_code ?? ""}
                      onChange={(event) => setBrandForm((form) => ({ ...form, group_code: event.target.value }))}
                    />
                  </label>
                  <button className="btn btn--ghost" type="submit" disabled={!brandForm.code || createBrandMutation.isPending}>
                    Создать бренд
                  </button>
                </form>
              </details>
            </>
          )}
        </aside>}
      </div>
    </div>
  );
}
