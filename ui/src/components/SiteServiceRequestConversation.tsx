import { useCallback, useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import { api } from "../api/client";
import "./SiteServiceRequestConversation.css";

type Attachment = {
  id: string;
  name: string;
  mimeType: string;
  size: number;
  status: string;
  downloadUrl: string | null;
};

type ConversationMessage = {
  id: string;
  direction: "inbound" | "outbound" | "internal";
  authorLabel: string;
  text: string | null;
  createdAt: string;
  deliveryStatus: "received" | "sending" | "delivered" | "failed" | "note";
  errorCode: string | null;
  retryable: boolean;
  visibleToCustomer: boolean;
  attachments: Attachment[];
};

type Conversation = {
  itemId: number;
  sourceKind: "site_ticket" | "bitrix_mail";
  ticketId: number | null;
  canReply: boolean;
  canAttachFiles: boolean;
  originalUrl: string | null;
  nextBeforeId: number | null;
  messages: ConversationMessage[];
};

const TEMPLATES = [
  "Здравствуйте! Обращение приняли в работу. Сообщим о результате здесь.",
  "Пожалуйста, пришлите номер заказа и фотографии товара, упаковки и дефекта.",
  "Передали информацию специалисту. Вернёмся с ответом после проверки.",
];

const statusLabel: Record<ConversationMessage["deliveryStatus"], string> = {
  received: "Получено",
  sending: "Отправляется",
  delivered: "Доставлено",
  failed: "Ошибка отправки",
  note: "Внутренняя заметка",
};

const conversationDateFormatter = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function formatDate(value: string) {
  return conversationDateFormatter.format(new Date(value));
}

function newRequestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function SiteServiceRequestConversation({ itemId }: { itemId: number }) {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"reply" | "note">("reply");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const pendingRequestIdRef = useRef<string | null>(null);

  const resetPendingRequest = () => {
    pendingRequestIdRef.current = null;
  };

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const { data } = await api.get<Conversation>(
        `/site-service-requests/ui/items/${itemId}/conversation`,
      );
      if (!data.canAttachFiles) setFiles([]);
      setConversation(data);
      setError("");
    } catch {
      setError("Не удалось загрузить переписку. Попробуйте ещё раз.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [itemId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!loading) listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [loading]);

  const loadOlder = async () => {
    if (!conversation?.nextBeforeId) return;
    const { data } = await api.get<Conversation>(
      `/site-service-requests/ui/items/${itemId}/conversation`,
      { params: { beforeId: conversation.nextBeforeId } },
    );
    setConversation({
      ...conversation,
      nextBeforeId: data.nextBeforeId,
      messages: [...data.messages, ...conversation.messages],
    });
  };

  const submit = async () => {
    const normalized = text.trim();
    if (!normalized) {
      toast.error(mode === "note" ? "Введите текст заметки" : "Введите ответ клиенту");
      return;
    }
    setBusy(true);
    try {
      const clientRequestId = pendingRequestIdRef.current || newRequestId();
      pendingRequestIdRef.current = clientRequestId;
      if (mode === "note") {
        await api.post(`/site-service-requests/ui/items/${itemId}/notes`, {
          clientRequestId,
          text: normalized,
        });
      } else {
        const form = new FormData();
        form.append("clientRequestId", clientRequestId);
        form.append("text", normalized);
        if (conversation?.canAttachFiles) {
          files.forEach((file) => form.append("files", file));
        }
        await api.post(`/site-service-requests/ui/items/${itemId}/replies`, form);
      }
      setText("");
      setFiles([]);
      resetPendingRequest();
      await load(true);
      requestAnimationFrame(() =>
        listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" }),
      );
      toast.success(mode === "note" ? "Заметка сохранена" : "Ответ поставлен в отправку");
    } catch (requestError: unknown) {
      const status = (
        requestError as { response?: { status?: number } }
      )?.response?.status;
      if (status === 409) resetPendingRequest();
      toast.error(mode === "note" ? "Не удалось сохранить заметку" : "Не удалось отправить ответ");
    } finally {
      setBusy(false);
    }
  };

  const retry = async (message: ConversationMessage) => {
    const commandId = Number(message.id.replace("command:", ""));
    if (!commandId) return;
    setBusy(true);
    try {
      await api.post(
        `/site-service-requests/ui/items/${itemId}/replies/${commandId}/retry`,
      );
      await load(true);
      toast.success("Повторная отправка запущена");
    } catch {
      toast.error("Не удалось повторить отправку");
    } finally {
      setBusy(false);
    }
  };

  const download = async (attachment: Attachment) => {
    if (!attachment.downloadUrl) return;
    try {
      const response = await api.get<Blob>(attachment.downloadUrl.replace("/api", ""), {
        responseType: "blob",
      });
      const url = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = attachment.name;
      link.click();
      URL.revokeObjectURL(url);
    } catch {
      toast.error("Не удалось скачать файл");
    }
  };

  if (loading) return <div className="ssr-chat-state">Загружаем переписку…</div>;
  if (error && !conversation) {
    return <div className="ssr-chat-state"><p>{error}</p><button onClick={() => void load()}>Повторить</button></div>;
  }
  if (!conversation) return null;

  return (
    <main className="ssr-chat">
      <header className="ssr-chat__header">
        <div>
          <h1>Переписка с клиентом</h1>
          <p className="ssr-chat__context">
            {conversation.sourceKind === "site_ticket" && conversation.ticketId
              ? `Тикет сайта №${conversation.ticketId}`
              : "Обращение по электронной почте"}
          </p>
          {conversation.canReply && (
            <p className="ssr-chat__delivery-hint">Ответ попадёт в личный кабинет клиента.</p>
          )}
        </div>
        {conversation.originalUrl && (
          <a href={conversation.originalUrl} target="_blank" rel="noreferrer">Открыть обращение на сайте</a>
        )}
      </header>

      {!conversation.canReply && (
        <section className="ssr-chat__channel-warning" role="status">
          {conversation.sourceKind === "bitrix_mail"
            ? "Это обращение пришло по электронной почте. Ответьте через письмо в таймлайне CRM."
            : "Только просмотр. Отправка ответов из карточки временно недоступна."}
        </section>
      )}

      <section className="ssr-chat__messages" ref={listRef} aria-live="polite" aria-label="История переписки">
        {conversation.nextBeforeId && <button className="ssr-chat__older" onClick={() => void loadOlder()}>Показать предыдущие сообщения</button>}
        {conversation.messages.length === 0 && <p className="ssr-chat__empty">Сообщений пока нет.</p>}
        {conversation.messages.map((message) => (
          <article key={message.id} className={`ssr-message ssr-message--${message.direction}`}>
            <div className="ssr-message__meta"><strong>{message.authorLabel}</strong><time>{formatDate(message.createdAt)}</time></div>
            <p>{message.text ?? "Текст удалён по сроку хранения"}</p>
            {message.attachments.length > 0 && <div className="ssr-message__files">
              {message.attachments.map((attachment) => (
                <button key={attachment.id} disabled={!attachment.downloadUrl} onClick={() => void download(attachment)}>
                  {attachment.name} · {(attachment.size / 1024).toFixed(0)} КБ
                </button>
              ))}
            </div>}
            <div className="ssr-message__status">
              <span>{statusLabel[message.deliveryStatus]}</span>
              {message.retryable && conversation.canReply && <button disabled={busy} onClick={() => void retry(message)}>Повторить</button>}
            </div>
          </article>
        ))}
      </section>

      {conversation.canReply ? (
        <section className={`ssr-composer ssr-composer--${mode}`}>
          <div className="ssr-composer__mode" role="group" aria-label="Тип сообщения">
            <button className={mode === "reply" ? "is-active" : ""} onClick={() => { if (mode !== "reply") { setMode("reply"); resetPendingRequest(); } }}>Ответ клиенту</button>
            <button className={mode === "note" ? "is-active" : ""} onClick={() => { if (mode !== "note") { setMode("note"); setFiles([]); resetPendingRequest(); } }}>Внутренняя заметка</button>
          </div>
          {mode === "note" && <p className="ssr-composer__notice">Клиент не увидит эту заметку.</p>}
          {mode === "reply" && <select aria-label="Шаблон ответа" defaultValue="" onChange={(event) => { if (event.target.value) { setText(event.target.value); resetPendingRequest(); } event.target.value = ""; }}>
            <option value="">Вставить шаблон…</option>
            {TEMPLATES.map((template) => <option key={template} value={template}>{template}</option>)}
          </select>}
          <textarea value={text} onChange={(event) => { setText(event.target.value); resetPendingRequest(); }} placeholder={mode === "note" ? "Заметка для коллег" : "Напишите ответ клиенту"} rows={4} />
          {mode === "reply" && conversation.canAttachFiles && <label className="ssr-composer__files">Прикрепить файлы
            <input type="file" multiple onChange={(event) => { setFiles(Array.from(event.target.files || []).slice(0, 5)); resetPendingRequest(); }} />
          </label>}
          {files.length > 0 && <ul className="ssr-composer__selected" aria-label="Выбранные файлы">
            {files.map((file, index) => <li key={`${file.name}-${file.size}-${index}`}>
              <span>{file.name} · {(file.size / 1024).toFixed(0)} КБ</span>
              <button type="button" onClick={() => { setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index)); resetPendingRequest(); }}>Убрать</button>
            </li>)}
          </ul>}
          <button className="ssr-composer__send" disabled={busy || !text.trim()} onClick={() => void submit()}>
            {busy ? "Сохраняем…" : mode === "note" ? "Сохранить заметку" : "Отправить клиенту"}
          </button>
        </section>
      ) : null}
    </main>
  );
}
