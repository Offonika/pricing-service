from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import logistics_bot as logistics_bot_api
from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.telegram.logistics_bot import LogisticsTelegramBot
from tests.test_logistics_bot import FakeBotApi, _message, seed_data, setup_db


class FakeBot:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.info_calls = 0
        self.register_calls: list[tuple[str, str | None]] = []
        self.delete_calls: list[bool] = []

    def handle_update(self, payload: dict) -> None:
        self.updates.append(payload)

    @property
    def bot_api(self):
        return self

    def get_webhook_info(self) -> dict:
        self.info_calls += 1
        return {"ok": True, "result": {"url": "https://example.com/webhook"}}

    def set_webhook(self, url: str, secret_token: str | None = None) -> dict:
        self.register_calls.append((url, secret_token))
        return {"ok": True, "result": True}

    def delete_webhook(self, drop_pending_updates: bool = False) -> dict:
        self.delete_calls.append(drop_pending_updates)
        return {"ok": True, "result": True}


def test_logistics_bot_webhook_api(monkeypatch) -> None:
    fake_bot = FakeBot()

    monkeypatch.setenv("LOGISTICS_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("LOGISTICS_BOT_WEBHOOK_SECRET", "secret-token")
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", "logistics-token")
    get_settings.cache_clear()
    monkeypatch.setattr(logistics_bot_api, "get_logistics_telegram_bot", lambda: fake_bot)

    client = TestClient(app)

    health = client.get("/api/logistics/bot/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["webhook_secret_configured"] is True

    unauthorized = client.post("/api/logistics/bot/webhook", json={"update_id": 1})
    assert unauthorized.status_code == 401

    authorized = client.post(
        "/api/logistics/bot/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        json={"update_id": 2, "message": {"chat": {"id": 123}}},
    )
    assert authorized.status_code == 200
    assert authorized.json()["status"] == "ok"
    assert fake_bot.updates == [{"update_id": 2, "message": {"chat": {"id": 123}}}]

    get_settings.cache_clear()


def test_logistics_bot_operational_api(monkeypatch) -> None:
    fake_bot = FakeBot()
    engine, path = setup_db()
    seed_data(engine)

    runtime_bot = LogisticsTelegramBot(FakeBotApi(), engine)
    runtime_bot.handle_update(_message(101, "sender_user", "/handoff 1 2"))
    runtime_bot.handle_update(_message(101, "sender_user", "UNKNOWN-BARCODE"))
    runtime_bot.handle_update(_message(101, "sender_user", "BC-0001"))

    monkeypatch.setenv("LOGISTICS_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("LOGISTICS_BOT_WEBHOOK_SECRET", "secret-token")
    monkeypatch.setenv("LOGISTICS_BOT_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setenv("LOGISTICS_INTERNAL_API_TOKEN", "logistics-token")
    monkeypatch.setenv("RETURN_SCHEME_INTERNAL_API_TOKEN", "return-scheme-token")
    get_settings.cache_clear()
    monkeypatch.setattr(logistics_bot_api, "get_logistics_telegram_bot", lambda: fake_bot)

    client = TestClient(app)
    headers = {"Authorization": "Bearer logistics-token"}

    unauthorized = client.get("/api/logistics/bot/sessions")
    assert unauthorized.status_code == 401

    wrong_token = client.get(
        "/api/logistics/bot/sessions",
        headers={"Authorization": "Bearer return-scheme-token"},
    )
    assert wrong_token.status_code == 401

    info = client.get("/api/logistics/bot/webhook/info", headers=headers)
    assert info.status_code == 200
    assert info.json()["ok"] is True
    assert fake_bot.info_calls == 1

    register = client.post("/api/logistics/bot/webhook/register", headers=headers)
    assert register.status_code == 200
    assert fake_bot.register_calls == [("https://example.com/webhook", "secret-token")]

    delete = client.post("/api/logistics/bot/webhook/delete", headers=headers)
    assert delete.status_code == 200
    assert fake_bot.delete_calls == [False]

    app.dependency_overrides = {}

    def override_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    sessions = client.get("/api/logistics/bot/sessions", headers=headers)
    assert sessions.status_code == 200
    sessions_json = sessions.json()
    payload = sessions_json["items"]
    assert sessions_json["total"] == 1
    assert sessions_json["limit"] == 50
    assert sessions_json["offset"] == 0
    assert len(payload) == 1
    assert payload[0]["draft_type"] == "handoff"
    assert payload[0]["scan_error_count"] == 1
    assert payload[0]["item_count"] == 1
    assert payload[0]["recent_errors"]
    assert payload[0]["items"][0]["barcode"] == "BC-0001"

    filtered = client.get(
        "/api/logistics/bot/sessions?draft_type=handoff&has_errors=true&actor_user_name=sender",
        headers=headers,
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert len(filtered.json()["items"]) == 1

    filtered_empty = client.get(
        "/api/logistics/bot/sessions?draft_type=receipt",
        headers=headers,
    )
    assert filtered_empty.status_code == 200
    assert filtered_empty.json()["total"] == 0
    assert filtered_empty.json()["items"] == []

    session_id = payload[0]["id"]
    close = client.post(f"/api/logistics/bot/sessions/{session_id}/close", headers=headers)
    assert close.status_code == 200
    assert close.json() == {"status": "closed", "session_id": session_id}

    sessions_after_close = client.get("/api/logistics/bot/sessions", headers=headers)
    assert sessions_after_close.status_code == 200
    assert sessions_after_close.json()["total"] == 0
    assert sessions_after_close.json()["items"] == []

    app.dependency_overrides = {}
    get_settings.cache_clear()
    engine.dispose()
