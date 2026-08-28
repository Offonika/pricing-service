import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CameraScanner, LogisticsWorkspace } from "./LogisticsWorkspace";

const zxing = vi.hoisted(() => ({
  decodeFromConstraints: vi.fn(),
  decodeFromImageUrl: vi.fn(),
  stop: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

vi.mock("@zxing/browser", () => ({
  BrowserMultiFormatReader: class {
    decodeFromConstraints = zxing.decodeFromConstraints;
    decodeFromImageUrl = zxing.decodeFromImageUrl;
  },
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

const adminBootstrap = {
  ...bootstrap,
  profile: {
    id: 3,
    full_name: "Кештов Арсений Юрьевич",
    role: "admin",
    default_warehouse_id: null,
    default_warehouse_name: null,
  },
  capabilities: ["handoff", "receipt", "expected", "monitor", "history", "errors"],
};

describe("LogisticsWorkspace", () => {
  beforeEach(() => {
    zxing.decodeFromConstraints.mockResolvedValue({ stop: zxing.stop });
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") return { data: bootstrap };
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });
  });

  it("открывает администратору все экраны, обе операции и выбор склада", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") return { data: adminBootstrap };
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });
    vi.mocked(api.post).mockResolvedValueOnce({
      data: {
        id: 44,
        draft_type: "receipt",
        status: "open",
        warehouse_id: 20,
        driver_id: null,
        item_count: 0,
        items: [],
      },
    });

    render(<LogisticsWorkspace />);

    expect(await screen.findByText("Администратор")).toBeVisible();
    expect(screen.getByRole("button", { name: "Сканер" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Ожидаются" })).toBeVisible();
    expect(screen.getByRole("button", { name: "В пути" })).toBeVisible();
    expect(screen.getByRole("button", { name: "История" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Разбор" })).toBeVisible();

    fireEvent.change(screen.getByRole("combobox", { name: "Операция" }), {
      target: { value: "receipt" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Склад операции" }), {
      target: { value: "20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Начать сканирование" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/bitrix/logistics/receipts/draft", {
        warehouse_id: 20,
        comment: null,
      }, undefined)
    );
  });

  it("просит заднюю камеру в запасном ZXing-сканере", async () => {
    vi.mocked(api.post).mockResolvedValueOnce({
      data: {
        id: 42,
        draft_type: "handoff",
        status: "open",
        warehouse_id: 10,
        driver_id: 30,
        item_count: 0,
        items: [],
      },
    });

    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Начать сканирование" }));
    fireEvent.click(await screen.findByRole("button", { name: "Открыть камеру" }));

    await waitFor(() =>
      expect(zxing.decodeFromConstraints).toHaveBeenCalledWith(
        {
          video: { facingMode: { ideal: "environment" } },
          audio: false,
        },
        expect.any(HTMLVideoElement),
        expect.any(Function)
      )
    );
  });

  it("объясняет отказ в доступе к камере без сырого текста браузера", async () => {
    zxing.decodeFromConstraints.mockRejectedValueOnce(
      Object.assign(new Error("Permission denied by system"), { name: "NotAllowedError" })
    );
    vi.mocked(api.post).mockResolvedValueOnce({
      data: {
        id: 43,
        draft_type: "handoff",
        status: "open",
        warehouse_id: 10,
        driver_id: 30,
        item_count: 0,
        items: [],
      },
    });

    render(<LogisticsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Начать сканирование" }));
    fireEvent.click(await screen.findByRole("button", { name: "Открыть камеру" }));

    expect(await screen.findByText(/Нет доступа к камере/)).toBeVisible();
    expect(screen.queryByText("Permission denied by system")).not.toBeInTheDocument();
  });

  it("останавливает поток камеры, полученный после закрытия сканера", async () => {
    let resolveStream!: (stream: MediaStream) => void;
    const delayedStream = new Promise<MediaStream>((resolve) => {
      resolveStream = resolve;
    });
    const stop = vi.fn();
    const originalMediaDevices = navigator.mediaDevices;
    const detectorWindow = window as unknown as { BarcodeDetector?: unknown };
    const originalDetector = detectorWindow.BarcodeDetector;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(() => delayedStream) },
    });
    Object.defineProperty(window, "BarcodeDetector", {
      configurable: true,
      value: class {
        detect = vi.fn();
      },
    });

    try {
      const view = render(<CameraScanner onCode={vi.fn()} onClose={vi.fn()} />);
      view.unmount();
      resolveStream({ getTracks: () => [{ stop }] } as unknown as MediaStream);

      await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
    } finally {
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: originalMediaDevices,
      });
      Object.defineProperty(window, "BarcodeDetector", {
        configurable: true,
        value: originalDetector,
      });
    }
  });

  it("игнорирует позднее распознавание фото после закрытия сканера", async () => {
    let resolveDecode!: (result: { getText: () => string }) => void;
    zxing.decodeFromImageUrl.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveDecode = resolve;
      })
    );
    const createObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
    const revokeObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:test-photo"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    const onCode = vi.fn();
    const onClose = vi.fn();

    try {
      const view = render(<CameraScanner onCode={onCode} onClose={onClose} />);
      await waitFor(() => expect(zxing.decodeFromConstraints).toHaveBeenCalled());
      const fileInput = view.container.querySelector<HTMLInputElement>('input[type="file"]');
      expect(fileInput).not.toBeNull();
      fireEvent.change(fileInput!, {
        target: { files: [new File(["barcode"], "barcode.png", { type: "image/png" })] },
      });
      await waitFor(() =>
        expect(zxing.decodeFromImageUrl).toHaveBeenCalledWith("blob:test-photo")
      );

      view.unmount();
      resolveDecode({ getText: () => "LATE-CODE" });

      await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:test-photo"));
      expect(onCode).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      if (createObjectUrlDescriptor) {
        Object.defineProperty(URL, "createObjectURL", createObjectUrlDescriptor);
      } else {
        delete (URL as unknown as { createObjectURL?: unknown }).createObjectURL;
      }
      if (revokeObjectUrlDescriptor) {
        Object.defineProperty(URL, "revokeObjectURL", revokeObjectUrlDescriptor);
      } else {
        delete (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL;
      }
    }
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
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

  it("повторяет загрузку рабочего места после ошибки bootstrap", async () => {
    vi.mocked(api.get)
      .mockRejectedValueOnce(new Error("Сервис временно недоступен"))
      .mockResolvedValueOnce({ data: bootstrap })
      .mockResolvedValue({ data: [] });

    render(<LogisticsWorkspace />);

    expect(await screen.findByText("Сервис временно недоступен")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByText("Передать водителю")).toBeVisible();
    expect(api.get).toHaveBeenCalledWith("/bitrix/logistics/bootstrap", undefined);
  });

  it("открывает ровно один fallback-контекст для одноразовой ссылки", async () => {
    const replace = vi.fn();
    const popup = {
      opener: window,
      location: { replace },
      close: vi.fn(),
    } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(popup);
    vi.mocked(api.post).mockResolvedValueOnce({
      data: { url: "https://bitrix-app.example/logistics/fallback?launch=one-time" },
    });

    render(<LogisticsWorkspace />);
    const fallbackButton = await screen.findByRole("button", {
      name: "Открыть сканер в браузере",
    });
    fireEvent.click(fallbackButton);
    fireEvent.click(fallbackButton);

    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        "https://bitrix-app.example/logistics/fallback?launch=one-time"
      )
    );
    expect(window.open).toHaveBeenCalledTimes(1);
    expect(popup.opener).toBeNull();
  });

  it("восстанавливает собственный открытый черновик после перезагрузки", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") {
        return {
          data: {
            ...bootstrap,
            open_draft: {
              id: 77,
              draft_type: "handoff",
              status: "open",
              warehouse_id: 10,
              driver_id: 30,
              default_dropoff_warehouse_id: 20,
              item_count: 1,
              items: [
                {
                  id: 78,
                  document_number: "РТУ-000077",
                  lookup_code: "MMLOG1|rtu|77|220077",
                  barcode: "BC-77",
                },
              ],
            },
          },
        };
      }
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });
    vi.mocked(api.post)
      .mockResolvedValueOnce({
        data: {
          id: 77,
          draft_type: "handoff",
          status: "open",
          warehouse_id: 10,
          driver_id: 30,
          default_dropoff_warehouse_id: 20,
          item_count: 0,
          items: [],
        },
      })
      .mockResolvedValueOnce({
        data: {
          id: 77,
          draft_type: "handoff",
          status: "cancelled",
          warehouse_id: 10,
          driver_id: 30,
          default_dropoff_warehouse_id: 20,
          item_count: 0,
          items: [],
        },
      });

    render(<LogisticsWorkspace />);

    expect(await screen.findByText("Черновик №77")).toBeVisible();
    expect(screen.getByText("РТУ-000077")).toBeVisible();
    expect(screen.getByText("Черновик №77 восстановлен")).toBeVisible();
    expect(screen.getByRole("button", { name: "Подтвердить (1)" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText("Ошибочный скан удалён")).toBeVisible();
    expect(screen.queryByText("РТУ-000077")).not.toBeInTheDocument();
    expect(api.post).toHaveBeenNthCalledWith(
      1,
      "/bitrix/logistics/handoffs/draft/77/items/78/remove",
      undefined,
      undefined
    );

    fireEvent.click(screen.getByRole("button", { name: "Отменить черновик" }));
    expect(await screen.findByText("Черновик отменён. Можно начать заново.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Начать сканирование" })).toBeEnabled();
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      "/bitrix/logistics/handoffs/draft/77/cancel",
      { reason: "Отменено пользователем в Bitrix24" },
      undefined
    );
  });

  it("не начинает передачу без активного водителя", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") {
        return { data: { ...bootstrap, drivers: [] } };
      }
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });

    render(<LogisticsWorkspace />);

    expect(await screen.findByText("Нет активных водителей")).toBeVisible();
    expect(screen.getByRole("button", { name: "Начать сканирование" })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("объясняет отправителю отсутствие привязки склада", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") {
        return {
          data: {
            ...bootstrap,
            profile: {
              ...bootstrap.profile,
              default_warehouse_id: null,
              default_warehouse_name: null,
            },
          },
        };
      }
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });

    render(<LogisticsWorkspace />);

    expect(await screen.findByText("Не назначен")).toBeVisible();
    expect(screen.getByText("Обратитесь к логисту для привязки склада")).toBeVisible();
    expect(screen.getByRole("button", { name: "Начать сканирование" })).toBeDisabled();
  });

  it("не начинает передачу без магазина назначения", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") {
        return { data: { ...bootstrap, warehouses: [bootstrap.warehouses[0]] } };
      }
      if (path === "/bitrix/logistics/monitor") return { data: [] };
      throw new Error(`unexpected GET ${path}`);
    });

    render(<LogisticsWorkspace />);

    expect(await screen.findByText("Нет доступного магазина назначения")).toBeVisible();
    expect(screen.getByRole("button", { name: "Начать сканирование" })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("открывает понятную историю только после выбора перемещения", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/bootstrap") return { data: bootstrap };
      if (path === "/bitrix/logistics/monitor") {
        return {
          data: [
            {
              transfer_id: 91,
              document_number: "РТУ-000091",
              status: "in_transit",
              dropoff_warehouse_name: "Тёплый Стан",
              driver_name: "Иван Водитель",
              last_event_at: "2026-08-27T10:00:00Z",
              manual_review_count: 0,
            },
          ],
        };
      }
      if (path === "/bitrix/logistics/transfers/91/history") {
        return {
          data: [
            {
              id: 301,
              event_type: "handed_to_driver",
              event_at: "2026-08-27T10:00:00Z",
              warehouse_name: "Центральный склад",
              dropoff_warehouse_name: "Тёплый Стан",
              driver_name: "Иван Водитель",
              user_name: "Отправитель",
              source: "bitrix",
            },
          ],
        };
      }
      throw new Error(`unexpected GET ${path}`);
    });

    render(<LogisticsWorkspace />);

    expect(await screen.findByRole("button", { name: "В пути" })).toBeVisible();
    expect(screen.getByRole("button", { name: "История" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "В пути" }));
    await screen.findByText("РТУ-000091");
    const historyButtons = screen.getAllByRole("button", { name: "История" });
    fireEvent.click(historyButtons.at(-1)!);

    expect(await screen.findByText("Передано водителю")).toBeVisible();
    expect(screen.getByText("Центральный склад → Тёплый Стан")).toBeVisible();
    expect(screen.getByText("Событие №301 · Водитель: Иван Водитель")).toBeVisible();
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
