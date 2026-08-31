import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CustomerReturnsWorkspace } from "./CustomerReturnsWorkspace";

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

const arrivedReturn = {
  id: 35,
  carrier: "russian_post",
  tracking_number: "12345678901234",
  status: "arrived_at_pickup_point",
  onec_order_ref: "ЗАКАЗ-3507",
  onec_return_ref: null,
  storage_deadline_at: "2026-09-05T18:00:00Z",
  onec_return_confirmed_at: null,
  updated_at: "2026-08-31T10:00:00Z",
};

const arrivedDetail = {
  ...arrivedReturn,
  events: [
    {
      id: 1,
      event_type: "registered",
      source: "bitrix_ui",
      normalized_status: "registered",
      occurred_at: "2026-08-30T10:00:00Z",
    },
    {
      id: 2,
      event_type: "carrier_status",
      source: "russian_post",
      normalized_status: "arrived_at_pickup_point",
      carrier_status_text: "Прибыло в отделение",
      occurred_at: "2026-08-31T10:00:00Z",
    },
  ],
};

describe("CustomerReturnsWorkspace", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
    vi.mocked(api.post).mockReset();
  });

  afterEach(cleanup);

  it("регистрирует трек Почты России или СДЭК из Bitrix24", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });
    vi.mocked(api.post).mockResolvedValue({
      data: {
        created: true,
        shipment: {
          ...arrivedDetail,
          carrier: "cdek",
          tracking_number: "CDEK-3507",
          status: "registered",
          storage_deadline_at: null,
          events: [arrivedDetail.events[0]],
        },
      },
    });

    render(<CustomerReturnsWorkspace />);
    await screen.findByText("Возвраты с выбранными условиями не найдены");

    fireEvent.change(screen.getByRole("combobox", { name: "Перевозчик возврата" }), {
      target: { value: "cdek" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Трек-номер возврата" }), {
      target: { value: "CDEK-3507" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Заказ 1С" }), {
      target: { value: "ЗАКАЗ-3507" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Зарегистрировать" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        "/bitrix/logistics/customer-returns",
        {
          carrier: "cdek",
          tracking_number: "CDEK-3507",
          onec_order_ref: "ЗАКАЗ-3507",
        },
        undefined
      )
    );
    expect(await screen.findByText("Возврат зарегистрирован")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Возврат CDEK-3507" })).toBeVisible();
  });

  it("показывает срок хранения и подтверждает действие «Забрали» текущим пользователем", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/customer-returns") return { data: [arrivedReturn] };
      if (path === "/bitrix/logistics/customer-returns/35") return { data: arrivedDetail };
      throw new Error(`unexpected GET ${path}`);
    });
    vi.mocked(api.post).mockResolvedValue({
      data: {
        ...arrivedDetail,
        status: "picked_up",
        picked_up_at: "2026-08-31T12:00:00Z",
        events: [
          ...arrivedDetail.events,
          {
            id: 3,
            event_type: "pickup_confirmed",
            source: "bitrix24",
            normalized_status: "picked_up",
            occurred_at: "2026-08-31T12:00:00Z",
          },
        ],
      },
    });

    render(<CustomerReturnsWorkspace />);

    expect(await screen.findByText("Можно забирать")).toBeVisible();
    expect(screen.getByText(/Хранение: до/)).toBeVisible();
    expect(screen.getByText("1С: ожидает сверки")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Открыть карточку" }));

    expect(await screen.findByText("Прибыло в отделение")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Комментарий к получению" }), {
      target: { value: "Получено онлайн-отделом" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Забрали" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        "/bitrix/logistics/customer-returns/35/pickup",
        {
          idempotency_key: "bitrix-ui-pickup-35",
          comment: "Получено онлайн-отделом",
        },
        undefined
      )
    );
    expect(
      await screen.findByText("Получение возврата подтверждено. Поставлен контроль сверки с 1С.")
    ).toBeVisible();
    expect(screen.getAllByText("Забрали").length).toBeGreaterThan(1);
    expect(screen.queryByRole("button", { name: "Забрали" })).not.toBeInTheDocument();
  });
});
