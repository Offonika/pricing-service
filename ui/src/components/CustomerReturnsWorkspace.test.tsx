import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import {
  openBitrixCustomerReturnDeal,
  openBitrixCustomerReturnServiceRequest,
} from "../api/bitrix";
import { CustomerReturnsWorkspace } from "./CustomerReturnsWorkspace";

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}));

vi.mock("../api/bitrix", () => ({
  isBitrixLogisticsRoute: vi.fn(() => false),
  openBitrixCustomerReturnDeal: vi.fn().mockResolvedValue(undefined),
  openBitrixCustomerReturnServiceRequest: vi.fn().mockResolvedValue(undefined),
  refreshBitrixLogisticsSession: vi.fn().mockResolvedValue(undefined),
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
    vi.mocked(api.put).mockReset();
    vi.mocked(openBitrixCustomerReturnDeal).mockClear();
    vi.mocked(openBitrixCustomerReturnServiceRequest).mockClear();
  });

  afterEach(cleanup);

  it("открывает рабочую справку и возвращает фокус после закрытия", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });

    render(<CustomerReturnsWorkspace />);
    await screen.findByText("Возвраты с выбранными условиями не найдены");

    const trigger = screen.getByRole("button", { name: "Открыть справку по возвратам" });
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Справка по возвратам" });
    expect(dialog).toBeVisible();
    expect(screen.getByRole("heading", { name: "Как зарегистрировать возврат" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "Тестирование" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Закрыть справку" })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Справка по возвратам" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("показывает администраторам отдельную инструкцию тестирования", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [] });

    render(<CustomerReturnsWorkspace showTestingGuide />);
    await screen.findByText("Возвраты с выбранными условиями не найдены");
    fireEvent.click(screen.getByRole("button", { name: "Открыть справку по возвратам" }));
    fireEvent.click(screen.getByRole("tab", { name: "Тестирование" }));

    expect(screen.getByText("99999999999999")).toBeVisible();
    expect(screen.getByText("TEST-3507-CDEK")).toBeVisible();
    expect(screen.getByText(/Тест создаёт записи в Production/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Проверка пилота" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Закрыть справку" }));
    expect(screen.queryByRole("dialog", { name: "Справка по возвратам" })).not.toBeInTheDocument();
  });

  it("открывает карточку поверх реестра и закрывает её по Escape", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/customer-returns") return { data: [arrivedReturn] };
      if (path === "/bitrix/logistics/customer-returns/35") return { data: arrivedDetail };
      throw new Error(`unexpected GET ${path}`);
    });

    render(<CustomerReturnsWorkspace />);
    const trigger = await screen.findByRole("button", { name: "Открыть карточку" });
    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "Возврат 12345678901234" });
    expect(dialog).toBeVisible();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Закрыть карточку возврата" })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Возврат 12345678901234" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("регистрирует трек Почты России или СДЭК из Bitrix24", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/customer-returns") return { data: [] };
      if (path === "/bitrix/logistics/customer-return-deals") {
        return {
          data: [
            {
              deal_id: 3507,
              title: "Интернет-заказ 241094",
              order_ref: "241094",
              stage_name: "Новая",
              closed: false,
              contact_name: "Иван Петров",
              responsible_name: "Анна Смирнова",
            },
          ],
        };
      }
      throw new Error(`unexpected GET ${path}`);
    });
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
    fireEvent.change(screen.getByRole("combobox", { name: "Сделка Bitrix24 (необязательно)" }), {
      target: { value: "241094" },
    });
    fireEvent.click(await screen.findByRole("option", { name: /Интернет-заказ 241094/ }));
    fireEvent.click(screen.getByRole("button", { name: "Зарегистрировать" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        "/bitrix/logistics/customer-returns",
        {
          carrier: "cdek",
          tracking_number: "CDEK-3507",
          bitrix_deal_id: 3507,
        },
        undefined
      )
    );
    expect(await screen.findByText("Возврат зарегистрирован")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Возврат CDEK-3507" })).toBeVisible();
  });

  it("привязывает сделку позднее из карточки возврата", async () => {
    const linkedDetail = {
      ...arrivedDetail,
      bitrix_deal_id: 3507,
      bitrix_deal_title: "Интернет-заказ 241094",
      bitrix_order_ref: "241094",
      bitrix_deal_stage_name: "Новая",
      bitrix_contact_name: "Иван Петров",
      bitrix_responsible_name: "Анна Смирнова",
      events: [
        ...arrivedDetail.events,
        {
          id: 3,
          event_type: "deal_link_changed",
          source: "bitrix24",
          occurred_at: "2026-09-01T12:00:00Z",
        },
      ],
    };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/customer-returns") return { data: [arrivedReturn] };
      if (path === "/bitrix/logistics/customer-returns/35") return { data: arrivedDetail };
      if (path === "/bitrix/logistics/customer-return-deals") {
        return {
          data: [
            {
              deal_id: 3507,
              title: "Интернет-заказ 241094",
              order_ref: "241094",
              stage_name: "Новая",
              closed: false,
              contact_name: "Иван Петров",
              responsible_name: "Анна Смирнова",
            },
          ],
        };
      }
      throw new Error(`unexpected GET ${path}`);
    });
    vi.mocked(api.put).mockResolvedValue({ data: linkedDetail });

    render(<CustomerReturnsWorkspace />);
    fireEvent.click(await screen.findByRole("button", { name: "Открыть карточку" }));
    fireEvent.click(await screen.findByRole("button", { name: "Привязать сделку" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Новая сделка Bitrix24" }), {
      target: { value: "241094" },
    });
    fireEvent.click(await screen.findByRole("option", { name: /Интернет-заказ 241094/ }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить связь" }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith(
        "/bitrix/logistics/customer-returns/35/deal-link",
        { bitrix_deal_id: 3507 },
        undefined
      )
    );
    expect(await screen.findByText("Сделка привязана к возврату")).toBeVisible();
    expect(screen.getByText(/Сделка: #3507/)).toBeVisible();
    expect(screen.getByText("Клиент: Иван Петров")).toBeVisible();
    expect(screen.getByText("Ответственный: Анна Смирнова")).toBeVisible();
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

  it("убирает полученный возврат из активного фильтра «Можно забирать»", async () => {
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
      },
    });

    render(<CustomerReturnsWorkspace />);
    expect(await screen.findByText("Можно забирать")).toBeVisible();

    fireEvent.change(screen.getByRole("combobox", { name: "Фильтр по состоянию" }), {
      target: { value: "arrived_at_pickup_point" },
    });
    await waitFor(() =>
      expect(api.get).toHaveBeenLastCalledWith(
        "/bitrix/logistics/customer-returns",
        { params: { limit: 100, status: "arrived_at_pickup_point" } }
      )
    );

    fireEvent.click(screen.getByRole("button", { name: "Открыть карточку" }));
    expect(await screen.findByRole("button", { name: "Забрали" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Забрали" }));

    expect(
      await screen.findByText("Получение возврата подтверждено. Поставлен контроль сверки с 1С.")
    ).toBeVisible();
    expect(screen.getByLabelText("Возвратов в списке: 0")).toBeVisible();
    expect(screen.getByText("Возвраты с выбранными условиями не найдены")).toBeVisible();
  });

  it("регистрирует возврат с обращением, выбранным с клавиатуры", async () => {
    const serviceRequest = {
      item_id: 113401,
      title: "Обращение по заказу 241094",
      stage_name: "Новое",
      closed: false,
      deal_id: 3507,
      order_ref: "241094",
      responsible_name: "Анна Смирнова",
      site_ticket_id: "7001",
    };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/customer-returns") return { data: [] };
      if (path === "/bitrix/logistics/customer-return-service-requests") {
        return { data: [serviceRequest] };
      }
      throw new Error(`unexpected GET ${path}`);
    });
    vi.mocked(api.post).mockResolvedValue({
      data: {
        created: true,
        shipment: {
          ...arrivedDetail,
          tracking_number: "CDEK-SERVICE-3507",
          carrier: "cdek",
          status: "registered",
          bitrix_deal_id: 3507,
          service_request_item_id: 113401,
          serviceRequest,
          expertiseCases: [],
        },
      },
    });

    render(<CustomerReturnsWorkspace serviceLinksEnabled />);
    await screen.findByText("Возвраты с выбранными условиями не найдены");
    fireEvent.change(screen.getByRole("combobox", { name: "Перевозчик возврата" }), {
      target: { value: "cdek" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Трек-номер возврата" }), {
      target: { value: "CDEK-SERVICE-3507" },
    });
    const requestPicker = screen.getByRole("combobox", {
      name: "Сервисное обращение (необязательно)",
    });
    fireEvent.change(requestPicker, { target: { value: "113401" } });
    await screen.findByRole("option", { name: /Обращение по заказу 241094/ });
    fireEvent.keyDown(requestPicker, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Зарегистрировать" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        "/bitrix/logistics/customer-returns",
        {
          carrier: "cdek",
          tracking_number: "CDEK-SERVICE-3507",
          bitrix_deal_id: null,
          serviceRequestItemId: 113401,
        },
        undefined,
      ),
    );
    expect(await screen.findByText(/#113401 · Обращение по заказу 241094/)).toBeVisible();
  });

  it("фильтрует возвраты без обращения", async () => {
    const withoutRequest = { ...arrivedReturn, id: 36, tracking_number: "NO-SERVICE-36" };
    vi.mocked(api.get).mockImplementation(async (path: string, config?: unknown) => {
      if (path !== "/bitrix/logistics/customer-returns") {
        throw new Error(`unexpected GET ${path}`);
      }
      const params = (config as { params?: Record<string, unknown> } | undefined)?.params;
      return {
        data: params?.without_service_request
          ? [withoutRequest]
          : [
              {
                ...arrivedReturn,
                service_request_item_id: 113401,
                serviceRequest: { item_id: 113401, title: "Обращение 113401" },
              },
              withoutRequest,
            ],
      };
    });

    render(<CustomerReturnsWorkspace serviceLinksEnabled />);
    expect((await screen.findAllByText("Обращение не привязано")).length).toBeGreaterThan(0);
    fireEvent.change(
      screen.getByRole("combobox", { name: "Фильтр по сервисному обращению" }),
      { target: { value: "missing" } },
    );

    await waitFor(() =>
      expect(api.get).toHaveBeenLastCalledWith(
        "/bitrix/logistics/customer-returns",
        { params: { limit: 100, without_service_request: "true" } },
      ),
    );
    expect(screen.getByText("NO-SERVICE-36")).toBeVisible();
    expect(screen.queryByText("Обращение #113401")).not.toBeInTheDocument();
  });

  it("заменяет и удаляет обращение, показывает экспертизы и открывает связи", async () => {
    const firstRequest = {
      item_id: 113401,
      title: "Первое обращение",
      stage_name: "Новое",
      closed: false,
      deal_id: 3507,
      order_ref: "241094",
    };
    const secondRequest = {
      ...firstRequest,
      item_id: 113402,
      title: "Закрытое обращение",
      stage_name: "Закрыто",
      closed: true,
    };
    const linkedDetail = {
      ...arrivedDetail,
      bitrix_deal_id: 3507,
      bitrix_deal_title: "Интернет-заказ 241094",
      bitrix_order_ref: "241094",
      service_request_item_id: 113401,
      serviceRequest: firstRequest,
      expertiseCases: [
        {
          id: 51,
          onec_expertise_number: "ЭКС-51",
          current_status: "registered",
          linked_customer_order_number: "241094",
          service_request_item_id: 113401,
        },
      ],
    };
    const relinkedDetail = {
      ...linkedDetail,
      service_request_item_id: 113402,
      serviceRequest: secondRequest,
      expertiseCases: [],
    };
    const unlinkedDetail = {
      ...relinkedDetail,
      service_request_item_id: null,
      serviceRequest: null,
      expertiseCases: [],
    };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/bitrix/logistics/customer-returns") return { data: [linkedDetail] };
      if (path === "/bitrix/logistics/customer-returns/35") return { data: linkedDetail };
      if (path === "/bitrix/logistics/customer-return-service-requests") {
        return { data: [secondRequest] };
      }
      throw new Error(`unexpected GET ${path}`);
    });
    vi.mocked(api.put)
      .mockResolvedValueOnce({ data: relinkedDetail })
      .mockResolvedValueOnce({ data: unlinkedDetail });

    render(<CustomerReturnsWorkspace serviceLinksEnabled />);
    fireEvent.click(await screen.findByRole("button", { name: "Открыть карточку" }));
    expect(await screen.findByText("Экспертиза ЭКС-51")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Открыть сделку" }));
    fireEvent.click(screen.getByRole("button", { name: "Открыть обращение" }));
    expect(openBitrixCustomerReturnDeal).toHaveBeenCalledWith(3507);
    expect(openBitrixCustomerReturnServiceRequest).toHaveBeenCalledWith(113401);

    fireEvent.click(screen.getByRole("button", { name: "Изменить обращение" }));
    const picker = screen.getByRole("combobox", { name: "Сервисное обращение" });
    fireEvent.change(picker, { target: { value: "113402" } });
    fireEvent.click(await screen.findByRole("option", { name: /Закрытое обращение/ }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить обращение" }));
    await waitFor(() =>
      expect(api.put).toHaveBeenNthCalledWith(
        1,
        "/bitrix/logistics/customer-returns/35/service-request-link",
        { serviceRequestItemId: 113402 },
        undefined,
      ),
    );
    expect(await screen.findByText(/#113402 · Закрытое обращение · закрыто/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Изменить обращение" }));
    fireEvent.click(screen.getByRole("button", { name: "Убрать связь" }));
    await waitFor(() =>
      expect(api.put).toHaveBeenNthCalledWith(
        2,
        "/bitrix/logistics/customer-returns/35/service-request-link",
        { serviceRequestItemId: null },
        undefined,
      ),
    );
    expect((await screen.findAllByText("Обращение не привязано")).length).toBeGreaterThan(0);
    expect(screen.getByText(/Сделка: #3507/)).toBeVisible();
  });
});
