import { describe, expect, it } from "vitest";

import {
  eventLabel,
  factorLabel,
  reasonLabel,
  recommendationLabel,
  roleLabel,
  snapshotMonthLabel,
  statusLabel,
} from "./customerPriceTypeLabels";

describe("русские подписи рабочего места типов цен", () => {
  it("переводит роли, статусы, рекомендации и события", () => {
    expect(roleLabel("network_head")).toBe("Руководитель сети");
    expect(roleLabel("executive")).toBe("Генеральный директор");
    expect(statusLabel("partial")).toBe("Данные загружены частично");
    expect(statusLabel("not_requested")).toBe("Не запрашивалось");
    expect(recommendationLabel("manager_retention")).toBe("Передать менеджеру на удержание");
    expect(eventLabel("case_created")).toBe("Кейс создан");
  });

  it("не показывает служебные коды в причинах и ограничениях", () => {
    expect(factorLabel("human_approval_required")).toBe("Требуется решение ответственного");
    expect(factorLabel("source_contracts_missing")).toBe("Источник «договоры»: нет данных");
    expect(reasonLabel("Требуется сверка данных: multi_contract.")).toBe(
      "Требуется сверка данных: найдено несколько договоров.",
    );
  });

  it("показывает расчётный месяц по-русски", () => {
    expect(snapshotMonthLabel("2026-06")).toBe("июнь 2026 г.");
  });
});
