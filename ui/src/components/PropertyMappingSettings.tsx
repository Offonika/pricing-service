import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  acceptSafePropertyValueSuggestions,
  createPropertyRule,
  createPropertyValueMap,
  fetchPropertyComparison,
  fetchPropertyProfiles,
  fetchPropertyRules,
  fetchPropertyValueSuggestions,
  fetchPropertyValueMaps,
  restorePropertyRuleDefault,
  updatePropertyRule,
  updatePropertyValueMap,
} from "../api/matching";
import type {
  PropertyComparisonResponse,
  PropertyRule,
  PropertyRulePayload,
  PropertyValueMap,
  PropertyValueMapPayload,
  PropertyValueSuggestion,
} from "../api/types";

interface PropertyMappingSettingsProps {
  open: boolean;
  onClose: () => void;
  onOpenCompatibility?: () => void;
}

type EditorTab = "rule" | "values" | "test";
type ValuePanelMode = "dictionary" | "suggestions";

const COMPARISON_MODES = [
  { value: "exact", label: "Точное совпадение" },
  { value: "boolean", label: "Да / нет" },
  { value: "mapped_value", label: "Через словарь значений" },
  { value: "set_overlap", label: "Пересечение набора" },
  { value: "numeric_tolerance", label: "Число с допуском" },
];

const SEVERITIES = [
  { value: "block", label: "блокировать" },
  { value: "review", label: "на проверку" },
  { value: "hint", label: "подсказка" },
];

const PROPERTY_KEYS = [
  { value: "model", label: "Модель" },
  { value: "subject", label: "Предмет" },
  { value: "quality", label: "Качество" },
  { value: "color", label: "Цвет" },
  { value: "has_frame", label: "В рамке" },
  { value: "has_touch", label: "Тачскрин" },
  { value: "type", label: "Тип матрицы" },
  { value: "construction", label: "Конструкция" },
  { value: "backlight", label: "Подсветка" },
  { value: "refresh_rate", label: "Частота обновления" },
  { value: "capacity", label: "Емкость аккумулятора" },
  { value: "connector", label: "Разъем" },
  { value: "finish", label: "Отделка / цвет" },
];

const FIELD_OPTIONS = [
  { value: "subject", label: "Предмет товара" },
  { value: "compatibility.model", label: "Совместимые модели" },
  { value: "display.model", label: "Модель устройства" },
  { value: "display.quality", label: "Качество дисплея" },
  { value: "display.color", label: "Цвет" },
  { value: "display.has_frame", label: "Наличие рамки" },
  { value: "display.has_touch", label: "Наличие тачскрина" },
  { value: "display.type", label: "Тип матрицы" },
  { value: "display.construction", label: "Конструкция дисплея" },
  { value: "display.backlight", label: "Подсветка" },
  { value: "display.matrix_tags", label: "Метки матрицы" },
  { value: "display.refresh_rate_hz", label: "Частота обновления" },
  { value: "battery.capacity_mah", label: "Емкость аккумулятора" },
  { value: "connector.type", label: "Тип разъема" },
  { value: "attrs.finish", label: "Отделка конкурента" },
  { value: "attrs.color", label: "Цвет конкурента из атрибутов" },
  { value: "attrs.quality", label: "Качество конкурента из атрибутов" },
];

const STATUS_TEXT: Record<string, string> = {
  match: "совпадает",
  missing: "не хватает значения",
  conflict: "конфликт",
  unmapped: "правило не настроено",
};

const CUSTOM_FIELD = "__custom";
const CUSTOM_PROPERTY_KEY = "__custom_property_key";

const EMPTY_RULE_FORM: PropertyRulePayload = {
  property_key: "",
  label: "",
  product_field: "",
  competitor_field: "",
  comparison_mode: "exact",
  severity: "review",
  sort_order: 0,
  is_active: true,
};

const EMPTY_VALUE_MAP_FORM: Omit<PropertyValueMapPayload, "rule_id"> = {
  competitor_source: "",
  competitor_value: "",
  mapped_value: "",
  notes: "",
  is_active: true,
};

function ruleToForm(rule: PropertyRule | null, profileId?: number): PropertyRulePayload {
  if (!rule) {
    return { ...EMPTY_RULE_FORM, profile_id: profileId };
  }
  return {
    profile_id: rule.profile_id,
    property_key: rule.property_key,
    label: rule.label,
    product_field: rule.product_field,
    competitor_field: rule.competitor_field,
    comparison_mode: rule.comparison_mode,
    severity: rule.severity,
    config_json: rule.config_json,
    sort_order: rule.sort_order,
    is_active: rule.is_active,
  };
}

function textValue(value: string | null | undefined) {
  return value && value.trim() ? value : "-";
}

function sourceLabel(value: string | null | undefined) {
  return value && value.trim() ? value : "Все конкуренты";
}

function compactSources(values: string[]) {
  if (!values.length) return { label: "-", title: "Словарь не настроен" };
  const label = values.length > 3 ? `${values.slice(0, 3).join(", ")} +${values.length - 3}` : values.join(", ");
  return { label, title: values.join(", ") };
}

function normalizeSearchValue(value: string | null | undefined) {
  return (value || "").trim().toLocaleLowerCase("ru");
}

function optionLabel(options: { value: string; label: string }[], value: string) {
  return options.find((item) => item.value === value)?.label;
}

function fieldLabel(value: string | null | undefined) {
  if (!value) return "-";
  return optionLabel(FIELD_OPTIONS, value) || value;
}

function modeLabel(value: string | null | undefined) {
  if (!value) return "-";
  return optionLabel(COMPARISON_MODES, value) || value;
}

function severityLabel(value: string | null | undefined) {
  if (!value) return "-";
  return optionLabel(SEVERITIES, value) || value;
}

function valueModeLabel(rule: PropertyRule) {
  if (rule.comparison_mode === "mapped_value") {
    return null;
  }
  if (rule.comparison_mode === "set_overlap" && rule.product_field === "compatibility.model") {
    return "по совместимости";
  }
  if (rule.comparison_mode === "set_overlap") {
    return "пересечение";
  }
  if (rule.comparison_mode === "exact") {
    return "точное поле";
  }
  if (rule.comparison_mode === "boolean") {
    return "да / нет";
  }
  if (rule.comparison_mode === "numeric_tolerance") {
    return "допуск";
  }
  return "без словаря";
}

function supportsDictionary(rule: PropertyRule | null) {
  return rule?.comparison_mode === "mapped_value";
}

function supportsValuePreview(rule: PropertyRule | null) {
  return Boolean(rule && (rule.comparison_mode === "mapped_value" || rule.competitor_field === "compatibility.model"));
}

interface LocalizedSelectProps {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  customValue: string;
  customLabel: string;
  onChange: (value: string) => void;
  required?: boolean;
}

function LocalizedSelect({
  label,
  value,
  options,
  customValue,
  customLabel,
  onChange,
  required,
}: LocalizedSelectProps) {
  const isKnownValue = Boolean(optionLabel(options, value));
  const isCustomValue = value === customValue || (!isKnownValue && Boolean(value));
  return (
    <label>
      <span>{label}</span>
      <select
        className="app__select"
        value={isKnownValue || !isCustomValue ? value : customValue}
        onChange={(event) => onChange(event.target.value)}
        required={required}
      >
        <option value="">Выберите значение</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
        <option value={customValue}>{customLabel}</option>
      </select>
      {isCustomValue && (
        <input
          className="app__search"
          value={value === customValue ? "" : value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="Техническое поле"
          required={required}
        />
      )}
    </label>
  );
}

export function PropertyMappingSettings({ open, onClose, onOpenCompatibility }: PropertyMappingSettingsProps) {
  const queryClient = useQueryClient();
  const [selectedProfileCode, setSelectedProfileCode] = useState<string>("");
  const [selectedRuleId, setSelectedRuleId] = useState<number | null>(null);
  const [selectedValueMapId, setSelectedValueMapId] = useState<number | null>(null);
  const [editorTab, setEditorTab] = useState<EditorTab>("rule");
  const [valuePanelMode, setValuePanelMode] = useState<ValuePanelMode>("dictionary");
  const [valueMapSourceFilter, setValueMapSourceFilter] = useState("");
  const [valueMapSearch, setValueMapSearch] = useState("");
  const [ruleForm, setRuleForm] = useState<PropertyRulePayload>(EMPTY_RULE_FORM);
  const [valueMapForm, setValueMapForm] = useState<Omit<PropertyValueMapPayload, "rule_id">>(EMPTY_VALUE_MAP_FORM);
  const [testProductId, setTestProductId] = useState("");
  const [testCompetitorItemId, setTestCompetitorItemId] = useState("");
  const [testResult, setTestResult] = useState<PropertyComparisonResponse | null>(null);
  const [isTesting, setIsTesting] = useState(false);

  const { data: profilesData, isLoading: isProfilesLoading } = useQuery({
    queryKey: ["property-profiles"],
    queryFn: fetchPropertyProfiles,
    enabled: open,
  });

  const profiles = useMemo(() => profilesData ?? [], [profilesData]);
  const selectedProfile = profiles.find((profile) => profile.code === selectedProfileCode) ?? profiles[0] ?? null;

  const { data: rulesData, isLoading: isRulesLoading } = useQuery({
    queryKey: ["property-rules", selectedProfile?.code],
    queryFn: () => fetchPropertyRules({ profile_code: selectedProfile!.code }),
    enabled: open && Boolean(selectedProfile?.code),
  });

  const { data: valueMapsData, isLoading: isValueMapsLoading } = useQuery({
    queryKey: ["property-value-maps", selectedProfile?.code],
    queryFn: () => fetchPropertyValueMaps({ profile_code: selectedProfile!.code }),
    enabled: open && Boolean(selectedProfile?.code),
  });

  const rules = useMemo(() => rulesData ?? [], [rulesData]);
  const selectedRule = rules.find((rule) => rule.id === selectedRuleId) ?? null;
  const valueMaps = useMemo(() => valueMapsData ?? [], [valueMapsData]);
  const { data: valueSuggestionsData, isLoading: isValueSuggestionsLoading } = useQuery({
    queryKey: ["property-value-suggestions", selectedProfile?.code, selectedRule?.id, valuePanelMode],
    queryFn: () =>
      fetchPropertyValueSuggestions({
        profile_code: selectedProfile!.code,
        rule_id: supportsValuePreview(selectedRule) ? selectedRule!.id : undefined,
        limit: 100,
      }),
    enabled:
      open &&
      editorTab === "values" &&
      (valuePanelMode === "suggestions" || selectedRule?.competitor_field === "compatibility.model") &&
      Boolean(selectedProfile?.code),
  });
  const valueSuggestions = useMemo(() => valueSuggestionsData ?? [], [valueSuggestionsData]);
  const valueCountByRule = useMemo(() => {
    const counts = new Map<number, number>();
    valueMaps.forEach((item) => {
      if (item.is_active) {
        counts.set(item.rule_id, (counts.get(item.rule_id) ?? 0) + 1);
      }
    });
    return counts;
  }, [valueMaps]);
  const valueRules = useMemo(() => rules.filter((rule) => supportsValuePreview(rule)), [rules]);
  const selectedRuleValueMaps = valueMaps.filter((item) => item.rule_id === selectedRule?.id);
  const firstRuleWithValueMaps = useMemo(
    () =>
      valueRules.find((rule) => valueMaps.some((item) => item.rule_id === rule.id)) ??
      valueRules[0] ??
      null,
    [valueRules, valueMaps]
  );
  const selectedValueMap = selectedRuleValueMaps.find((item) => item.id === selectedValueMapId) ?? null;
  const valueMapSources = useMemo(
    () =>
      Array.from(
        new Set(
          selectedRuleValueMaps
            .map((item) => item.competitor_source?.trim())
            .filter((value): value is string => Boolean(value))
        )
      ).sort((left, right) => left.localeCompare(right, "ru")),
    [selectedRuleValueMaps]
  );
  const visibleValueMaps = selectedRuleValueMaps.filter((item) => {
    if (valueMapSourceFilter && item.competitor_source !== valueMapSourceFilter) {
      return false;
    }
    const search = normalizeSearchValue(valueMapSearch);
    if (!search) {
      return true;
    }
    return [item.competitor_source, item.competitor_value, item.mapped_value, item.notes]
      .map(normalizeSearchValue)
      .some((value) => value.includes(search));
  });
  const visibleValueSuggestions = valueSuggestions.filter((item) => {
    if (supportsValuePreview(selectedRule) && item.rule_id !== selectedRule?.id) {
      return false;
    }
    const search = normalizeSearchValue(valueMapSearch);
    if (!search) {
      return true;
    }
    return [item.competitor_source, item.competitor_value, item.sample_name]
      .map(normalizeSearchValue)
      .some((value) => value.includes(search));
  });
  const safeValueSuggestionsCount = visibleValueSuggestions.filter((item) => item.safe_auto && item.suggested_mapped_value).length;
  const ruleCompetitors = useMemo(() => {
    const competitorsByRule = new Map<number, { label: string; title: string }>();
    rules.forEach((rule) => {
      const mapsForRule = valueMaps.filter((item) => item.rule_id === rule.id);
      const hasGlobalMap = mapsForRule.some((item) => !item.competitor_source?.trim());
      const sources = Array.from(
        new Set(
          mapsForRule
            .map((item) => item.competitor_source?.trim())
            .filter((value): value is string => Boolean(value))
        )
      ).sort((left, right) => left.localeCompare(right, "ru"));
      competitorsByRule.set(rule.id, compactSources(hasGlobalMap ? ["Все конкуренты", ...sources] : sources));
    });
    return competitorsByRule;
  }, [rules, valueMaps]);

  useEffect(() => {
    if (!open || selectedProfileCode || !profiles.length) return;
    setSelectedProfileCode(profiles[0].code);
  }, [open, profiles, selectedProfileCode]);

  useEffect(() => {
    if (!rules.length) {
      setSelectedRuleId(null);
      return;
    }
    if (!selectedRuleId || !rules.some((rule) => rule.id === selectedRuleId)) {
      setSelectedRuleId(rules[0].id);
    }
  }, [rules, selectedRuleId]);

  useEffect(() => {
    setRuleForm(ruleToForm(selectedRule, selectedProfile?.id));
    setValueMapForm(EMPTY_VALUE_MAP_FORM);
    setSelectedValueMapId(null);
    setValueMapSourceFilter("");
    setValueMapSearch("");
    setTestResult(null);
  }, [selectedProfile?.id, selectedRule]);

  useEffect(() => {
    if (!selectedValueMap) {
      return;
    }
    setValueMapForm({
      competitor_source: selectedValueMap.competitor_source ?? "",
      competitor_value: selectedValueMap.competitor_value,
      mapped_value: selectedValueMap.mapped_value,
      notes: selectedValueMap.notes ?? "",
      is_active: selectedValueMap.is_active,
    });
  }, [selectedValueMap]);

  const openValuesTab = () => {
    if (firstRuleWithValueMaps && !supportsValuePreview(selectedRule)) {
      setSelectedRuleId(firstRuleWithValueMaps.id);
    }
    if (selectedRule?.competitor_field === "compatibility.model") {
      setValuePanelMode("suggestions");
    }
    setEditorTab("values");
  };

  const invalidateRules = () => {
    queryClient.invalidateQueries({ queryKey: ["property-rules"] });
    queryClient.invalidateQueries({ queryKey: ["property-value-maps"] });
    queryClient.invalidateQueries({ queryKey: ["property-value-suggestions"] });
    queryClient.invalidateQueries({ queryKey: ["candidate-search"] });
    queryClient.invalidateQueries({ queryKey: ["property-comparison"] });
  };

  const saveRuleMutation = useMutation({
    mutationFn: () => {
      if (!selectedProfile) {
        throw new Error("Профиль не выбран");
      }
      const payload = { ...ruleForm, profile_id: selectedProfile.id };
      if (selectedRule) {
        return updatePropertyRule(selectedRule.id, payload);
      }
      return createPropertyRule(payload);
    },
    onSuccess: (rule) => {
      setSelectedRuleId(rule.id);
      invalidateRules();
      toast.success("Правило сохранено");
    },
    onError: () => toast.error("Не удалось сохранить правило"),
  });

  const restoreRuleMutation = useMutation({
    mutationFn: () => {
      if (!selectedRule) {
        throw new Error("Правило не выбрано");
      }
      return restorePropertyRuleDefault(selectedRule.id);
    },
    onSuccess: (rule) => {
      setSelectedRuleId(rule.id);
      invalidateRules();
      toast.success("Стандартные настройки восстановлены");
    },
    onError: () => toast.error("Не удалось восстановить правило"),
  });

  const saveValueMapMutation = useMutation({
    mutationFn: () => {
      if (!selectedRule) {
        throw new Error("Правило не выбрано");
      }
      const payload: PropertyValueMapPayload = {
        ...valueMapForm,
        competitor_source: valueMapForm.competitor_source || null,
        notes: valueMapForm.notes || null,
        rule_id: selectedRule.id,
      };
      if (selectedValueMap) {
        return updatePropertyValueMap(selectedValueMap.id, payload);
      }
      return createPropertyValueMap(payload);
    },
    onSuccess: (valueMap) => {
      setSelectedValueMapId(valueMap.id);
      setValueMapForm(EMPTY_VALUE_MAP_FORM);
      invalidateRules();
      toast.success("Сопоставление значений сохранено");
    },
    onError: () => toast.error("Не удалось сохранить сопоставление значений"),
  });

  const toggleValueMapMutation = useMutation({
    mutationFn: (item: PropertyValueMap) => updatePropertyValueMap(item.id, { is_active: !item.is_active }),
    onSuccess: () => {
      invalidateRules();
      toast.success("Словарь обновлен");
    },
    onError: () => toast.error("Не удалось обновить словарь"),
  });

  const acceptSafeSuggestionsMutation = useMutation({
    mutationFn: () => {
      if (!selectedProfile?.code) {
        throw new Error("Профиль не выбран");
      }
      return acceptSafePropertyValueSuggestions({
        profile_code: selectedProfile.code,
        rule_id: supportsValuePreview(selectedRule) ? selectedRule?.id : undefined,
        limit: 500,
      });
    },
    onSuccess: (result) => {
      invalidateRules();
      toast.success(`Автосмаплено: ${result.created_count}`);
    },
    onError: () => toast.error("Не удалось принять безопасные значения"),
  });

  const addSuggestionToForm = (suggestion: PropertyValueSuggestion) => {
    const targetRule = rules.find((rule) => rule.id === suggestion.rule_id);
    if (targetRule) {
      setSelectedRuleId(targetRule.id);
    }
    setSelectedValueMapId(null);
    setValuePanelMode("dictionary");
    setValueMapForm({
      competitor_source: suggestion.competitor_source ?? "",
      competitor_value: suggestion.competitor_value,
      mapped_value: suggestion.suggested_mapped_value ?? "",
      notes: suggestion.sample_name ? `Пример: ${suggestion.sample_name}` : "",
      is_active: true,
    });
  };

  const runTest = async () => {
    const productId = Number(testProductId);
    const competitorItemId = Number(testCompetitorItemId);
    if (!Number.isInteger(productId) || !Number.isInteger(competitorItemId) || productId <= 0 || competitorItemId <= 0) {
      toast.error("Укажите ID товара и кандидата");
      return;
    }
    setIsTesting(true);
    try {
      const result = await fetchPropertyComparison(productId, competitorItemId, selectedProfile?.code);
      setTestResult(result);
    } catch {
      toast.error("Не удалось проверить пару");
    } finally {
      setIsTesting(false);
    }
  };

  if (!open) return null;

  return (
    <div className="settings-overlay" role="dialog" aria-modal="true">
      <div className="settings-shell">
        <header className="settings-shell__topbar">
          <div>
            <div className="picker__eyebrow">Bitrix Matching</div>
            <h2>Настройки свойств</h2>
          </div>
          <button className="btn btn--ghost" onClick={onClose}>
            Закрыть
          </button>
        </header>

        <aside className="settings-profiles">
          <div className="picker__section-title">Профили</div>
          {isProfilesLoading && <div className="panel__loading">Загрузка...</div>}
          {profiles.map((profile) => (
            <button
              key={profile.id}
              className={`settings-profile ${profile.code === selectedProfile?.code ? "settings-profile--active" : ""}`}
              onClick={() => {
                setSelectedProfileCode(profile.code);
                setSelectedRuleId(null);
              }}
            >
              <strong>{profile.name}</strong>
              <span>{profile.code}</span>
            </button>
          ))}
        </aside>

        <main className="settings-rules">
          <div className="settings-panel__header">
            <div className="picker__section-title">Правила</div>
            <button
              className="btn btn--ghost btn--compact"
              onClick={() => {
                setSelectedRuleId(null);
                setRuleForm(ruleToForm(null, selectedProfile?.id));
                setEditorTab("rule");
              }}
              disabled={!selectedProfile}
            >
              Новое правило
            </button>
          </div>
          <div className="settings-rule-table">
            <div className="settings-rule-table__head">
              <span>Характеристика</span>
              <span>Значений</span>
              <span>Конкурент</span>
              <span>Поля</span>
              <span>Режим</span>
              <span>Строгость</span>
            </div>
            {isRulesLoading && <div className="panel__loading">Загрузка...</div>}
            {!isRulesLoading &&
              rules.map((rule) => (
                <button
                  key={rule.id}
                  className={`settings-rule-row ${rule.id === selectedRule?.id ? "settings-rule-row--active" : ""}`}
                  onClick={() => setSelectedRuleId(rule.id)}
                >
                  <strong>{rule.label}</strong>
                  <span className={rule.comparison_mode === "mapped_value" ? "settings-count" : "settings-count settings-count--muted"}>
                    {rule.comparison_mode === "mapped_value" ? valueCountByRule.get(rule.id) ?? 0 : valueModeLabel(rule)}
                  </span>
                  <span title={ruleCompetitors.get(rule.id)?.title}>{ruleCompetitors.get(rule.id)?.label}</span>
                  <span className="settings-field-pair" title={`${rule.product_field} / ${rule.competitor_field}`}>
                    <em>Наше: {fieldLabel(rule.product_field)}</em>
                    <em>Конкурент: {fieldLabel(rule.competitor_field)}</em>
                  </span>
                  <span>{modeLabel(rule.comparison_mode)}</span>
                  <span>
                    {severityLabel(rule.severity)}
                    {rule.has_default_drift ? <em className="settings-drift-dot">изменено</em> : null}
                  </span>
                </button>
              ))}
            {!isRulesLoading && !rules.length && <div className="panel__empty">Правил пока нет</div>}
          </div>
        </main>

        <aside className="settings-editor">
          <div className="settings-editor-tabs" role="tablist" aria-label="Редактор настроек свойств">
            <button
              className={`settings-editor-tab ${editorTab === "rule" ? "settings-editor-tab--active" : ""}`}
              onClick={() => setEditorTab("rule")}
              type="button"
            >
              Правило
            </button>
            <button
              className={`settings-editor-tab ${editorTab === "values" ? "settings-editor-tab--active" : ""}`}
              onClick={openValuesTab}
              disabled={!selectedRule && !firstRuleWithValueMaps}
              type="button"
            >
              Значения
              <span>{Array.from(valueCountByRule.values()).reduce((sum, count) => sum + count, 0)}</span>
            </button>
            <button
              className={`settings-editor-tab ${editorTab === "test" ? "settings-editor-tab--active" : ""}`}
              onClick={() => setEditorTab("test")}
              type="button"
            >
              Проверка
            </button>
          </div>

          {editorTab === "rule" && (
            <form
              className="settings-form"
              onSubmit={(event) => {
                event.preventDefault();
                saveRuleMutation.mutate();
              }}
            >
              <div className="settings-panel__header">
                <div className="picker__section-title">{selectedRule ? "Редактор правила" : "Новое правило"}</div>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={Boolean(ruleForm.is_active)}
                    onChange={(event) => setRuleForm((form) => ({ ...form, is_active: event.target.checked }))}
                  />
                  Активно
                </label>
              </div>
              {selectedRule?.has_default_drift && (
                <div className="settings-warning">
                  <span>Правило отличается от стандартной настройки профиля.</span>
                  <button
                    className="btn btn--ghost btn--compact"
                    type="button"
                    onClick={() => restoreRuleMutation.mutate()}
                    disabled={restoreRuleMutation.isPending}
                  >
                    Вернуть стандарт
                  </button>
                </div>
              )}
              <LocalizedSelect
                label="Характеристика"
                value={ruleForm.property_key}
                options={PROPERTY_KEYS}
                customValue={CUSTOM_PROPERTY_KEY}
                customLabel="Другая характеристика"
                onChange={(value) => setRuleForm((form) => ({ ...form, property_key: value }))}
                required
              />
              <label>
                <span>Название</span>
                <input
                  className="app__search"
                  value={ruleForm.label}
                  onChange={(event) => setRuleForm((form) => ({ ...form, label: event.target.value }))}
                  required
                />
              </label>
              <LocalizedSelect
                label="Поле нашего товара"
                value={ruleForm.product_field}
                options={FIELD_OPTIONS}
                customValue={CUSTOM_FIELD}
                customLabel="Другое поле"
                onChange={(value) => setRuleForm((form) => ({ ...form, product_field: value }))}
                required
              />
              <LocalizedSelect
                label="Поле конкурента"
                value={ruleForm.competitor_field}
                options={FIELD_OPTIONS}
                customValue={CUSTOM_FIELD}
                customLabel="Другое поле"
                onChange={(value) => setRuleForm((form) => ({ ...form, competitor_field: value }))}
                required
              />
              <div className="settings-form__grid">
                <label>
                  <span>Режим</span>
                  <select
                    className="app__select"
                    value={ruleForm.comparison_mode}
                    onChange={(event) => setRuleForm((form) => ({ ...form, comparison_mode: event.target.value }))}
                  >
                    {COMPARISON_MODES.map((mode) => (
                      <option key={mode.value} value={mode.value}>
                        {mode.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Строгость</span>
                  <select
                    className="app__select"
                    value={ruleForm.severity}
                    onChange={(event) => setRuleForm((form) => ({ ...form, severity: event.target.value }))}
                  >
                    {SEVERITIES.map((severity) => (
                      <option key={severity.value} value={severity.value}>
                        {severity.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Порядок</span>
                  <input
                    className="app__search"
                    type="number"
                    value={ruleForm.sort_order ?? 0}
                    onChange={(event) => setRuleForm((form) => ({ ...form, sort_order: Number(event.target.value) }))}
                  />
                </label>
              </div>
              <button className="btn" type="submit" disabled={!selectedProfile || saveRuleMutation.isPending}>
                Сохранить правило
              </button>
            </form>
          )}

          {editorTab === "values" && (
            <section className="settings-dictionary">
            <div className="settings-panel__header">
              <div>
                <div className="picker__section-title">Сопоставление значений</div>
                <strong>{selectedRule?.label || "Характеристика не выбрана"}</strong>
              </div>
              <button
                className="btn btn--ghost btn--compact"
                onClick={() => {
                  setSelectedValueMapId(null);
                  setValueMapForm(EMPTY_VALUE_MAP_FORM);
                  setEditorTab("values");
                }}
                disabled={!selectedRule || !supportsDictionary(selectedRule)}
                type="button"
              >
                Новое значение
              </button>
            </div>
            {isValueMapsLoading && <div className="panel__loading">Загрузка...</div>}
            {!selectedRule && <div className="panel__empty">Выберите правило</div>}
            {selectedRule && (
              <>
                {valueRules.length > 0 && (
                  <div className="settings-rule-tabs" aria-label="Правила со словарями">
                    {valueRules.map((rule) => (
                      <button
                        key={rule.id}
                        className={`settings-rule-tab ${rule.id === selectedRule.id ? "settings-rule-tab--active" : ""}`}
                        type="button"
                        onClick={() => {
                          setSelectedRuleId(rule.id);
                          setValuePanelMode(supportsDictionary(rule) ? "dictionary" : "suggestions");
                        }}
                      >
                        {rule.label}
                        <span>{supportsDictionary(rule) ? valueCountByRule.get(rule.id) ?? 0 : "модели"}</span>
                      </button>
                    ))}
                  </div>
                )}
                <div className="settings-rule-link">
                  <span>
                    <strong>Наше свойство</strong>
                    {fieldLabel(selectedRule.product_field)}
                  </span>
                  <span>
                    <strong>Свойство конкурента</strong>
                    {fieldLabel(selectedRule.competitor_field)}
                  </span>
                  <span>
                    <strong>Режим</strong>
                    {modeLabel(selectedRule.comparison_mode)}
                  </span>
                </div>
                {!supportsValuePreview(selectedRule) && (
                  <div className="panel__empty">Для этого режима значения не просматриваются</div>
                )}
                {supportsValuePreview(selectedRule) && (
                  <>
                    <div className="settings-value-mode">
                      {supportsDictionary(selectedRule) && (
                        <button
                          className={`settings-source-filter__item ${
                            valuePanelMode === "dictionary" ? "settings-source-filter__item--active" : ""
                          }`}
                          onClick={() => setValuePanelMode("dictionary")}
                          type="button"
                        >
                          Словарь
                        </button>
                      )}
                      <button
                        className={`settings-source-filter__item ${
                          valuePanelMode === "suggestions" || !supportsDictionary(selectedRule)
                            ? "settings-source-filter__item--active"
                            : ""
                        }`}
                        onClick={() => setValuePanelMode("suggestions")}
                        type="button"
                      >
                        {supportsDictionary(selectedRule) ? "Неразобранные" : "Найденные модели"}
                      </button>
                    </div>
                    {supportsDictionary(selectedRule) && valuePanelMode === "dictionary" && (
                      <div className="settings-source-filter">
                        <button
                          className={`settings-source-filter__item ${
                            !valueMapSourceFilter ? "settings-source-filter__item--active" : ""
                          }`}
                          onClick={() => setValueMapSourceFilter("")}
                          type="button"
                        >
                          Все конкуренты
                        </button>
                        {valueMapSources.map((source) => (
                          <button
                            key={source}
                            className={`settings-source-filter__item ${
                              source === valueMapSourceFilter ? "settings-source-filter__item--active" : ""
                            }`}
                            onClick={() => setValueMapSourceFilter(source)}
                            type="button"
                          >
                            {source}
                          </button>
                        ))}
                      </div>
                    )}
                    <input
                      className="app__search"
                      placeholder={
                        supportsDictionary(selectedRule) && valuePanelMode === "dictionary"
                          ? "Поиск по словарю"
                          : "Поиск по найденным значениям"
                      }
                      value={valueMapSearch}
                      onChange={(event) => setValueMapSearch(event.target.value)}
                    />
                  </>
                )}
                {supportsDictionary(selectedRule) && valuePanelMode === "dictionary" && (
                  <div className="settings-value-map-list">
                    <div className="settings-value-map-head">
                      <span>Конкурент</span>
                      <span>Значение у конкурента</span>
                      <span>Значение у нас</span>
                      <span>Статус</span>
                      <span></span>
                    </div>
                    {visibleValueMaps.map((item) => (
                      <div
                        key={item.id}
                        className={`settings-value-map-row ${
                          item.id === selectedValueMap?.id ? "settings-value-map-row--active" : ""
                        }`}
                        onClick={() => setSelectedValueMapId(item.id)}
                        role="button"
                        tabIndex={0}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            setSelectedValueMapId(item.id);
                          }
                        }}
                      >
                        <span>{sourceLabel(item.competitor_source)}</span>
                        <strong>{item.competitor_value}</strong>
                        <span>{item.mapped_value}</span>
                        <span>{item.is_active ? "активно" : "отключено"}</span>
                        <button
                          className="btn btn--ghost btn--compact"
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleValueMapMutation.mutate(item);
                          }}
                          type="button"
                        >
                          {item.is_active ? "Отключить" : "Включить"}
                        </button>
                      </div>
                    ))}
                    {!isValueMapsLoading && !selectedRuleValueMaps.length && (
                      <div className="panel__empty">Сопоставлений пока нет</div>
                    )}
                    {!isValueMapsLoading && Boolean(selectedRuleValueMaps.length) && !visibleValueMaps.length && (
                      <div className="panel__empty">По фильтру ничего не найдено</div>
                    )}
                  </div>
                )}
                {supportsValuePreview(selectedRule) && (valuePanelMode === "suggestions" || !supportsDictionary(selectedRule)) && (
                  <div className="settings-value-map-list">
                    {supportsDictionary(selectedRule) && (
                      <div className="settings-suggestion-toolbar">
                        <span>Безопасных значений: {safeValueSuggestionsCount}</span>
                        <button
                          className="btn btn--ghost btn--compact"
                          type="button"
                          disabled={!safeValueSuggestionsCount || acceptSafeSuggestionsMutation.isPending}
                          onClick={() => acceptSafeSuggestionsMutation.mutate()}
                        >
                          Принять безопасные
                        </button>
                      </div>
                    )}
                    <div className="settings-suggestion-head">
                      <span>Конкурент</span>
                      <span>Значение</span>
                      <span>Мапинг</span>
                      <span>Частота</span>
                      <span>Пример</span>
                      <span></span>
                    </div>
                    {isValueSuggestionsLoading && <div className="panel__loading">Загрузка...</div>}
                    {visibleValueSuggestions.map((item) => (
                      <div key={`${item.rule_id}-${item.competitor_source}-${item.competitor_value}`} className="settings-suggestion-row">
                        <span>{sourceLabel(item.competitor_source)}</span>
                        <strong>{item.competitor_value}</strong>
                        <span className="settings-suggestion-map-cell">
                          {item.suggested_mapped_value || "-"}
                          {item.safe_auto && <span className="compatibility-badge compatibility-badge--ok">авто</span>}
                        </span>
                        <span>{item.count}</span>
                        <span title={item.sample_name || undefined}>
                          #{item.sample_competitor_item_id} {item.sample_name || ""}
                        </span>
                        {supportsDictionary(selectedRule) ? (
                          <button className="btn btn--ghost btn--compact" type="button" onClick={() => addSuggestionToForm(item)}>
                            Добавить
                          </button>
                        ) : selectedRule.competitor_field === "compatibility.model" && onOpenCompatibility ? (
                          <button className="btn btn--ghost btn--compact" type="button" onClick={onOpenCompatibility}>
                            Открыть
                          </button>
                        ) : (
                          <span></span>
                        )}
                      </div>
                    ))}
                    {!isValueSuggestionsLoading && !visibleValueSuggestions.length && (
                      <div className="panel__empty">Найденных значений нет</div>
                    )}
                  </div>
                )}
                {selectedRule?.competitor_field === "compatibility.model" && onOpenCompatibility && (
                  <div className="settings-warning">
                    <span>Совместимость моделей ведется в отдельном справочнике.</span>
                    <button className="btn btn--ghost btn--compact" type="button" onClick={onOpenCompatibility}>
                      Открыть мапинг совместимости
                    </button>
                  </div>
                )}
                {supportsDictionary(selectedRule) && (
                  <div className="settings-value-map-form">
                    <label>
                      <span>Конкурент</span>
                      <input
                        className="app__search"
                        placeholder="Все конкуренты"
                        value={valueMapForm.competitor_source ?? ""}
                        onChange={(event) =>
                          setValueMapForm((form) => ({ ...form, competitor_source: event.target.value }))
                        }
                      />
                    </label>
                    <label>
                      <span>Значение у конкурента</span>
                      <input
                        className="app__search"
                        value={valueMapForm.competitor_value}
                        onChange={(event) =>
                          setValueMapForm((form) => ({ ...form, competitor_value: event.target.value }))
                        }
                      />
                    </label>
                    <label>
                      <span>Значение у нас</span>
                      <input
                        className="app__search"
                        value={valueMapForm.mapped_value}
                        onChange={(event) => setValueMapForm((form) => ({ ...form, mapped_value: event.target.value }))}
                      />
                    </label>
                    <label>
                      <span>Комментарий</span>
                      <input
                        className="app__search"
                        value={valueMapForm.notes ?? ""}
                        onChange={(event) => setValueMapForm((form) => ({ ...form, notes: event.target.value }))}
                      />
                    </label>
                    <button
                      className="btn btn--ghost"
                      onClick={() => saveValueMapMutation.mutate()}
                      disabled={!selectedRule || !valueMapForm.competitor_value || !valueMapForm.mapped_value}
                      type="button"
                    >
                      {selectedValueMap ? "Сохранить" : "Добавить"}
                    </button>
                  </div>
                )}
              </>
            )}
            </section>
          )}

          {editorTab === "test" && (
            <section className="settings-test">
            <div className="picker__section-title">Проверка пары</div>
            <div className="settings-test__controls">
              <input
                className="app__search"
                inputMode="numeric"
                placeholder="ID товара"
                value={testProductId}
                onChange={(event) => setTestProductId(event.target.value)}
              />
              <input
                className="app__search"
                inputMode="numeric"
                placeholder="ID кандидата"
                value={testCompetitorItemId}
                onChange={(event) => setTestCompetitorItemId(event.target.value)}
              />
              <button className="btn btn--ghost" onClick={runTest} disabled={isTesting} type="button">
                Проверить
              </button>
            </div>
            {testResult && (
              <div className="settings-test__result">
                <div className="property-summary">
                  <span>{testResult.profile_name}</span>
                  <strong>{testResult.summary.label}</strong>
                </div>
                {testResult.items.slice(0, 8).map((item) => (
                  <div key={item.property_key} className="settings-test-row">
                    <strong>{item.label}</strong>
                    <span>{textValue(item.product_value)}</span>
                    <span>{textValue(item.competitor_value)}</span>
                    <span className={`badge badge--property-${item.status}`}>
                      {STATUS_TEXT[item.status] || item.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}
