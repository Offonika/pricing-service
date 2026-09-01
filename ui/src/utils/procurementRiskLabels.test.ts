import { describe, expect, it } from "vitest";
import {
  groupProcurementBlockers,
  groupProcurementRiskCodes,
  procurementBlockerText,
  procurementRiskLabel,
} from "./procurementRiskLabels";

describe("procurementRiskLabel", () => {
  it("translates lifecycle and catalog codes", () => {
    expect(procurementRiskLabel("working_confirmation_required")).toBe(
      "Переход в «Поддерживаем (Рабочий)» должен подтвердить Омар"
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

  it("understands the line prefix that order blockers carry", () => {
    expect(procurementRiskLabel("line_13:defect_rate_suspected")).toBe(
      "Автозаказ остановлен: высокий процент брака"
    );
  });

  it("translates the legacy display-family blocker from saved projects", () => {
    expect(procurementRiskLabel("line_2:display_family_recommendation_review_required")).toBe(
      "Требуется проверить и подтвердить распределение заказа внутри семейства дисплеев"
    );
  });

  it("names the status that blocks the purchase", () => {
    expect(procurementRiskLabel("classification_blocks_order:pension")).toBe(
      "Статус «Допродаём (Пенсия)» запрещает закупку"
    );
  });
});

describe("groupProcurementBlockers", () => {
  it("collapses the same reason from several lines into one row", () => {
    const groups = groupProcurementBlockers([
      "line_13:defect_rate_suspected",
      "line_17:batch_error_suspected",
      "line_19:defect_rate_suspected",
      "line_22:defect_rate_suspected",
    ]);

    expect(groups.map(procurementBlockerText)).toEqual([
      "Автозаказ остановлен: высокий процент брака — строки 13, 19, 22",
      "Подозрение на партийную ошибку (пересорт) — строка 17",
    ]);
  });

  it("keeps order-level blockers without line numbers", () => {
    const groups = groupProcurementBlockers(["currency_missing"]);
    expect(groups.map(procurementBlockerText)).toEqual(["Не указана валюта заказа"]);
  });

  it("keeps unknown codes apart by line instead of repeating one phrase", () => {
    const groups = groupProcurementBlockers(["line_2:brand_new_code", "line_5:brand_new_code"]);
    expect(groups).toHaveLength(1);
    expect(procurementBlockerText(groups[0])).toBe(
      "Требуется дополнительная проверка — строки 2, 5"
    );
  });
});

describe("groupProcurementRiskCodes", () => {
  it("печатает одинаковую подпись один раз и сохраняет коды в подсказке", () => {
    const groups = groupProcurementRiskCodes([
      "order_qty_rounded_to_multiple",
      "brand_new_code",
      "another_new_code",
    ]);

    expect(groups.map((group) => group.text)).toEqual([
      "Количество округлено до кратности",
      "Требуется дополнительная проверка",
    ]);
    expect(groups[1].codes).toEqual(["brand_new_code", "another_new_code"]);
  });
});
