import { describe, expect, it } from "vitest";
import { procurementRiskLabel } from "./procurementRiskLabels";

describe("procurementRiskLabel", () => {
  it("translates lifecycle and catalog codes", () => {
    expect(procurementRiskLabel("working_confirmation_required")).toBe(
      "Переход в «Рабочий» должен подтвердить Омар"
    );
    expect(procurementRiskLabel("catalog_product_missing")).toBe(
      "Товар не найден в каталоге Bitrix по GUID 1С"
    );
  });

  it("does not expose an unknown English code to the user", () => {
    expect(procurementRiskLabel("new_backend_code")).toBe(
      "Требуется дополнительная проверка"
    );
  });
});
