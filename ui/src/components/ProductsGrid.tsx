import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchProducts } from "../api/matching";
import type { ProductFacets } from "../api/types";
import { useSelectedProduct } from "../store/useSelectionStore";

interface Props {
  search: string;
  status: string;
  category: string;
  compatibilityBrand: string;
  subject: string;
  page: number;
  pageSize: number;
  onTotalChange?: (total: number) => void;
  onFacetsChange?: (facets: ProductFacets | null) => void;
}

const STATUS_LABEL: Record<string, string> = {
  none: "Нет пары",
  live_candidates: "Есть в live",
  candidates: "Есть кандидаты",
  auto: "Сопоставлен авто",
  manual: "Сопоставлен вручную",
  matched: "Сопоставлен",
  ambiguous: "Неоднозначно",
  uncertain: "На проверку",
  multiple: "Несколько связей",
};

export function ProductsGrid({
  search,
  status,
  category,
  compatibilityBrand,
  subject,
  page,
  pageSize,
  onTotalChange,
  onFacetsChange,
}: Props) {
  const { selectedProductId, setSelectedProduct, openPicker } = useSelectedProduct();
  const hasProductFilters = Boolean(search || status || category || compatibilityBrand || subject);
  const includeLiveCounts = !hasProductFilters;
  const { data, isError, isLoading } = useQuery({
    queryKey: ["products", search, status, category, compatibilityBrand, subject, includeLiveCounts, page, pageSize],
    queryFn: () =>
      fetchProducts({
        page,
        page_size: pageSize,
        search: search || undefined,
        status: status || undefined,
        category: category || undefined,
        compatibility_brand: compatibilityBrand || undefined,
        subject: subject || undefined,
        include_live_counts: includeLiveCounts,
      }),
    refetchOnWindowFocus: false,
    staleTime: 15_000,
  });

  useEffect(() => {
    if (data && onTotalChange) {
      onTotalChange(data.total);
    }
  }, [data, onTotalChange]);

  useEffect(() => {
    if (onFacetsChange) {
      onFacetsChange(data?.facets ?? null);
    }
  }, [data?.facets, onFacetsChange]);

  useEffect(() => {
    if (!data?.items?.length) {
      setSelectedProduct(null);
      return;
    }
    const exists = data.items.some((p) => p.id === selectedProductId);
    if (!exists) {
      setSelectedProduct(data.items[0]);
    }
  }, [data, selectedProductId, setSelectedProduct]);

  const products = data?.items ?? [];

  return (
    <div className="products-grid">
      <div className="products-table">
        <div className="products-table__head">
          <span>Артикул</span>
          <span>Наш товар</span>
          <span>Бренд</span>
          <span>Категория</span>
          <span>Предмет</span>
          <span>Статус</span>
          <span>Текущий матч</span>
          <span>Кандидаты</span>
          <span>Связи</span>
          <span />
        </div>

        {isLoading && <div className="products-table__state">Загрузка товаров...</div>}
        {isError && <div className="products-table__state products-table__state--error">Не удалось загрузить товары из API</div>}
        {!isLoading && !isError && !products.length && <div className="products-table__state">Товары не найдены</div>}

        {!isLoading &&
          !isError &&
          products.map((product) => (
            <div
              key={product.id}
              className={`products-table__row ${product.id === selectedProductId ? "products-table__row--selected" : ""}`}
              role="button"
              tabIndex={0}
              onClick={() => setSelectedProduct(product)}
              onDoubleClick={() => openPicker(product)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  openPicker(product);
                }
              }}
            >
              <span className="mono">{product.article || "-"}</span>
              <strong>{product.name}</strong>
              <span>{product.brand || "-"}</span>
              <span>{product.category || "-"}</span>
              <span>{product.subject || "-"}</span>
              <span>
                <span className={`badge badge--${product.status ?? "none"}`}>
                  {STATUS_LABEL[product.status ?? "none"] ?? "-"}
                </span>
              </span>
              <span>{product.current_match?.name || product.current_match?.competitor_name || "-"}</span>
              <span className="candidate-preview-list">
                {product.candidate_previews?.length ? (
                  product.candidate_previews.map((candidate) => (
                    <span
                      key={`${candidate.competitor_item_id ?? candidate.sku}-${candidate.status}`}
                      className={`candidate-preview candidate-preview--${candidate.status || "suggested"}`}
                    >
                      <strong>{candidate.competitor_name || "-"}</strong>
                      <span>{candidate.sku || candidate.name || "-"}</span>
                      {candidate.price != null && <em>{candidate.price}</em>}
                    </span>
                  ))
                ) : product.live_candidate_count ? (
                  <span className="candidate-preview candidate-preview--live">
                    <strong>Live</strong>
                    <span>Есть в живом поиске</span>
                    <em>{product.live_candidate_count}</em>
                  </span>
                ) : (
                  "-"
                )}
              </span>
              <span>
                {product.accepted_count ?? 0}/{product.suggested_count ?? 0}/{product.review_count ?? 0}
              </span>
              <span>
                <button
                  className="btn btn--compact"
                  onClick={(event) => {
                    event.stopPropagation();
                    openPicker(product);
                  }}
                >
                  Подобрать
                </button>
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}
