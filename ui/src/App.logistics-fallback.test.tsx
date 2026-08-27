import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LogisticsFallbackApp } from "./App";

const warehouses = [
  { id: 10, name: "Центральный склад", kind: "central" },
  { id: 20, name: "Тёплый Стан", kind: "store" },
];
const drivers = [{ id: 30, full_name: "Иван Водитель" }];

function jsonResponse(data: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
  );
}

function mockFallbackApi(
  role: "sender" | "receiver" | "logist",
  openDraft: Record<string, unknown> | null = null
) {
  const defaultWarehouseId = role === "logist" ? null : 10;
  return vi.fn((input: string | URL | Request) => {
    const path = String(input);
    if (path.endsWith("/profile")) {
      return jsonResponse({
        id: 1,
        full_name: "Тестовый сотрудник",
        role,
        default_warehouse_id: defaultWarehouseId,
        default_warehouse_name: defaultWarehouseId ? "Центральный склад" : null,
      });
    }
    if (path.endsWith("/warehouses")) return jsonResponse(warehouses);
    if (path.endsWith("/drivers")) return jsonResponse(drivers);
    if (path.endsWith("/draft/open")) return jsonResponse(openDraft);
    if (path.includes("/monitor?")) return jsonResponse([]);
    if (path.endsWith("/handoffs/draft")) {
      return jsonResponse({
        id: 41,
        draft_type: "handoff",
        status: "open",
        warehouse_id: 10,
        driver_id: 30,
        item_count: 0,
        items: [],
      });
    }
    throw new Error(`unexpected fetch ${path}`);
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("LogisticsFallbackApp", () => {
  it("показывает отправителю только передачу с фиксированным исходным складом", async () => {
    vi.stubGlobal("fetch", mockFallbackApi("sender"));

    render(<LogisticsFallbackApp />);

    expect(await screen.findByRole("heading", { name: "Передача" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Приемка" })).not.toBeInTheDocument();
    expect(screen.getByText("Центральный склад")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Водитель" })).toBeVisible();
    const destination = screen.getByRole("combobox", { name: "Склад назначения" });
    expect(destination).toHaveValue("20");
    const openDraft = screen.getByRole("button", { name: "Открыть" });
    expect(openDraft).toBeEnabled();
    fireEvent.click(openDraft);
    expect(await screen.findByRole("button", { name: "Открыть камеру" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скан" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Подтвердить" })).toBeDisabled();
    expect(document.title).toBe("Логистика — браузер");
  });

  it("показывает получателю только приёмку", async () => {
    vi.stubGlobal("fetch", mockFallbackApi("receiver"));

    render(<LogisticsFallbackApp />);

    expect(await screen.findByRole("heading", { name: "Приёмка" })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Водитель" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Склад назначения" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть" })).toBeEnabled();
  });

  it("восстанавливает открытый черновик в браузере", async () => {
    vi.stubGlobal(
      "fetch",
      mockFallbackApi("sender", {
        id: 61,
        draft_type: "handoff",
        status: "open",
        warehouse_id: 10,
        driver_id: 30,
        default_dropoff_warehouse_id: 20,
        item_count: 1,
        items: [
          {
            id: 62,
            barcode: "BC-61",
            lookup_code: "MMLOG1|rtu|61|220061",
            document_number: "РТУ-000061",
          },
        ],
      })
    );

    render(<LogisticsFallbackApp />);

    expect(await screen.findByText("#61")).toBeVisible();
    expect(screen.getByText(/РТУ-000061/)).toBeVisible();
    expect(screen.getByText("Черновик #61 восстановлен")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подтвердить" })).toBeEnabled();
  });

  it("не показывает логисту операции чужого склада", async () => {
    vi.stubGlobal("fetch", mockFallbackApi("logist"));

    render(<LogisticsFallbackApp />);

    expect(
      await screen.findByText("Для этой роли доступны мониторинг и история в приложении Bitrix24")
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Логистика" })).toBeVisible());
  });

  it("повторяет первоначальную загрузку после временной ошибки", async () => {
    const fallbackApi = mockFallbackApi("sender");
    let profileFailed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string | URL | Request) => {
        if (String(input).endsWith("/profile") && !profileFailed) {
          profileFailed = true;
          return Promise.reject(new Error("Сервис временно недоступен"));
        }
        return fallbackApi(input);
      })
    );

    render(<LogisticsFallbackApp />);

    expect(await screen.findByText("Сервис временно недоступен")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByRole("heading", { name: "Передача" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
  });

  it("не создаёт два черновика по быстрому двойному клику", async () => {
    const fallbackApi = mockFallbackApi("sender");
    vi.stubGlobal("fetch", fallbackApi);

    render(<LogisticsFallbackApp />);

    const openDraft = await screen.findByRole("button", { name: "Открыть" });
    fireEvent.click(openDraft);
    fireEvent.click(openDraft);

    await screen.findByText("Черновик #41");
    const createCalls = fallbackApi.mock.calls.filter(([input]) =>
      String(input).endsWith("/handoffs/draft")
    );
    expect(createCalls).toHaveLength(1);
  });
});
