from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.api.dependencies import get_db
from app.core.config import Settings, get_settings
from app.main import app
from app.models.site_service_requests import SiteServiceRequestCase
from app.services.site_service_requests_ui_auth import (
    SiteServiceRequestUiSession,
    require_site_service_request_ui_session,
)


@contextmanager
def _ui_dependencies(db_session, *, item_id: int = 391):
    settings = Settings(
        site_service_requests_ui_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
        site_service_requests_command_attachments_enabled=True,
        site_service_requests_event_encryption_key=base64.urlsafe_b64encode(b"u" * 32).decode(
            "ascii"
        ),
    )

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_site_service_request_ui_session] = lambda: (
        SiteServiceRequestUiSession(
            domain="portal.example",
            member_id="member-3223",
            user_id=131016,
            user_name="Тимур Тибилов",
            is_admin=False,
            item_id=item_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def test_ui_reply_note_conversation_and_item_scope(client, db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=760,
        source_kind="site_ticket",
        source_key="site-support-ticket:760",
        bitrix_item_id=391,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    with _ui_dependencies(db_session):
        reply = client.post(
            "/api/site-service-requests/ui/items/391/replies",
            data={"clientRequestId": "request-3223", "text": "Ответ клиенту"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        duplicate = client.post(
            "/api/site-service-requests/ui/items/391/replies",
            data={"clientRequestId": "request-3223", "text": "Ответ клиенту"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        conflict = client.post(
            "/api/site-service-requests/ui/items/391/replies",
            data={"clientRequestId": "request-3223", "text": "Другой ответ"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        note = client.post(
            "/api/site-service-requests/ui/items/391/notes",
            json={"clientRequestId": "note-3223", "text": "Заметка для коллег"},
        )
        conversation = client.get("/api/site-service-requests/ui/items/391/conversation")
        forbidden = client.get("/api/site-service-requests/ui/items/392/conversation")

    assert reply.status_code == 200
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert conflict.status_code == 409
    assert note.status_code == 200
    assert conversation.status_code == 200
    assert [row["direction"] for row in conversation.json()["messages"]] == [
        "outbound",
        "internal",
    ]
    assert forbidden.status_code == 403
