import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  fetchDisplayFamilies,
  fetchDisplayFamily,
  fetchDisplayFamilyRegistrySummary,
  fetchDisplayFamilyRegistryVersions,
} from "../api/matching";
import type { DisplayFamilyRow } from "../api/types";

type FamilyShapeFilter = "all" | "multi" | "singleton";

function formatDate(value?: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("ru-RU");
}

function familyLabel(family: DisplayFamilyRow) {
  return family.phone_models.join(", ") || family.family_key;
}

function shortChecksum(value?: string | null) {
  if (!value) return "—";
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

export function DisplayFamilyRegistryPanel() {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [shapeFilter, setShapeFilter] = useState<FamilyShapeFilter>("all");
  const [hasWarnings, setHasWarnings] = useState(false);
  const [needsReview, setNeedsReview] = useState(false);
  const [matchingReview, setMatchingReview] = useState(false);
  const [qualityUnknown, setQualityUnknown] = useState(false);
  const [selectedFamilyId, setSelectedFamilyId] = useState<number | null>(null);

  const { data: summary, isLoading: isSummaryLoading } = useQuery({
    queryKey: ["display-family-registry-summary"],
    queryFn: fetchDisplayFamilyRegistrySummary,
  });
  const { data: versions } = useQuery({
    queryKey: ["display-family-registry-versions"],
    queryFn: () => fetchDisplayFamilyRegistryVersions({ limit: 10 }),
  });
  const familyQuery = useMemo(
    () => ({
      page,
      page_size: 50,
      search: search.trim() || undefined,
      singleton: shapeFilter === "all" ? undefined : shapeFilter === "singleton",
      has_warnings: hasWarnings || undefined,
      needs_review: needsReview || undefined,
      matching_review: matchingReview || undefined,
      quality_unknown: qualityUnknown || undefined,
    }),
    [hasWarnings, matchingReview, needsReview, page, qualityUnknown, search, shapeFilter]
  );
  const { data: families, isLoading: isFamiliesLoading } = useQuery({
    queryKey: ["display-families", familyQuery],
    queryFn: () => fetchDisplayFamilies(familyQuery),
    enabled: Boolean(summary?.active_version),
  });
  const activeFamilyId = selectedFamilyId ?? families?.items[0]?.id ?? null;
  const { data: selectedFamily, isLoading: isFamilyLoading } = useQuery({
    queryKey: ["display-family", activeFamilyId],
    queryFn: () => fetchDisplayFamily(activeFamilyId as number),
    enabled: activeFamilyId !== null,
  });

  const totalPages = Math.max(1, Math.ceil((families?.total ?? 0) / 50));
  const activeVersion = summary?.active_version;

  return (
    <div className="display-family-registry">
      <aside className="display-family-registry__summary">
        <div className="picker__section-title">Активный реестр</div>
        {isSummaryLoading && <div className="panel__loading">Загрузка...</div>}
        {!isSummaryLoading && !activeVersion && (
          <div className="panel__empty">Активная версия ещё не загружена.</div>
        )}
        {activeVersion && (
          <>
            <div className="display-family-registry__readonly">Только чтение</div>
            <strong>Версия {activeVersion.version_number}</strong>
            <span>Действует с {formatDate(activeVersion.effective_from)}</span>
            <code title={activeVersion.inventory_checksum}>
              {shortChecksum(activeVersion.inventory_checksum)}
            </code>
            <dl className="display-family-registry__metrics">
              <div><dt>Семьи</dt><dd>{summary.family_count}</dd></div>
              <div><dt>SKU</dt><dd>{summary.member_count}</dd></div>
              <div><dt>Несколько SKU</dt><dd>{summary.multi_sku_family_count}</dd></div>
              <div><dt>Singleton</dt><dd>{summary.singleton_family_count}</dd></div>
              <div><dt>Конфликты с конкурентами</dt><dd>{summary.matching_review_member_count}</dd></div>
              <div><dt>Качество неизвестно</dt><dd>{summary.quality_unknown_member_count}</dd></div>
            </dl>
            <small>
              Реестр не изменяет Matching, заказы или 1С. Предупреждения сохранены как evidence принятого preflight.
            </small>
          </>
        )}

        <div className="picker__section-title display-family-registry__filter-title">Фильтры</div>
        <select
          className="app__select"
          value={shapeFilter}
          onChange={(event) => {
            setShapeFilter(event.target.value as FamilyShapeFilter);
            setPage(1);
            setSelectedFamilyId(null);
          }}
        >
          <option value="all">Все семьи</option>
          <option value="multi">Только несколько SKU</option>
          <option value="singleton">Только singleton</option>
        </select>
        {[
          ["С предупреждениями", hasWarnings, setHasWarnings],
          ["Нужна ручная проверка", needsReview, setNeedsReview],
          ["Есть конфликт с конкурентом", matchingReview, setMatchingReview],
          ["Неизвестно качество", qualityUnknown, setQualityUnknown],
        ].map(([label, checked, setter]) => (
          <label className="compatibility-toggle" key={label as string}>
            <input
              type="checkbox"
              checked={checked as boolean}
              onChange={(event) => {
                (setter as (value: boolean) => void)(event.target.checked);
                setPage(1);
                setSelectedFamilyId(null);
              }}
            />
            <span>{label as string}</span>
          </label>
        ))}

        {versions && versions.length > 0 && (
          <details className="compatibility-details">
            <summary>История версий ({versions.length})</summary>
            <div className="display-family-registry__versions">
              {versions.map((version) => (
                <span key={version.id}>
                  v{version.version_number} · {version.status} · {formatDate(version.effective_from)}
                </span>
              ))}
            </div>
          </details>
        )}
      </aside>

      <section className="display-family-registry__list">
        <div className="compatibility-filters">
          <input
            className="app__search"
            placeholder="Код, название, модель или ID семьи"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setPage(1);
              setSelectedFamilyId(null);
            }}
          />
          <span className="compatibility-badge">Найдено: {families?.total ?? 0}</span>
        </div>
        <div className="settings-value-map-list compatibility-list display-family-registry__rows">
          {isFamiliesLoading && <div className="panel__loading">Загрузка...</div>}
          {families?.items.map((family) => (
            <button
              className={`display-family-row ${activeFamilyId === family.id ? "display-family-row--active" : ""}`}
              key={family.id}
              type="button"
              onClick={() => setSelectedFamilyId(family.id)}
            >
              <span className="display-family-row__title">
                <strong>{familyLabel(family)}</strong>
                <code>{family.family_key}</code>
              </span>
              <span className="display-family-row__badges">
                <span className="compatibility-badge compatibility-badge--strong">{family.member_count} SKU</span>
                <span className="compatibility-badge">остаток {family.total_current_stock_qty}</span>
                {family.is_singleton && <span className="compatibility-badge compatibility-badge--muted">singleton</span>}
                {family.matching_review_member_count > 0 && (
                  <span className="compatibility-badge compatibility-badge--warn">
                    конкуренты {family.matching_review_member_count}
                  </span>
                )}
                {family.quality_unknown_member_count > 0 && (
                  <span className="compatibility-badge compatibility-badge--warn">
                    качество ? {family.quality_unknown_member_count}
                  </span>
                )}
              </span>
              <small>{family.segment_ids.join(" · ")}</small>
            </button>
          ))}
          {!isFamiliesLoading && activeVersion && !families?.items.length && (
            <div className="panel__empty">Семьи по фильтрам не найдены.</div>
          )}
        </div>
        <div className="display-family-registry__pagination">
          <button className="btn btn--ghost btn--compact" type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            Назад
          </button>
          <span>{page} / {totalPages}</span>
          <button className="btn btn--ghost btn--compact" type="button" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
            Дальше
          </button>
        </div>
      </section>

      <aside className="display-family-registry__detail">
        <div className="picker__section-title">Состав и доказательства</div>
        {isFamilyLoading && <div className="panel__loading">Загрузка...</div>}
        {!activeFamilyId && <div className="panel__empty">Выберите семью.</div>}
        {selectedFamily && (
          <>
            <strong>{familyLabel(selectedFamily)}</strong>
            <code>{selectedFamily.family_key}</code>
            <div className="display-family-registry__detail-summary">
              <span>{selectedFamily.member_count} SKU</span>
              <span>{selectedFamily.segment_ids.length} сегментов</span>
              <span>остаток {selectedFamily.total_current_stock_qty}</span>
            </div>
            {selectedFamily.warning_codes.length > 0 && (
              <details className="compatibility-details">
                <summary>Предупреждения ({selectedFamily.warning_codes.length})</summary>
                <div className="compatibility-chip-list">
                  {selectedFamily.warning_codes.map((warning) => (
                    <span className="compatibility-badge compatibility-badge--warn" key={warning}>{warning}</span>
                  ))}
                </div>
              </details>
            )}
            <div className="display-family-registry__members">
              {selectedFamily.members.map((member) => (
                <article className="display-family-member" key={member.id}>
                  <strong>{member.product.name || `Товар ${member.product_id}`}</strong>
                  <span>{member.product.article || "—"} · {member.product.nomenclature_code || "—"}</span>
                  <span>{member.segment_id}</span>
                  <span>Остаток: {member.current_stock_qty} · последняя продажа: {formatDate(member.product.last_sale_at as string | null)}</span>
                  <div className="display-family-row__badges">
                    <span className="compatibility-badge">{member.proposal_status}</span>
                    {member.matching_evidence.accepted_count ? (
                      <span className={`compatibility-badge ${member.matching_evidence.requires_review ? "compatibility-badge--warn" : "compatibility-badge--ok"}`}>
                        конкуренты {member.matching_evidence.accepted_count}
                      </span>
                    ) : null}
                    {member.matching_evidence.requires_review && (
                      <span className="compatibility-badge compatibility-badge--warn">
                        Сопоставление нужно проверить
                      </span>
                    )}
                    {member.matching_review_confirmed && (
                      <span className="compatibility-badge compatibility-badge--ok">
                        Проверено {member.matching_review_confirmed_by || "закупщиком"}
                        {member.matching_review_confirmed_at
                          ? ` · ${formatDate(member.matching_review_confirmed_at)}`
                          : ""}
                      </span>
                    )}
                    {member.requires_manual_review && <span className="compatibility-badge compatibility-badge--warn">evidence сохранён</span>}
                  </div>
                  {(member.matching_evidence.matches?.length || 0) > 0 && (
                    <details className="compatibility-details">
                      <summary>
                        Сопоставления конкурентов ({member.matching_evidence.matches?.length})
                      </summary>
                      <div className="display-family-registry__versions">
                        {member.matching_evidence.matches?.map((match, index) => {
                          const disagreements = match.property_disagreements || [];
                          return (
                            <span key={`${match.competitor || "competitor"}-${match.competitor_item_id || index}`}>
                              <strong>{match.competitor || "Источник"}</strong>
                              {` · ${match.competitor_name || `позиция ${match.competitor_item_id || "—"}`}`}
                              {match.method ? ` · ${match.method}` : ""}
                              {match.model_relation ? ` · модели: ${match.model_relation}` : ""}
                              {disagreements.length > 0
                                ? ` · расхождения: ${disagreements.map((item) => item.field || "свойство").join(", ")}`
                                : ""}
                            </span>
                          );
                        })}
                      </div>
                    </details>
                  )}
                  <small>{member.scope_reasons.join(" · ")}</small>
                </article>
              ))}
            </div>
            {selectedFamily.events.length > 0 && (
              <details className="compatibility-details">
                <summary>Журнал решений</summary>
                <div className="display-family-registry__versions">
                  {selectedFamily.events.map((event) => (
                    <span key={event.id}>
                      {event.action} · {event.reason} · {event.actor} · {formatDate(event.created_at)}
                    </span>
                  ))}
                </div>
              </details>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
