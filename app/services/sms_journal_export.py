"""Read-only sensitive export service for the encrypted SMS journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sms_journal import SmsJournalAttempt
from app.services.sms_journal import SmsJournalCipher


@dataclass(frozen=True)
class SmsJournalExportRow:
    event_id: str
    created_at: datetime
    source_system: str
    source_entity_type: str
    source_entity_id: str
    event_type: str
    actor_id: str | None
    recipient_phone_masked: str
    message_text: str
    message_fingerprint: str
    character_count: int
    encoding: str
    estimated_segments: int
    provider: str
    provider_message_id: str | None
    send_status: str
    delivery_status: str
    provider_error_code: str | None
    attempt_number: int
    sent_at: datetime | None
    delivered_at: datetime | None
    billed_segments: int | None
    unit_price: Decimal | None
    total_cost: Decimal | None
    reconciliation_period: str | None


def load_sms_journal_export_rows(
    session: Session,
    cipher: SmsJournalCipher,
    *,
    created_from: datetime,
    created_to: datetime,
    source_system: str | None = None,
    limit: int = 50_000,
) -> list[SmsJournalExportRow]:
    if created_to <= created_from:
        raise ValueError("created_to must be later than created_from")
    if limit < 1 or limit > 50_000:
        raise ValueError("limit must be between 1 and 50000")

    query = (
        select(SmsJournalAttempt)
        .where(
            SmsJournalAttempt.created_at >= created_from,
            SmsJournalAttempt.created_at < created_to,
        )
        .order_by(SmsJournalAttempt.created_at, SmsJournalAttempt.id)
        .limit(limit)
    )
    if source_system:
        query = query.where(SmsJournalAttempt.source_system == source_system)

    rows = session.scalars(query).all()
    return [
        SmsJournalExportRow(
            event_id=str(row.id),
            created_at=row.created_at,
            source_system=row.source_system,
            source_entity_type=row.source_entity_type,
            source_entity_id=row.source_entity_id,
            event_type=row.event_type,
            actor_id=row.actor_id,
            recipient_phone_masked=row.recipient_phone_masked,
            message_text=cipher.decrypt(row.message_text_encrypted),
            message_fingerprint=row.message_fingerprint,
            character_count=row.character_count,
            encoding=row.encoding,
            estimated_segments=row.estimated_segments,
            provider=row.provider,
            provider_message_id=row.provider_message_id,
            send_status=row.send_status,
            delivery_status=row.delivery_status,
            provider_error_code=row.provider_error_code,
            attempt_number=row.attempt_number,
            sent_at=row.sent_at,
            delivered_at=row.delivered_at,
            billed_segments=row.billed_segments,
            unit_price=row.unit_price,
            total_cost=row.total_cost,
            reconciliation_period=row.reconciliation_period,
        )
        for row in rows
    ]
