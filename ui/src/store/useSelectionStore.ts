import { create } from "zustand";
import type { ProductRow } from "../api/types";

interface SelectionState {
  selectedProductId: number | null;
  selectedProductName: string | null;
  selectedProductArticle: string | null;
  selectedProduct: ProductRow | null;
  isPickerOpen: boolean;
  setSelectedProduct: (product: ProductRow | null) => void;
  openPicker: (product?: ProductRow | null) => void;
  closePicker: () => void;
}

export const useSelectedProduct = create<SelectionState>((set) => ({
  selectedProductId: null,
  selectedProductName: null,
  selectedProductArticle: null,
  selectedProduct: null,
  isPickerOpen: false,
  setSelectedProduct: (product) =>
    set({
      selectedProductId: product?.id ?? null,
      selectedProductName: product?.name ?? null,
      selectedProductArticle: product?.article ?? null,
      selectedProduct: product ?? null,
    }),
  openPicker: (product) =>
    set((state) => ({
      selectedProductId: product?.id ?? state.selectedProductId,
      selectedProductName: product?.name ?? state.selectedProductName,
      selectedProductArticle: product?.article ?? state.selectedProductArticle,
      selectedProduct: product ?? state.selectedProduct,
      isPickerOpen: true,
    })),
  closePicker: () => set({ isPickerOpen: false }),
}));
