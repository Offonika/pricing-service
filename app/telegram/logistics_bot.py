from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.dependencies import get_engine
from app.core.config import get_settings
from app.models import (
    LogisticsBotSession,
    LogisticsBotSessionPhoto,
    LogisticsDraft,
    LogisticsDraftItem,
    LogisticsDriver,
    LogisticsWarehouse,
)
from app.services import logistics as logistics_service

logger = logging.getLogger(__name__)


HELP_TEXT = """
Как работать:
1. Нажмите кнопку в меню.
2. Откройте передачу или приемку.
3. Сканируйте штрихкоды сообщениями в чат.
4. При необходимости добавьте фото.
5. Нажмите «Подтвердить».

Быстрые команды:
/start
/help
/handoff <driver_id> <dropoff_warehouse_id>
/receive
/confirm
/cancel
/expected
/monitor

Фото, отправленное во время открытого черновика, прикрепится к ближайшему подтверждению.
""".strip()

REMOVE_SCAN_PAGE_SIZE = 20


class TelegramApiError(RuntimeError):
    def __init__(self, api_method: str, status_code: int | None = None) -> None:
        self.api_method = api_method
        self.status_code = status_code
        if status_code is None:
            message = f"telegram {api_method} request failed"
        else:
            message = f"telegram {api_method} failed with HTTP {status_code}"
        super().__init__(message)


def _inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": callback_data} for text, callback_data in row]
            for row in rows
        ]
    }


def _main_menu(profile: dict[str, Any]) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    if profile["role"] == "sender":
        rows.append([("📦 Передать", "menu:handoff")])
    elif profile["role"] == "receiver":
        rows.append([("📥 Принять", "menu:receive")])
    rows.append([("🚚 Ожидается", "menu:expected"), ("📊 Монитор", "menu:monitor")])
    if profile["role"] in {"logist", "admin"}:
        rows.append([("🏬 Склады", "menu:warehouses"), ("👤 Водители", "menu:drivers")])
    return _inline_keyboard(rows)


def _draft_controls() -> dict[str, Any]:
    return _inline_keyboard(
        [
            [("✅ Подтвердить", "draft:confirm"), ("✖️ Отмена", "draft:cancel")],
            [("🧾 Статус", "draft:status"), ("🗑 Удалить скан", "draft:remove_picker")],
            [("⚠️ Инцидент", "draft:incident_picker"), ("↩️ Возврат", "draft:return_picker")],
        ]
    )


def _remove_scan_picker(
    draft: LogisticsDraft,
    page: int,
) -> tuple[str, dict[str, Any]]:
    items = sorted(draft.items, key=lambda item: item.id)
    page_count = max(1, (len(items) + REMOVE_SCAN_PAGE_SIZE - 1) // REMOVE_SCAN_PAGE_SIZE)
    current_page = min(max(page, 0), page_count - 1)
    page_start = current_page * REMOVE_SCAN_PAGE_SIZE
    page_items = items[page_start : page_start + REMOVE_SCAN_PAGE_SIZE]

    rows = [
        [
            (
                f"{item.transfer.document_number} | {item.barcode}",
                f"draft:remove:{item.id}",
            )
        ]
        for item in page_items
    ]
    navigation: list[tuple[str, str]] = []
    if current_page > 0:
        navigation.append(("⬅️ Назад", f"draft:remove_picker:{current_page - 1}"))
    navigation.append(
        (
            f"{current_page + 1}/{page_count}",
            f"draft:remove_picker:{current_page}",
        )
    )
    if current_page < page_count - 1:
        navigation.append(("Далее ➡️", f"draft:remove_picker:{current_page + 1}"))
    rows.append(navigation)
    rows.append([("↩️ К статусу", "draft:status")])
    return (
        f"🗑 Выберите ошибочный скан. Страница {current_page + 1}/{page_count}:",
        _inline_keyboard(rows),
    )


class TelegramBotApi:
    def __init__(self, token: str):
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = httpx.Client(timeout=40.0)

    def _request(self, method: str, api_method: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{self._base_url}/{api_method}",
                **kwargs,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TelegramApiError(api_method, exc.response.status_code) from None
        except httpx.RequestError:
            raise TelegramApiError(api_method) from None
        return response

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload = {"timeout": timeout_seconds}
        if offset is not None:
            payload["offset"] = offset
        response = self._request("GET", "getUpdates", params=payload)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getUpdates failed: {data}")
        return data.get("result", [])

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = self._request("POST", "sendMessage", json=payload)
        return response.json().get("result", {})

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = self._request("POST", "editMessageText", json=payload)
        return response.json().get("result", {})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        self._request("POST", "answerCallbackQuery", json=payload)

    def set_webhook(self, url: str, secret_token: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        response = self._request("POST", "setWebhook", json=payload)
        return response.json()

    def get_webhook_info(self) -> dict[str, Any]:
        response = self._request("GET", "getWebhookInfo")
        return response.json()

    def delete_webhook(self, drop_pending_updates: bool = False) -> dict[str, Any]:
        response = self._request(
            "POST",
            "deleteWebhook",
            json={"drop_pending_updates": drop_pending_updates},
        )
        return response.json()

    def close(self) -> None:
        self._client.close()


class LogisticsTelegramBot:
    def __init__(self, bot_api: TelegramBotApi, engine) -> None:
        self.bot_api = bot_api
        self.engine = engine

    def run_forever(self, timeout_seconds: int = 30) -> None:
        offset: int | None = None
        while True:
            try:
                updates = self.bot_api.get_updates(offset=offset, timeout_seconds=timeout_seconds)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except KeyboardInterrupt:  # pragma: no cover
                raise
            except Exception:  # pragma: no cover
                logger.exception("logistics telegram bot loop failed")
                time.sleep(3)

    def handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback_query(callback_query)
            return
        message = update.get("message")
        if not message:
            return
        chat_id = int(message["chat"]["id"])
        from_user = message.get("from") or {}
        telegram_user_id = int(from_user.get("id", 0))
        username = from_user.get("username")

        with Session(self.engine) as session:
            try:
                profile = logistics_service.telegram_auth(
                    session,
                    telegram_user_id=telegram_user_id,
                    username=username,
                )
            except Exception:
                self.bot_api.send_message(
                    chat_id,
                    "Пользователь Telegram не привязан к логистическому профилю. Обратитесь к логисту/админу.",
                )
                return

            if message.get("photo"):
                self._handle_photo(session, chat_id, telegram_user_id, profile["id"], message)
                return

            text = (message.get("text") or "").strip()
            if not text:
                return
            if text.startswith("/"):
                reply = self._handle_command(session, profile, chat_id, text)
            else:
                try:
                    reply = self._handle_barcode(session, profile, chat_id, text)
                except Exception as exc:
                    current = self._get_session(session, chat_id)
                    if current is None:
                        reply = f"Ошибка сканирования: {exc}"
                    else:
                        self._register_scan_error(session, current, str(exc))
                        reply = self._render_draft_status(
                            session,
                            profile,
                            chat_id,
                            headline=f"Ошибка сканирования: {exc}",
                        )

            if reply:
                if isinstance(reply, tuple):
                    text, reply_markup = reply
                    current = self._get_session(session, chat_id)
                    if current is not None:
                        self._send_or_update_draft_message(
                            session, chat_id, text, reply_markup=reply_markup
                        )
                    else:
                        self._send(chat_id, text, reply_markup=reply_markup)
                else:
                    self._send(chat_id, reply)

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = callback_query["id"]
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        from_user = callback_query.get("from") or {}
        telegram_user_id = int(from_user.get("id", 0))
        username = from_user.get("username")
        data = callback_query.get("data") or ""

        with Session(self.engine) as session:
            try:
                profile = logistics_service.telegram_auth(
                    session,
                    telegram_user_id=telegram_user_id,
                    username=username,
                )
            except Exception:
                self.bot_api.answer_callback_query(
                    callback_id,
                    "Профиль Telegram не привязан.",
                )
                return

            try:
                reply, reply_markup = self._dispatch_callback(session, profile, chat_id, data)
                self.bot_api.answer_callback_query(callback_id)
            except Exception as exc:
                self.bot_api.answer_callback_query(callback_id, str(exc))
                return

            if reply:
                if data.startswith("draft:"):
                    self._send_or_update_draft_message(
                        session,
                        chat_id,
                        reply,
                        reply_markup=reply_markup,
                    )
                else:
                    self._send(chat_id, reply, reply_markup=reply_markup)

    def _get_session(
        self,
        session: Session,
        chat_id: int,
    ) -> LogisticsBotSession | None:
        return session.scalar(
            select(LogisticsBotSession)
            .where(LogisticsBotSession.chat_id == chat_id)
            .options(joinedload(LogisticsBotSession.photos))
        )

    def _list_open_drafts_for_actor(
        self,
        session: Session,
        actor_user_id: int,
    ) -> list[LogisticsDraft]:
        return session.scalars(
            select(LogisticsDraft)
            .where(
                LogisticsDraft.actor_user_id == actor_user_id,
                LogisticsDraft.status == "open",
            )
            .order_by(LogisticsDraft.id.asc())
        ).all()

    def _get_single_open_draft_for_actor(
        self,
        session: Session,
        actor_user_id: int,
    ) -> LogisticsDraft | None:
        drafts = self._list_open_drafts_for_actor(session, actor_user_id)
        if not drafts:
            return None
        if len(drafts) > 1:
            raise RuntimeError(
                "Обнаружено несколько открытых черновиков. Требуется ручная очистка/закрытие."
            )
        return drafts[0]

    def _resume_existing_draft(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
        draft_id: int,
        *,
        prefix: str = "У вас уже открыт черновик.",
    ) -> tuple[str, dict[str, Any] | None]:
        draft = self._get_open_draft(session, draft_id)
        if draft is None:
            return (
                "Открытый черновик найден, но больше недоступен. Обновите список и попробуйте снова.",
                _main_menu(profile),
            )
        self._upsert_session(
            session,
            chat_id=chat_id,
            telegram_user_id=profile["telegram_user_id"],
            actor_user_id=profile["id"],
            draft_id=draft.id,
            draft_type=draft.draft_type,
        )
        draft_label = "передачи" if draft.draft_type == "handoff" else "приемки"
        return (
            f"{prefix}\nПродолжаем черновик {draft_label} #{draft.id}.",
            _draft_controls(),
        )

    def _handle_create_draft_conflict(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
        exc: HTTPException,
    ) -> tuple[str, dict[str, Any] | None]:
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("message") == "open draft already exists":
            draft_id = detail.get("draft_id")
            if isinstance(draft_id, int):
                return self._resume_existing_draft(
                    session,
                    profile,
                    chat_id,
                    draft_id,
                    prefix="У вас уже есть открытый черновик.",
                )
        if isinstance(detail, dict) and detail.get("message") == "multiple open drafts found":
            return (
                "Обнаружено несколько открытых черновиков. Требуется ручная очистка/закрытие.",
                _main_menu(profile),
            )
        raise exc

    def _upsert_session(
        self,
        session: Session,
        *,
        chat_id: int,
        telegram_user_id: int,
        actor_user_id: int,
        draft_id: int,
        draft_type: str,
    ) -> LogisticsBotSession:
        current = self._get_session(session, chat_id)
        if current is None:
            current = LogisticsBotSession(
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                actor_user_id=actor_user_id,
                draft_id=draft_id,
                draft_type=draft_type,
                scan_error_count=0,
                recent_errors=[],
            )
            session.add(current)
        else:
            draft_changed = current.draft_id != draft_id
            current.telegram_user_id = telegram_user_id
            current.actor_user_id = actor_user_id
            current.draft_id = draft_id
            current.draft_type = draft_type
            current.scan_error_count = 0
            current.recent_errors = []
            if draft_changed:
                current.photos.clear()
                current.status_message_id = None
        session.commit()
        return self._get_session(session, chat_id)

    def _drop_session(self, session: Session, chat_id: int) -> LogisticsBotSession | None:
        current = self._get_session(session, chat_id)
        if current is None:
            return None
        session.delete(current)
        session.commit()
        return current

    def _cancel_current_draft_session(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
    ) -> tuple[str, dict[str, Any] | None]:
        current = self._get_session(session, chat_id)
        if current is None:
            return "Нет открытого draft.", _main_menu(profile)

        draft_id = current.draft_id
        already_closed = False
        try:
            logistics_service.cancel_draft(
                session,
                draft_id=draft_id,
                actor_user_id=profile["id"],
                reason="Отменено пользователем в Telegram",
            )
        except HTTPException as exc:
            session.expire_all()
            draft = session.get(LogisticsDraft, draft_id)
            if exc.status_code != 409 or draft is None or draft.status == "open":
                raise
            already_closed = True

        self._drop_session(session, chat_id)
        if already_closed:
            return (
                f"Черновик #{draft_id} уже закрыт. Сессия Telegram очищена; можно начать заново.",
                _main_menu(profile),
            )
        return (
            f"✖️ Черновик #{draft_id} отменён. Можно начать заново.",
            _main_menu(profile),
        )

    def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.bot_api.send_message(chat_id, text, reply_markup=reply_markup)

    def _send_or_update_draft_message(
        self,
        session: Session,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        bot_session = self._get_session(session, chat_id)
        if bot_session is None:
            self._send(chat_id, text, reply_markup=reply_markup)
            return
        if bot_session.status_message_id is not None:
            try:
                self.bot_api.edit_message_text(
                    chat_id,
                    bot_session.status_message_id,
                    text,
                    reply_markup=reply_markup,
                )
                return
            except (httpx.HTTPStatusError, TelegramApiError) as exc:
                status_code = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else exc.status_code
                )
                if status_code not in {400, 404}:
                    raise
                logger.warning(
                    "failed to edit logistics bot status message, sending a new one instead",
                    extra={
                        "chat_id": chat_id,
                        "status_message_id": bot_session.status_message_id,
                        "status_code": status_code,
                    },
                )
                bot_session.status_message_id = None
                session.commit()
        result = self.bot_api.send_message(chat_id, text, reply_markup=reply_markup)
        message_id = result.get("message_id")
        if message_id is not None:
            bot_session.status_message_id = int(message_id)
            session.commit()

    def _handle_photo(
        self,
        session: Session,
        chat_id: int,
        telegram_user_id: int,
        actor_user_id: int,
        message: dict[str, Any],
    ) -> None:
        current = self._get_session(session, chat_id)
        if current is None:
            try:
                draft = self._get_single_open_draft_for_actor(session, actor_user_id)
            except RuntimeError as exc:
                self.bot_api.send_message(chat_id, str(exc))
                return
            if draft is not None:
                current = self._upsert_session(
                    session,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    actor_user_id=actor_user_id,
                    draft_id=draft.id,
                    draft_type=draft.draft_type,
                )
        if current is None:
            self.bot_api.send_message(chat_id, "Нет открытого draft для привязки фото.")
            return
        photos = message.get("photo") or []
        best = photos[-1]
        current.photos.append(
            LogisticsBotSessionPhoto(
                telegram_file_id=str(best["file_id"])[:255],
                comment=(str(message.get("caption") or "")[:1000] or None),
            )
        )
        session.commit()
        self.bot_api.send_message(chat_id, "Фото добавлено к текущему draft.")

    def _get_open_draft(self, session: Session, draft_id: int) -> LogisticsDraft | None:
        return session.scalar(
            select(LogisticsDraft)
            .where(LogisticsDraft.id == draft_id)
            .options(
                joinedload(LogisticsDraft.warehouse),
                joinedload(LogisticsDraft.driver),
                joinedload(LogisticsDraft.default_dropoff_warehouse),
                joinedload(LogisticsDraft.items).joinedload(LogisticsDraftItem.transfer),
                joinedload(LogisticsDraft.items).joinedload(LogisticsDraftItem.dropoff_warehouse),
            )
        )

    def _register_scan_error(
        self,
        session: Session,
        bot_session: LogisticsBotSession,
        message: str,
    ) -> None:
        errors = list(bot_session.recent_errors or [])
        errors.append({"message": message})
        bot_session.scan_error_count += 1
        bot_session.recent_errors = errors[-5:]
        session.commit()

    def _render_draft_status(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
        headline: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        bot_session = self._get_session(session, chat_id)
        if bot_session is None:
            return "Нет открытого draft.", _main_menu(profile)
        draft = self._get_open_draft(session, bot_session.draft_id)
        if draft is None:
            self._drop_session(session, chat_id)
            return "Черновик больше недоступен.", _main_menu(profile)

        label = "📦 Передача" if draft.draft_type == "handoff" else "📥 Приемка"
        lines: list[str] = []
        if headline:
            lines.append(headline)
            lines.append("")
        lines.extend(
            [
                f"{label} #{draft.id}",
                f"🏬 Склад: {draft.warehouse.name}",
                f"📦 Позиции: {len(draft.items)}",
                f"📷 Фото к подтверждению: {len(bot_session.photos)}",
                f"⚠️ Ошибки сканирования: {bot_session.scan_error_count}",
            ]
        )
        if draft.driver is not None:
            lines.append(f"👤 Водитель: {draft.driver.full_name}")
        if draft.default_dropoff_warehouse is not None:
            lines.append(f"📍 Точка выгрузки: {draft.default_dropoff_warehouse.name}")
        if draft.items:
            lines.append("")
            lines.append("🧾 Последние сканы:")
            for item in sorted(draft.items, key=lambda row: row.scan_at, reverse=True)[:5]:
                lines.append(f"- {item.transfer.document_number} | {item.barcode}")
        if bot_session.recent_errors:
            lines.append("")
            lines.append("🚨 Последние ошибки:")
            for entry in bot_session.recent_errors[-3:]:
                lines.append(f"- {entry['message']}")
        return "\n".join(lines), _draft_controls()

    def _format_expected_deliveries(self, rows: list[dict[str, Any]]) -> str:
        lines = ["🚚 Ожидаемые доставки:"]
        for row in rows[:20]:
            lines.extend(
                [
                    f"• {row['document_number']}",
                    f"  Код: {row['barcode']}",
                    f"  Водитель: {row.get('driver_name') or 'не указан'}",
                    f"  Точка: {row.get('dropoff_warehouse_name') or '-'}",
                ]
            )
        return "\n".join(lines)

    def _format_monitor(self, rows: list[dict[str, Any]]) -> str:
        lines = ["📊 Монитор:"]
        for row in rows[:20]:
            status = "🚚 В пути" if row["status"] == "in_transit" else "📦 На складе"
            location = row.get("current_warehouse_name") or row.get("dropoff_warehouse_name") or "-"
            lines.extend(
                [
                    f"• {row['document_number']}",
                    f"  Статус: {status}",
                    f"  Где сейчас: {location}",
                ]
            )
        return "\n".join(lines)

    def _handle_command(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
        text: str,
    ) -> str | tuple[str, dict[str, Any] | None]:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()

        if command in {"/start", "/help"}:
            return (
                f"Здравствуйте, {profile['full_name']}.\n"
                f"Роль: {profile['role']}\n"
                f"Склад по умолчанию: {profile.get('default_warehouse_name') or 'не задан'}\n\n"
                "Выберите действие в меню ниже.\n\n"
                f"{HELP_TEXT}",
                _main_menu(profile),
            )

        if command == "/drivers":
            drivers = session.scalars(
                select(LogisticsDriver).where(LogisticsDriver.is_active.is_(True))
            ).all()
            if not drivers:
                return "Список водителей пуст."
            return "Водители:\n" + "\n".join(f"{row.id}: {row.full_name}" for row in drivers)

        if command == "/warehouses":
            warehouses = session.scalars(
                select(LogisticsWarehouse).where(LogisticsWarehouse.is_active.is_(True))
            ).all()
            return "Склады:\n" + "\n".join(
                f"{row.id}: {row.name} ({row.kind})" for row in warehouses
            )

        if command == "/handoff":
            if profile["role"] != "sender":
                return "Передача доступна только отправителю."
            if len(parts) < 3:
                return "Использование: /handoff <driver_id> <dropoff_warehouse_id>"
            try:
                payload = logistics_service.create_draft(
                    session,
                    draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
                    actor_user_id=profile["id"],
                    warehouse_id=profile["default_warehouse_id"],
                    driver_id=int(parts[1]),
                    default_dropoff_warehouse_id=int(parts[2]),
                )
            except HTTPException as exc:
                return self._handle_create_draft_conflict(session, profile, chat_id, exc)
            self._upsert_session(
                session,
                chat_id=chat_id,
                telegram_user_id=profile["telegram_user_id"],
                actor_user_id=profile["id"],
                draft_id=payload["id"],
                draft_type=payload["draft_type"],
            )
            return (
                f"📦 Создан черновик передачи #{payload['id']}.\n"
                "Отправляйте штрихкоды сообщением или добавляйте фото, затем /confirm.",
                _draft_controls(),
            )

        if command == "/receive":
            if profile["role"] != "receiver":
                return "Приёмка доступна только получателю."
            try:
                payload = logistics_service.create_draft(
                    session,
                    draft_type=logistics_service.DRAFT_TYPE_RECEIPT,
                    actor_user_id=profile["id"],
                    warehouse_id=profile["default_warehouse_id"],
                )
            except HTTPException as exc:
                return self._handle_create_draft_conflict(session, profile, chat_id, exc)
            self._upsert_session(
                session,
                chat_id=chat_id,
                telegram_user_id=profile["telegram_user_id"],
                actor_user_id=profile["id"],
                draft_id=payload["id"],
                draft_type=payload["draft_type"],
            )
            return (
                f"📥 Создан черновик приемки #{payload['id']}. Отправляйте штрихкоды сообщением.",
                _draft_controls(),
            )

        if command == "/confirm":
            current = self._get_session(session, chat_id)
            if current is None:
                return "Нет открытого draft."
            payload = logistics_service.confirm_draft(
                session,
                draft_id=current.draft_id,
                actor_user_id=profile["id"],
                comment=None,
                idempotency_key=f"tg-confirm-{chat_id}-{current.draft_id}",
                photos=[
                    {"telegram_file_id": photo.telegram_file_id, "comment": photo.comment}
                    for photo in current.photos
                ],
                source_channel="telegram",
            )
            self._drop_session(session, chat_id)
            return (
                f"✅ Черновик #{payload['draft_id']} подтвержден.\n"
                f"Событие: {payload['event_type']}\n"
                f"Обработано позиций: {payload['processed_count']}",
                _main_menu(profile),
            )

        if command == "/cancel":
            return self._cancel_current_draft_session(session, profile, chat_id)

        if command == "/expected":
            rows = logistics_service.list_expected_deliveries(
                session,
                warehouse_id=profile["default_warehouse_id"],
            )
            if not rows:
                return "Ожидаемых доставок нет."
            return self._format_expected_deliveries(rows)

        if command == "/monitor":
            rows = logistics_service.list_monitor(
                session, warehouse_id=profile["default_warehouse_id"]
            )
            if not rows:
                return "Перемещений для мониторинга нет."
            return self._format_monitor(rows)

        if command == "/incident":
            if len(parts) < 3:
                return "Использование: /incident <transfer_id> <comment>"
            logistics_service.create_transfer_event(
                session,
                transfer_id=int(parts[1]),
                actor_user_id=profile["id"],
                event_type=logistics_service.EVENT_INCIDENT,
                source="telegram",
                warehouse_id=profile["default_warehouse_id"],
                comment=" ".join(parts[2:]),
                idempotency_key=f"tg-incident-{parts[1]}",
                photos=[],
            )
            return "Инцидент зафиксирован."

        if command == "/return":
            if len(parts) < 4:
                return "Использование: /return <transfer_id> <warehouse_id> <comment>"
            logistics_service.create_transfer_event(
                session,
                transfer_id=int(parts[1]),
                actor_user_id=profile["id"],
                event_type=logistics_service.EVENT_RETURNED,
                source="telegram",
                warehouse_id=int(parts[2]),
                comment=" ".join(parts[3:]),
                idempotency_key=f"tg-return-{parts[1]}",
                photos=[],
            )
            return "Возврат зафиксирован."

        return "Неизвестная команда. Используйте /help."

    def _dispatch_callback(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
        data: str,
    ) -> tuple[str, dict[str, Any] | None]:
        if (
            data == "menu:handoff"
            or data.startswith("handoff_driver:")
            or data.startswith("handoff_dropoff:")
        ) and profile["role"] != "sender":
            return "Передача доступна только отправителю.", _main_menu(profile)
        if data == "menu:receive" and profile["role"] != "receiver":
            return "Приёмка доступна только получателю.", _main_menu(profile)

        if data == "menu:handoff":
            drivers = session.scalars(
                select(LogisticsDriver).where(LogisticsDriver.is_active.is_(True))
            ).all()
            if not drivers:
                return "Список водителей пуст.", _main_menu(profile)
            rows = [[(driver.full_name, f"handoff_driver:{driver.id}")] for driver in drivers[:20]]
            rows.append([("⬅️ Назад", "menu:main")])
            return "📦 Выберите водителя:", _inline_keyboard(rows)

        if data.startswith("handoff_driver:"):
            driver_id = int(data.split(":", 1)[1])
            warehouses = session.scalars(
                select(LogisticsWarehouse).where(LogisticsWarehouse.is_active.is_(True))
            ).all()
            rows = [
                [(warehouse.name, f"handoff_dropoff:{driver_id}:{warehouse.id}")]
                for warehouse in warehouses[:20]
            ]
            rows.append([("⬅️ Назад", "menu:main")])
            return "📍 Выберите точку выгрузки:", _inline_keyboard(rows)

        if data.startswith("handoff_dropoff:"):
            _, driver_id, warehouse_id = data.split(":")
            try:
                payload = logistics_service.create_draft(
                    session,
                    draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
                    actor_user_id=profile["id"],
                    warehouse_id=profile["default_warehouse_id"],
                    driver_id=int(driver_id),
                    default_dropoff_warehouse_id=int(warehouse_id),
                )
            except HTTPException as exc:
                return self._handle_create_draft_conflict(session, profile, chat_id, exc)
            self._upsert_session(
                session,
                chat_id=chat_id,
                telegram_user_id=profile["telegram_user_id"],
                actor_user_id=profile["id"],
                draft_id=payload["id"],
                draft_type=payload["draft_type"],
            )
            return (
                f"📦 Создан черновик передачи #{payload['id']}.\n"
                "Сканируйте штрихкоды сообщениями.\n"
                "Фото тоже можно добавить.\n"
                "Когда закончите, нажмите «Подтвердить».",
                _draft_controls(),
            )

        if data == "menu:receive":
            try:
                payload = logistics_service.create_draft(
                    session,
                    draft_type=logistics_service.DRAFT_TYPE_RECEIPT,
                    actor_user_id=profile["id"],
                    warehouse_id=profile["default_warehouse_id"],
                )
            except HTTPException as exc:
                return self._handle_create_draft_conflict(session, profile, chat_id, exc)
            self._upsert_session(
                session,
                chat_id=chat_id,
                telegram_user_id=profile["telegram_user_id"],
                actor_user_id=profile["id"],
                draft_id=payload["id"],
                draft_type=payload["draft_type"],
            )
            return (
                f"📥 Создан черновик приемки #{payload['id']}.\n"
                "Сканируйте штрихкоды сообщениями.\n"
                "Когда закончите, нажмите «Подтвердить».",
                _draft_controls(),
            )

        if data == "draft:confirm":
            current = self._get_session(session, chat_id)
            if current is None:
                return "Нет открытого draft.", _main_menu(profile)
            payload = logistics_service.confirm_draft(
                session,
                draft_id=current.draft_id,
                actor_user_id=profile["id"],
                comment=None,
                idempotency_key=f"tg-confirm-{chat_id}-{current.draft_id}",
                photos=[
                    {"telegram_file_id": photo.telegram_file_id, "comment": photo.comment}
                    for photo in current.photos
                ],
                source_channel="telegram",
            )
            self._drop_session(session, chat_id)
            return (
                f"✅ Черновик #{payload['draft_id']} подтвержден.\n"
                f"Событие: {payload['event_type']}\n"
                f"Обработано позиций: {payload['processed_count']}",
                _main_menu(profile),
            )

        if data == "draft:cancel":
            return self._cancel_current_draft_session(session, profile, chat_id)

        if data == "draft:status":
            return self._render_draft_status(session, profile, chat_id)

        if data == "draft:remove_picker" or data.startswith("draft:remove_picker:"):
            current = self._get_session(session, chat_id)
            if current is None:
                return "Нет открытого draft.", _main_menu(profile)
            draft = self._get_open_draft(session, current.draft_id)
            if draft is None or not draft.items:
                return "В draft пока нет позиций.", _draft_controls()
            page = int(data.rsplit(":", 1)[1]) if data != "draft:remove_picker" else 0
            return _remove_scan_picker(draft, page)

        if data.startswith("draft:remove:"):
            current = self._get_session(session, chat_id)
            if current is None:
                return "Нет открытого draft.", _main_menu(profile)
            item_id = int(data.rsplit(":", 1)[1])
            logistics_service.remove_scan_from_draft(
                session,
                draft_id=current.draft_id,
                item_id=item_id,
                actor_user_id=profile["id"],
            )
            return self._render_draft_status(
                session,
                profile,
                chat_id,
                headline="Ошибочный скан удалён.",
            )

        if data == "draft:incident_picker":
            current = self._get_session(session, chat_id)
            if current is None:
                return "Нет открытого draft.", _main_menu(profile)
            draft = self._get_open_draft(session, current.draft_id)
            if draft is None or not draft.items:
                return "В draft пока нет позиций.", _draft_controls()
            rows = [
                [
                    (
                        f"{item.transfer.document_number} | {item.barcode}",
                        f"draft:incident:{item.transfer_id}",
                    )
                ]
                for item in draft.items[:20]
            ]
            rows.append([("⬅️ Назад", "draft:status")])
            return "⚠️ Выберите позицию для инцидента:", _inline_keyboard(rows)

        if data == "draft:return_picker":
            current = self._get_session(session, chat_id)
            if current is None:
                return "Нет открытого draft.", _main_menu(profile)
            draft = self._get_open_draft(session, current.draft_id)
            if draft is None or not draft.items:
                return "В draft пока нет позиций.", _draft_controls()
            rows = [
                [
                    (
                        f"{item.transfer.document_number} | {item.barcode}",
                        f"draft:return:{item.transfer_id}",
                    )
                ]
                for item in draft.items[:20]
            ]
            rows.append([("⬅️ Назад", "draft:status")])
            return "↩️ Выберите позицию для возврата:", _inline_keyboard(rows)

        if data.startswith("draft:incident:"):
            transfer_id = int(data.rsplit(":", 1)[1])
            logistics_service.create_transfer_event(
                session,
                transfer_id=transfer_id,
                actor_user_id=profile["id"],
                event_type=logistics_service.EVENT_INCIDENT,
                source="telegram",
                warehouse_id=profile["default_warehouse_id"],
                comment="Зафиксировано через Telegram UI",
                idempotency_key=f"tg-draft-incident-{chat_id}-{transfer_id}",
                photos=[],
            )
            return self._render_draft_status(
                session,
                profile,
                chat_id,
                headline="Инцидент зафиксирован.",
            )

        if data.startswith("draft:return:"):
            transfer_id = int(data.rsplit(":", 1)[1])
            logistics_service.create_transfer_event(
                session,
                transfer_id=transfer_id,
                actor_user_id=profile["id"],
                event_type=logistics_service.EVENT_RETURNED,
                source="telegram",
                warehouse_id=profile["default_warehouse_id"],
                comment="Возврат зафиксирован через Telegram UI",
                idempotency_key=f"tg-draft-return-{chat_id}-{transfer_id}",
                photos=[],
            )
            return self._render_draft_status(
                session,
                profile,
                chat_id,
                headline="Возврат зафиксирован.",
            )

        if data == "menu:expected":
            rows = logistics_service.list_expected_deliveries(
                session,
                warehouse_id=profile["default_warehouse_id"],
            )
            if not rows:
                return "Ожидаемых доставок нет.", _main_menu(profile)
            return self._format_expected_deliveries(rows), _main_menu(profile)

        if data == "menu:monitor":
            rows = logistics_service.list_monitor(
                session, warehouse_id=profile["default_warehouse_id"]
            )
            if not rows:
                return "Перемещений для мониторинга нет.", _main_menu(profile)
            return self._format_monitor(rows), _main_menu(profile)

        if data == "menu:drivers":
            drivers = session.scalars(
                select(LogisticsDriver).where(LogisticsDriver.is_active.is_(True))
            ).all()
            if not drivers:
                return "Список водителей пуст.", _main_menu(profile)
            return (
                "👤 Водители:\n" + "\n".join(f"{row.id}: {row.full_name}" for row in drivers),
                _main_menu(profile),
            )

        if data == "menu:warehouses":
            warehouses = session.scalars(
                select(LogisticsWarehouse).where(LogisticsWarehouse.is_active.is_(True))
            ).all()
            return (
                "🏬 Склады:\n"
                + "\n".join(f"{row.id}: {row.name} ({row.kind})" for row in warehouses),
                _main_menu(profile),
            )

        if data == "menu:main":
            return "Главное меню:", _main_menu(profile)

        return "Неизвестное действие.", _main_menu(profile)

    def _handle_barcode(
        self,
        session: Session,
        profile: dict[str, Any],
        chat_id: int,
        barcode: str,
    ) -> str | tuple[str, dict[str, Any] | None]:
        current = self._get_session(session, chat_id)
        if current is None:
            try:
                draft = self._get_single_open_draft_for_actor(session, profile["id"])
            except RuntimeError as exc:
                return str(exc)
            if draft is None:
                return "Нет открытого draft. Используйте /handoff или /receive."
            current = self._upsert_session(
                session,
                chat_id=chat_id,
                telegram_user_id=profile["telegram_user_id"],
                actor_user_id=profile["id"],
                draft_id=draft.id,
                draft_type=draft.draft_type,
            )

        payload = logistics_service.add_scan_to_draft(
            session,
            draft_id=current.draft_id,
            actor_user_id=profile["id"],
            barcode=barcode,
        )
        return self._render_draft_status(
            session,
            profile,
            chat_id,
            headline=f"✅ Штрихкод принят. Черновик #{payload['id']}.",
        )


def main() -> None:  # pragma: no cover
    settings = get_settings()
    if not settings.logistics_bot_token:
        raise RuntimeError("LOGISTICS_BOT_TOKEN is not configured")
    bot = get_logistics_telegram_bot()
    try:
        bot.run_forever(timeout_seconds=settings.logistics_bot_poll_timeout_seconds)
    finally:
        bot.bot_api.close()


@lru_cache(maxsize=1)
def get_logistics_telegram_bot() -> LogisticsTelegramBot:
    settings = get_settings()
    if not settings.logistics_bot_token:
        raise RuntimeError("LOGISTICS_BOT_TOKEN is not configured")
    return LogisticsTelegramBot(
        bot_api=TelegramBotApi(settings.logistics_bot_token),
        engine=get_engine(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
