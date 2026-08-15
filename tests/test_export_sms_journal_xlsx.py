from __future__ import annotations

import base64
import json
import stat
from datetime import UTC, date, datetime

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.services.sms_journal import (
    SmsJournalCipher,
    SmsJournalConfigurationError,
    SmsJournalService,
)
from app.services.sms_journal_export import load_sms_journal_export_rows
from tasks.export_sms_journal_xlsx import export_sms_journal_xlsx, validate_export_request


def _cipher() -> SmsJournalCipher:
    encryption_key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    return SmsJournalCipher(
        encryption_key,
        "sms-journal-export-test-phone-hash-key-at-least-32-bytes",
    )


def _create_attempt(session: Session, *, message_text: str) -> None:
    service = SmsJournalService(session, _cipher())
    service.create_attempt(
        idempotency_key="task2662-export-create-0001",
        payload={
            "event_id": None,
            "created_at": datetime(2026, 8, 11, 9, 30, tzinfo=UTC),
            "source_system": "ut10.3",
            "source_entity_type": "customer_order",
            "source_entity_id": "TASK-2662-EXPORT",
            "event_type": "order_sms_shadow",
            "actor_id": "test-operator",
            "recipient_phone": "+70000002662",
            "message_text": message_text,
            "secret_kind": "none",
            "redaction_values": [],
            "provider": "megafon",
            "sender_name": None,
            "attempt_number": 1,
        },
    )
    session.commit()


def test_cipher_decrypts_and_rejects_invalid_ciphertext() -> None:
    cipher = _cipher()
    encrypted = cipher.encrypt("ТЕСТ 2662")

    assert cipher.decrypt(encrypted) == "ТЕСТ 2662"
    with pytest.raises(SmsJournalConfigurationError, match="ciphertext is invalid"):
        cipher.decrypt(base64.urlsafe_b64encode(b"not-a-valid-ciphertext").decode("ascii"))


def test_sensitive_export_requires_allowlist_confirmation_and_short_period() -> None:
    validate_export_request(
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        actor="owner",
        allowed_actors={"owner"},
        confirmed=True,
    )

    with pytest.raises(PermissionError, match="explicit confirmation"):
        validate_export_request(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 1),
            actor="owner",
            allowed_actors={"owner"},
            confirmed=False,
        )
    with pytest.raises(PermissionError, match="not allowed"):
        validate_export_request(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 1),
            actor="outsider",
            allowed_actors={"owner"},
            confirmed=True,
        )
    with pytest.raises(ValueError, match="must not exceed 31 days"):
        validate_export_request(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 9, 1),
            actor="owner",
            allowed_actors={"owner"},
            confirmed=True,
        )


def test_xlsx_export_contains_text_but_only_masked_phone_and_safe_audit(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sms-export.db'}")
    Base.metadata.create_all(engine)
    formula_like_text = '=HYPERLINK("https://invalid.example","ТЕСТ 2662")'
    with Session(engine) as session:
        _create_attempt(session, message_text=formula_like_text)
        rows = load_sms_journal_export_rows(
            session,
            _cipher(),
            created_from=datetime(2026, 8, 11, 0, 0),
            created_to=datetime(2026, 8, 12, 0, 0),
        )

    output = tmp_path / "sms-journal.xlsx"
    xlsx_path, audit_path = export_sms_journal_xlsx(
        rows,
        output_path=output,
        actor="owner",
        date_from=date(2026, 8, 11),
        date_to=date(2026, 8, 11),
        source_system=None,
        exported_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
    )

    assert stat.S_IMODE(xlsx_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    workbook = load_workbook(xlsx_path, data_only=False)
    worksheet = workbook["SMS"]
    assert worksheet["H2"].value == "+***2662"
    assert worksheet["I2"].value == formula_like_text
    assert worksheet["I2"].data_type == "s"
    assert "+70000002662" not in " ".join(str(cell.value or "") for cell in worksheet[2])
    summary = workbook["Сводка"]
    assert summary["D2"].value == formula_like_text
    assert summary["D2"].data_type == "s"
    assert summary["G2"].value == 1
    assert summary["H2"].value == 1

    audit_text = audit_path.read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit["actor"] == "owner"
    assert audit["row_count"] == 1
    assert len(audit["xlsx_sha256"]) == 64
    assert formula_like_text not in audit_text
    assert "+70000002662" not in audit_text

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        export_sms_journal_xlsx(
            rows,
            output_path=output,
            actor="owner",
            date_from=date(2026, 8, 11),
            date_to=date(2026, 8, 11),
            source_system=None,
        )
