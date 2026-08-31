import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initializeBitrixLogisticsSession } from "./api/bitrix";
import { BitrixLogisticsApp } from "./BitrixLogisticsApp";

vi.mock("./api/bitrix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/bitrix")>();
  return {
    ...actual,
    initializeBitrixLogisticsSession: vi.fn(),
  };
});

vi.mock("./components/LogisticsWorkspace", () => ({
  LogisticsWorkspace: () => <div>Рабочее место логистики</div>,
}));

describe("BitrixLogisticsApp", () => {
  beforeEach(() => {
    vi.mocked(initializeBitrixLogisticsSession).mockReset();
  });

  afterEach(cleanup);

  it("объясняет, что приложение нужно открыть из меню Bitrix24 при ошибке SDK", async () => {
    vi.mocked(initializeBitrixLogisticsSession).mockRejectedValueOnce(
      new Error("Не удалось загрузить Bitrix24 SDK")
    );

    render(<BitrixLogisticsApp />);

    expect(await screen.findByText("Откройте приложение из меню Bitrix24.")).toBeVisible();
    expect(screen.getByText("Не удалось загрузить Bitrix24 SDK")).toBeVisible();
    expect(screen.queryByText("Проверьте привязку пользователя к роли и складу.")).toBeNull();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeEnabled();
  });

  it("показывает проверку роли и склада только при настоящем отказе доступа", async () => {
    vi.mocked(initializeBitrixLogisticsSession).mockRejectedValueOnce(
      new Error("Request failed with status code 403")
    );

    render(<BitrixLogisticsApp />);

    expect(
      await screen.findByText("Нет доступа к логистике. Проверьте роль и привязку склада.")
    ).toBeVisible();
    expect(screen.queryByText("Откройте приложение из меню Bitrix24.")).toBeNull();
  });

  it("повторно открывает сессию после ошибки SDK", async () => {
    vi.mocked(initializeBitrixLogisticsSession)
      .mockRejectedValueOnce(new Error("Не удалось загрузить Bitrix24 SDK"))
      .mockResolvedValueOnce({} as Awaited<ReturnType<typeof initializeBitrixLogisticsSession>>);

    render(<BitrixLogisticsApp />);

    fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));

    expect(await screen.findByText("Рабочее место логистики")).toBeVisible();
    expect(initializeBitrixLogisticsSession).toHaveBeenCalledTimes(2);
  });
});
