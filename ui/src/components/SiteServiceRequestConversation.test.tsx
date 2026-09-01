import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { SiteServiceRequestConversation } from "./SiteServiceRequestConversation";

vi.mock("../api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const failedMessage = {
  id: "command:41",
  direction: "outbound",
  authorLabel: "Тимур Тибилов · Поддержка",
  text: "Ответ клиенту",
  createdAt: "2026-08-31T10:00:00+03:00",
  deliveryStatus: "failed",
  errorCode: "message_write_failed",
  retryable: true,
  visibleToCustomer: true,
  attachments: [],
};

function conversation(overrides: Record<string, unknown> = {}) {
  return {
    itemId: 392,
    sourceKind: "site_ticket",
    ticketId: 760,
    canReply: false,
    canAttachFiles: false,
    originalUrl: "https://master-mobile.ru/personal/tickets/?ID=760",
    nextBeforeId: null,
    messages: [failedMessage],
    ...overrides,
  };
}

describe("SiteServiceRequestConversation", () => {
  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(),
    });
  });

  beforeEach(() => {
    vi.mocked(api.get).mockReset();
    vi.mocked(api.post).mockReset();
  });

  afterEach(cleanup);

  it("показывает понятный read-only режим до истории и скрывает все действия", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: conversation() });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByText("Тикет сайта №760")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Только просмотр. Отправка ответов из карточки временно недоступна.",
    );
    expect(screen.getByRole("link", { name: "Открыть обращение на сайте" })).toHaveAttribute(
      "href",
      "https://master-mobile.ru/personal/tickets/?ID=760",
    );
    expect(screen.queryByText("Ответ попадёт в личный кабинет клиента.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Напишите ответ клиенту")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Прикрепить файлы")).not.toBeInTheDocument();
  });

  it("разрешает текст и retry, но не предлагает файлы без capability", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: conversation({ canReply: true, canAttachFiles: false }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByText("Ответ попадёт в личный кабинет клиента.")).toBeVisible();
    expect(screen.getByPlaceholderText("Напишите ответ клиенту")).toBeVisible();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeVisible();
    expect(screen.queryByText("Только просмотр.")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Прикрепить файлы")).not.toBeInTheDocument();
  });

  it("показывает файлы только при отдельной capability", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: conversation({ canReply: true, canAttachFiles: true }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByLabelText("Прикрепить файлы")).toBeVisible();
  });

  it("направляет email-обращение в штатный таймлайн CRM", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: conversation({
        sourceKind: "bitrix_mail",
        ticketId: null,
        originalUrl: null,
        messages: [],
      }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByText("Обращение по электронной почте")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Ответьте через письмо в таймлайне CRM.",
    );
  });

  it("подгружает предыдущую страницу и сохраняет статусы внутренних заметок", async () => {
    const internalNote = {
      ...failedMessage,
      id: "note:17",
      direction: "internal",
      authorLabel: "Администратор · Поддержка",
      text: "Проверить заказ у логиста",
      deliveryStatus: "note",
      errorCode: null,
      retryable: false,
      visibleToCustomer: false,
    };
    vi.mocked(api.get)
      .mockResolvedValueOnce({
        data: conversation({ canReply: true, nextBeforeId: 41 }),
      })
      .mockResolvedValueOnce({
        data: conversation({
          canReply: true,
          nextBeforeId: null,
          messages: [internalNote],
        }),
      });

    render(<SiteServiceRequestConversation itemId={392} />);
    fireEvent.click(await screen.findByRole("button", { name: "Показать предыдущие сообщения" }));

    const noteText = await screen.findByText("Проверить заказ у логиста");
    expect(noteText).toBeVisible();
    expect(noteText.closest("article")).toHaveTextContent("Внутренняя заметка");
    await waitFor(() =>
      expect(api.get).toHaveBeenNthCalledWith(
        2,
        "/site-service-requests/ui/items/392/conversation",
        { params: { beforeId: 41 } },
      ),
    );
  });
});
