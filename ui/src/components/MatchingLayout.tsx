import { ProductsGrid } from "./ProductsGrid";
import { CandidatePanel } from "./CandidatePanel";
import type { ProductFacets, ProductRow, ProductSort } from "../api/types";

interface Props {
  search: string;
  status: string;
  category: string;
  compatibilityBrand: string;
  subject: string;
  sort: ProductSort;
  page: number;
  pageSize: number;
  onTotalChange?: (total: number) => void;
  onFacetsChange?: (facets: ProductFacets | null) => void;
  onProductRowsChange?: (items: ProductRow[], page: number) => void;
  onNextProduct?: () => void;
  onAfterDecision?: () => void;
}

export function MatchingLayout({
  search,
  status,
  category,
  compatibilityBrand,
  subject,
  sort,
  page,
  pageSize,
  onTotalChange,
  onFacetsChange,
  onProductRowsChange,
  onNextProduct,
  onAfterDecision,
}: Props) {
  return (
    <>
      <div className="layout layout--single">
        <div className="layout__left">
          <ProductsGrid
            search={search}
            status={status}
            category={category}
            compatibilityBrand={compatibilityBrand}
            subject={subject}
            sort={sort}
            page={page}
            pageSize={pageSize}
            onTotalChange={onTotalChange}
            onFacetsChange={onFacetsChange}
            onProductRowsChange={onProductRowsChange}
          />
        </div>
      </div>
      <CandidatePanel onNextProduct={onNextProduct} onAfterDecision={onAfterDecision} />
    </>
  );
}
