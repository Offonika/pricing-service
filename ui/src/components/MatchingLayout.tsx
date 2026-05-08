import { ProductsGrid } from "./ProductsGrid";
import { CandidatePanel } from "./CandidatePanel";
import type { ProductFacets } from "../api/types";

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

export function MatchingLayout({
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
            page={page}
            pageSize={pageSize}
            onTotalChange={onTotalChange}
            onFacetsChange={onFacetsChange}
          />
        </div>
      </div>
      <CandidatePanel />
    </>
  );
}
