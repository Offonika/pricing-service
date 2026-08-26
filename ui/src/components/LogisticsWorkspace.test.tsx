import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { LogisticsWorkspace } from "./LogisticsWorkspace";

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

const bootstrap = {
  profile: {
    id: 1,
    full_name: "Отправитель",
    role: "sender",
    default_warehouse_id: 10,
    default_warehouse_name: "Центральный склад",
  },
  warehouses: [
    { id: 10, external_id: "central", name: "Центральный склад", kind: "central" },
    { id: 20, external_id: "teply-stan", name: "Тёплый Стан", kind: "store" },
  ],
  drivers: [{ id: 30, full_name: "Иван Водитель" }],
  capabilities: ["handoff", "monitor", "history"],
};

const logistBootstrap = {
  ...bootstrap,
  profile: {
    id: 2,
    full_name: "Кештов Арсений Юрьевич",
    role: "logist",
    default_warehouse_id: null,
    default_warehouse_name: null,
  },
  capabilities: ["expected", "monitor", "history", "errors"],
};

describe("LogisticsWorkspace", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") return { data: bootstrap };
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("сохраняет пакет сканов после ошибки следующего кода", async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({
        data: {
          id: 41,
          draft_type: "handoff",
          status: "open",
          warehouse_id: 10,
          driver_id: 30,
          item_count: 0,
          items: [],
        },
      })
      .mockResolvedValueOnce({
        data: {
          id: 41,
          draft_type: "handoff",
          status: "open",
          warehouse_id: 10,
          driver_id: 30,
          item_count: 1,
          items: [
            {
              id: 51,
              document_number: "РТУ-000051",
              lookup_code: "MMLOG1|rtu|51|216951",
              barcode: "BC-51",
            },
          ],
        },
      })
      .mockRejectedValueOnce(new Error("Код уже добавлен"));

    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Начать сканирование" }));
    const input = await screen.findByPlaceholderText("QR, штрихкод или номер");
    fireEvent.change(input, { target: { value: "MMLOG1|rtu|51|216951" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить код" }));

    expect(await screen.findByText("РТУ-000051")).toBeVisible();
    expect(screen.getByRole("button", { name: "Подтвердить (1)" })).toBeEnabled();

    fireEvent.change(input, { target: { value: "BAD-CODE" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить код" }));
    expect(await screen.findByText("Код уже добавлен")).toBeVisible();
    expect(screen.getByText("РТУ-000051")).toBeVisible();
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(3));
  });

  it("показывает безопасную очередь разбора с фильтром и пагинацией", async () => {
    const firstItem = {
      id: 71,
      review_type: "rtu_target_warehouse_unresolved",
      document_number: "РБГУ0408001",
      rtu_number: "РБГУ0408001",
      onec_order_number: "РБГУ0067001",
      site_order_number: "220001",
      source_warehouse_name: "Сайт",
      delivery_method: "Самовывоз",
      created_at: "2026-08-26T12:30:00Z",
    };
    vi.mocked(api.get).mockImplementation(async (path: string, config?: { params?: Record<string, unknown> }) => {
      if (path === "/bitrix/logistics/bootstrap") return { data: logistBootstrap };
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      if (path === "/bitrix/logistics/errors") {
        if (config?.params?.review_type) {
          return {
            data: {
              items: [firstItem],
              total: 1,
              limit: 30,
              offset: 0,
              counts: { rtu_target_warehouse_unresolved: 1 },
            },
          };
        }
        if (config?.params?.offset === 1) {
          return {
            data: {
              items: [{ ...firstItem, id: 72, document_number: "РБГУ0408002" }],
              total: 2,
              limit: 30,
              offset: 1,
              counts: { rtu_target_warehouse_unresolved: 2 },
            },
          };
        }
        return {
          data: {
            items: [firstItem],
            total: 2,
            limit: 30,
            offset: 0,
            counts: { rtu_target_warehouse_unresolved: 2 },
          },
        };
      }
      if (path === "/bitrix/logistics/expected-deliveries") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });

    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Разбор" }));

    expect(await screen.findByText("РБГУ0408001")).toBeVisible();
    expect(screen.getByText("Не определён магазин")).toBeVisible();
    expect(screen.getByText("Заказ 1С: РБГУ0067001")).toBeVisible();
    expect(screen.getByText("Кештов Арсений Юрьевич")).toBeVisible();
    expect(screen.getByText("Логист")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Показать ещё (1)" }));
    expect(await screen.findByText("РБГУ0408002")).toBeVisible();

    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "rtu_target_warehouse_unresolved" },
    });
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith("/bitrix/logistics/errors", {
        params: {
          limit: 30,
          offset: 0,
          review_type: "rtu_target_warehouse_unresolved",
        },
      })
    );
  });

  it("не возвращает старую очередь после быстрого переключения фильтра", async () => {
    let resolveStalePage!: (value: { data: Record<string, unknown> }) => void;
    const stalePage = new Promise<{ data: Record<string, unknown> }>((resolve) => {
      resolveStalePage = resolve;
    });
    const filteredItem = {
      id: 81,
      review_type: "rtu_source_invalid",
      document_number: "РБГУ0408081",
      created_at: "2026-08-26T12:30:00Z",
    };

    vi.mocked(api.get).mockImplementation(
      async (path: string, config?: { params?: Record<string, unknown> }) => {
        if (path === "/bitrix/logistics/bootstrap") return { data: logistBootstrap };
        if (path === "/bitrix/logistics/monitor") return { data: [] };
        if (path === "/bitrix/logistics/errors") {
          if (config?.params?.review_type === "rtu_source_invalid") {
            return {
              data: {
                items: [filteredItem],
                total: 1,
                limit: 30,
                offset: 0,
                counts: { rtu_source_invalid: 1 },
              },
            };
          }
          return stalePage;
        }
        throw new Error(`unexpected GET ${path}`);
      }
    );

    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Разбор" }));
    fireEvent.change(screen.getByLabelText("Причина"), {
      target: { value: "rtu_source_invalid" },
    });

    expect(screen.queryByText("УСТАРЕВШАЯ-РТУ")).not.toBeInTheDocument();
    expect(screen.getByText("Загружаем очередь…")).toBeVisible();
    expect(await screen.findByText("РБГУ0408081")).toBeVisible();
    resolveStalePage({
      data: {
        items: [
          {
            id: 80,
            review_type: "rtu_target_warehouse_unresolved",
            document_number: "УСТАРЕВШАЯ-РТУ",
            created_at: "2026-08-26T12:00:00Z",
          },
        ],
        total: 1,
        limit: 30,
        offset: 0,
        counts: { rtu_target_warehouse_unresolved: 1 },
      },
    });

    await waitFor(() => expect(screen.getByText("РБГУ0408081")).toBeVisible());
    expect(screen.queryByText("УСТАРЕВШАЯ-РТУ")).not.toBeInTheDocument();
  });
});
