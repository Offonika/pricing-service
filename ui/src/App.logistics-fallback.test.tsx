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
  role: "sender" | "receiver" | "logist" | "admin",
  openDraft: Record<string, unknown> | null = null
) {
  const defaultWarehouseId = ["logist", "admin"].includes(role) ? null : 10;
  return vi.fn((input: string | URL | Request, init?: RequestInit) => {
    void init;
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
    if (path.endsWith("/handoffs/draft/61/items/62/remove")) {
      return jsonResponse({
        ...openDraft,
        item_count: 0,
        items: [],
      });
    }
    if (path.endsWith("/handoffs/draft/61/cancel")) {
      return jsonResponse({
        ...openDraft,
        status: "cancelled",
        item_count: 0,
        items: [],
      });
    }
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
    if (path.endsWith("/receipts/draft")) {
      return jsonResponse({
        id: 42,
        draft_type: "receipt",
        status: "open",
        warehouse_id: 20,
        driver_id: null,
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
    const fallbackApi = mockFallbackApi("sender");
    vi.stubGlobal("fetch", fallbackApi);

    render(<LogisticsFallbackApp />);

    expect(await screen.findByRole("heading", { name: "Передача" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Приемка" })).not.toBeInTheDocument();
    expect(screen.getByText("Центральный склад")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Водитель" })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Склад назначения" })).not.toBeInTheDocument();
    expect(screen.getByText("Определится автоматически после сканирования документа")).toBeVisible();
    const openDraft = screen.getByRole("button", { name: "Открыть" });
    expect(openDraft).toBeEnabled();
    fireEvent.click(openDraft);
    expect(await screen.findByRole("button", { name: "Открыть камеру" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скан" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Подтвердить" })).toBeDisabled();
    expect(document.title).toBe("Логистика — браузер");
    const createCall = fallbackApi.mock.calls.find(([input]) =>
      String(input).endsWith("/handoffs/draft")
    );
    expect(createCall?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ warehouse_id: 10, driver_id: 30, comment: "" }),
    });
  });

  it("показывает получателю только приёмку", async () => {
    vi.stubGlobal("fetch", mockFallbackApi("receiver"));

    render(<LogisticsFallbackApp />);

    expect(await screen.findByRole("heading", { name: "Приёмка" })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Водитель" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Склад назначения" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть" })).toBeEnabled();
  });

  it("открывает администратору обе операции и выбор склада", async () => {
    const fallbackApi = mockFallbackApi("admin");
    vi.stubGlobal("fetch", fallbackApi);

    render(<LogisticsFallbackApp />);

    const operation = await screen.findByRole("combobox", { name: "Операция" });
    const warehouse = screen.getByRole("combobox", { name: "Склад операции" });
    expect(operation).toHaveValue("handoff");
    expect(warehouse).toHaveValue("10");
    await waitFor(() =>
      expect(fallbackApi).toHaveBeenCalledWith(
        "/api/logistics/web/monitor?warehouse_id=10",
        expect.any(Object)
      )
    );
    fireEvent.change(operation, { target: { value: "receipt" } });
    fireEvent.change(warehouse, { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "Открыть" }));

    expect(await screen.findByText("Черновик #42")).toBeVisible();
    const createCall = fallbackApi.mock.calls.find(([input]) =>
      String(input).endsWith("/receipts/draft")
    );
    expect(createCall?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ warehouse_id: 20, comment: "" }),
    });
  });

  it("восстанавливает открытый черновик в браузере", async () => {
    const fallbackApi = mockFallbackApi("sender", {
        id: 61,
        draft_type: "handoff",
        status: "open",
        warehouse_id: 10,
        driver_id: 30,
        item_count: 1,
        items: [
          {
            id: 62,
            barcode: "BC-61",
            lookup_code: "MMLOG1|rtu|61|220061",
            document_number: "РТУ-000061",
          },
        ],
      });
    vi.stubGlobal("fetch", fallbackApi);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<LogisticsFallbackApp />);

    expect(await screen.findByText("#61")).toBeVisible();
    expect(screen.getByText(/РТУ-000061/)).toBeVisible();
    expect(screen.getByText("Черновик #61 восстановлен")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подтвердить" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText("Ошибочный скан удалён")).toBeVisible();
    expect(screen.queryByText(/РТУ-000061/)).not.toBeInTheDocument();
    const removeCall = fallbackApi.mock.calls.find(([input]) =>
      String(input).endsWith("/handoffs/draft/61/items/62/remove")
    );
    expect(removeCall?.[1]).toMatchObject({ method: "POST", credentials: "include" });

    fireEvent.click(screen.getByRole("button", { name: "Отменить черновик" }));
    expect(await screen.findByText("Черновик отменён. Можно начать заново.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Открыть" })).toBeEnabled();
    const cancelCall = fallbackApi.mock.calls.find(([input]) =>
      String(input).endsWith("/handoffs/draft/61/cancel")
    );
    expect(cancelCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({ reason: "Отменено пользователем в web fallback" }),
    });
  });

  it("не показывает логисту операции и ограничивает монитор складом пилота", async () => {
    const fallbackApi = mockFallbackApi("logist");
    vi.stubGlobal("fetch", fallbackApi);

    render(<LogisticsFallbackApp />);

    expect(
      await screen.findByText("Для этой роли доступны мониторинг и история в приложении Bitrix24")
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
    const warehouse = screen.getByRole("combobox", { name: "Склад мониторинга" });
    fireEvent.change(warehouse, { target: { value: "20" } });
    await waitFor(() =>
      expect(fallbackApi).toHaveBeenCalledWith(
        "/api/logistics/web/monitor?warehouse_id=20",
        expect.any(Object)
      )
    );
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
