from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CounterpartyDuplicateCase

DEFAULT_COUNTERPARTY_DUPLICATE_SQL = """
SELECT
    counterparty_ref,
    counterparty_name,
    phone,
    email,
    tax_id,
    responsible_code,
    updated_at
FROM counterparty_duplicate_source
WHERE updated_at >= :window_start
  AND updated_at < :window_end
"""

RISK_P1 = "P1"
RISK_P2 = "P2"
DELIVERY_PENDING = "pending"
DELIVERY_SENT = "sent"
DELIVERY_ACKED = "acked"
DELIVERY_RETRY = "retry"

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CONFIRMED_DUPLICATE = "confirmed_duplicate"
STATUS_FALSE_POSITIVE = "false_positive"
STATUS_CLOSED = "closed"

REASON_PHONE = "phone"
REASON_EMAIL = "email"
REASON_TAX_ID = "inn"
REASON_NAME_PHONE = "name_phone"
REASON_NAME_EMAIL = "name_email"
REASON_NAME_FUZZY = "name_fuzzy"


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex().upper()
    text = str(value).strip()
    return text or None


def normalize_email(value: Any) -> str | None:
    text = _clean_string(value)
    if not text:
        return None
    return text.lower()


def normalize_name(value: Any) -> str | None:
    text = _clean_string(value)
    if not text:
        return None
    normalized = re.sub(r"[\s\-_()\"'`]+", " ", text, flags=re.UNICODE)
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized, flags=re.UNICODE)
    return normalized.lower().strip() or None


def normalize_tax_id(value: Any) -> str | None:
    text = _clean_string(value)
    if not text:
        return None
    digits = re.sub(r"\D+", "", text)
    return digits or None


def normalize_phone(value: Any) -> str | None:
    text = _clean_string(value)
    if not text:
        return None
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 10:
        return None
    return f"+{digits}"


def _normalize_updated_at(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else parsed.astimezone(UTC).replace(tzinfo=None)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class CounterpartySnapshotRecord:
    counterparty_ref: str
    counterparty_name: str | None
    phone: str | None
    email: str | None
    tax_id: str | None
    responsible_code: str | None
    updated_at: datetime | None
    normalized_phone: str | None
    normalized_email: str | None
    normalized_tax_id: str | None
    normalized_name: str | None

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> CounterpartySnapshotRecord:
        name = _clean_string(row.get("counterparty_name"))
        return cls(
            counterparty_ref=_clean_string(row.get("counterparty_ref")) or "",
            counterparty_name=name,
            phone=_clean_string(row.get("phone")),
            email=_clean_string(row.get("email")),
            tax_id=_clean_string(row.get("tax_id")),
            responsible_code=_clean_string(row.get("responsible_code")),
            updated_at=_normalize_updated_at(row.get("updated_at")),
            normalized_phone=normalize_phone(row.get("phone")),
            normalized_email=normalize_email(row.get("email")),
            normalized_tax_id=normalize_tax_id(row.get("tax_id")),
            normalized_name=normalize_name(name),
        )

    def to_candidate_payload(self) -> dict[str, Any]:
        return {
            "counterparty_ref": self.counterparty_ref,
            "counterparty_name": self.counterparty_name,
            "phone": self.normalized_phone or self.phone,
            "email": self.normalized_email or self.email,
            "tax_id": self.normalized_tax_id or self.tax_id,
            "responsible_code": self.responsible_code,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(slots=True)
class DetectedCounterpartyDuplicateCase:
    dedupe_key: str
    risk_level: str
    reason_codes: list[str]
    candidate_records: list[dict[str, Any]]
    responsible_code: str | None
    status: str
    sla_deadline_at: datetime
    summary_text: str
    source_hash: str


class CounterpartyDuplicateExtractor:
    def __init__(self, engine: Engine, *, sql_text: str | None = None):
        self.engine = engine
        self.sql_text = sql_text or DEFAULT_COUNTERPARTY_DUPLICATE_SQL

    def fetch_rows(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[CounterpartySnapshotRecord]:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            rows = conn.execute(
                text(self.sql_text),
                {"window_start": window_start, "window_end": window_end},
            )
            return [CounterpartySnapshotRecord.from_mapping(dict(row._mapping)) for row in rows]


class _DisjointSet:
    def __init__(self, items: Sequence[str]):
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _group_duplicates(
    records: Sequence[CounterpartySnapshotRecord],
    *,
    p2_enabled: bool,
    fuzzy_threshold: float,
) -> list[DetectedCounterpartyDuplicateCase]:
    valid_records = [record for record in records if record.counterparty_ref]
    if len(valid_records) < 2:
        return []

    dsu = _DisjointSet([record.counterparty_ref for record in valid_records])
    phone_index: dict[str, list[CounterpartySnapshotRecord]] = defaultdict(list)
    email_index: dict[str, list[CounterpartySnapshotRecord]] = defaultdict(list)
    tax_index: dict[str, list[CounterpartySnapshotRecord]] = defaultdict(list)

    for record in valid_records:
        if record.normalized_phone:
            phone_index[record.normalized_phone].append(record)
        if record.normalized_email:
            email_index[record.normalized_email].append(record)
        if record.normalized_tax_id:
            tax_index[record.normalized_tax_id].append(record)

    for buckets in (phone_index, email_index, tax_index):
        for matches in buckets.values():
            if len(matches) < 2:
                continue
            anchor = matches[0].counterparty_ref
            for item in matches[1:]:
                dsu.union(anchor, item.counterparty_ref)

    if p2_enabled:
        named_records = [record for record in valid_records if record.normalized_name]
        for index, left in enumerate(named_records):
            for right in named_records[index + 1 :]:
                if left.counterparty_ref == right.counterparty_ref:
                    continue
                if left.normalized_name != right.normalized_name:
                    continue
                if left.normalized_phone and left.normalized_phone == right.normalized_phone:
                    dsu.union(left.counterparty_ref, right.counterparty_ref)
                elif left.normalized_email and left.normalized_email == right.normalized_email:
                    dsu.union(left.counterparty_ref, right.counterparty_ref)
                elif fuzzy_threshold <= 1.0 and (
                    left.normalized_phone
                    or left.normalized_email
                    or right.normalized_phone
                    or right.normalized_email
                ):
                    dsu.union(left.counterparty_ref, right.counterparty_ref)

    groups: dict[str, list[CounterpartySnapshotRecord]] = defaultdict(list)
    for record in valid_records:
        groups[dsu.find(record.counterparty_ref)].append(record)

    detected: list[DetectedCounterpartyDuplicateCase] = []
    settings = get_settings()
    for members in groups.values():
        if len(members) < 2:
            continue

        duplicate_signals: list[str] = []
        reason_codes: set[str] = set()
        for token, matches in phone_index.items():
            member_refs = {item.counterparty_ref for item in matches}
            if token and len(member_refs.intersection({m.counterparty_ref for m in members})) >= 2:
                duplicate_signals.append(f"phone:{token}")
                reason_codes.add(REASON_PHONE)
        for token, matches in email_index.items():
            member_refs = {item.counterparty_ref for item in matches}
            if token and len(member_refs.intersection({m.counterparty_ref for m in members})) >= 2:
                duplicate_signals.append(f"email:{token}")
                reason_codes.add(REASON_EMAIL)
        for token, matches in tax_index.items():
            member_refs = {item.counterparty_ref for item in matches}
            if token and len(member_refs.intersection({m.counterparty_ref for m in members})) >= 2:
                duplicate_signals.append(f"tax:{token}")
                reason_codes.add(REASON_TAX_ID)

        if p2_enabled:
            names = [m.normalized_name for m in members if m.normalized_name]
            if len(set(names)) == 1 and names:
                if any(m.normalized_phone for m in members):
                    reason_codes.add(REASON_NAME_PHONE)
                if any(m.normalized_email for m in members):
                    reason_codes.add(REASON_NAME_EMAIL)
                if fuzzy_threshold <= 1.0:
                    reason_codes.add(REASON_NAME_FUZZY)

        if not reason_codes:
            continue

        risk_level = (
            RISK_P1 if {REASON_PHONE, REASON_EMAIL, REASON_TAX_ID} & reason_codes else RISK_P2
        )
        ordered_members = sorted(members, key=lambda item: item.counterparty_ref)
        candidate_records = [item.to_candidate_payload() for item in ordered_members]
        dedupe_key = _stable_hash(
            sorted(set(duplicate_signals)) or [m["counterparty_ref"] for m in candidate_records]
        )
        source_hash = _stable_hash(
            {
                "risk_level": risk_level,
                "reason_codes": sorted(reason_codes),
                "candidate_records": candidate_records,
            }
        )
        responsible_code = next(
            (item.responsible_code for item in ordered_members if item.responsible_code),
            settings.counterparty_duplicate_owner_code,
        )
        summary_text = (
            f"Найден дубль контрагента ({risk_level}) по причинам "
            f"{', '.join(sorted(reason_codes))}: {', '.join(item.counterparty_ref for item in ordered_members)}"
        )
        detected.append(
            DetectedCounterpartyDuplicateCase(
                dedupe_key=dedupe_key,
                risk_level=risk_level,
                reason_codes=sorted(reason_codes),
                candidate_records=candidate_records,
                responsible_code=responsible_code,
                status=STATUS_NEW,
                sla_deadline_at=datetime.now()
                + timedelta(hours=settings.counterparty_duplicate_sla_hours),
                summary_text=summary_text,
                source_hash=source_hash,
            )
        )
    return sorted(detected, key=lambda item: (item.risk_level, item.dedupe_key))


def detect_counterparty_duplicate_cases(
    records: Sequence[CounterpartySnapshotRecord],
    *,
    p2_enabled: bool = False,
    fuzzy_threshold: float = 0.9,
) -> list[DetectedCounterpartyDuplicateCase]:
    return _group_duplicates(records, p2_enabled=p2_enabled, fuzzy_threshold=fuzzy_threshold)


def _load_counterparty_duplicate_sql() -> str:
    settings = get_settings()
    if settings.counterparty_duplicate_sql:
        return settings.counterparty_duplicate_sql
    if settings.counterparty_duplicate_sql_file:
        return Path(settings.counterparty_duplicate_sql_file).read_text(encoding="utf-8")
    return DEFAULT_COUNTERPARTY_DUPLICATE_SQL


def build_counterparty_duplicate_payload(case: CounterpartyDuplicateCase) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "dedupe_key": case.dedupe_key,
        "detected_at": case.detected_at,
        "risk_level": case.risk_level,
        "reason_codes": case.reason_codes,
        "records": case.candidate_records,
        "responsible_code": case.responsible_code,
        "status": case.status,
        "sla_deadline_at": case.sla_deadline_at,
        "summary_text": case.summary_text,
        "source_hash": case.source_hash,
        "delivery_state": case.delivery_state,
        "external_case_id": case.external_case_id,
        "external_status": case.external_status,
        "external_url": case.external_url,
    }


def upsert_counterparty_duplicate_cases(
    session: Session,
    detected_cases: Sequence[DetectedCounterpartyDuplicateCase],
    *,
    detected_at: datetime,
    anti_duplicate_window_hours: int,
) -> dict[str, list[CounterpartyDuplicateCase]]:
    created: list[CounterpartyDuplicateCase] = []
    pending: list[CounterpartyDuplicateCase] = []
    existing_rows = {
        row.dedupe_key: row
        for row in session.execute(select(CounterpartyDuplicateCase)).scalars().all()
    }
    anti_duplicate_cutoff = detected_at - timedelta(hours=anti_duplicate_window_hours)

    for item in detected_cases:
        row = existing_rows.get(item.dedupe_key)
        if row is None:
            row = CounterpartyDuplicateCase(
                dedupe_key=item.dedupe_key,
                detected_at=detected_at,
                last_seen_at=detected_at,
                risk_level=item.risk_level,
                reason_codes=item.reason_codes,
                candidate_records=item.candidate_records,
                responsible_code=item.responsible_code,
                status=item.status,
                sla_deadline_at=item.sla_deadline_at,
                summary_text=item.summary_text,
                source_hash=item.source_hash,
                delivery_state=DELIVERY_PENDING,
            )
            session.add(row)
            session.flush()
            existing_rows[item.dedupe_key] = row
            created.append(row)
            pending.append(row)
            continue

        row.last_seen_at = detected_at
        if row.source_hash != item.source_hash:
            row.risk_level = item.risk_level
            row.reason_codes = item.reason_codes
            row.candidate_records = item.candidate_records
            row.responsible_code = item.responsible_code
            row.summary_text = item.summary_text
            row.source_hash = item.source_hash
            row.sla_deadline_at = item.sla_deadline_at
            row.status = STATUS_NEW
            row.delivery_state = DELIVERY_PENDING
            row.delivered_at = None
            pending.append(row)
            continue

        if row.delivery_state in {DELIVERY_PENDING, DELIVERY_RETRY}:
            pending.append(row)
            continue

        if row.delivery_state == DELIVERY_SENT and (
            row.last_notified_at is None or row.last_notified_at < anti_duplicate_cutoff
        ):
            row.delivery_state = DELIVERY_RETRY
            pending.append(row)

    return {"new": created, "pending": pending}


def list_pending_counterparty_duplicate_cases(session: Session) -> list[CounterpartyDuplicateCase]:
    return (
        session.execute(
            select(CounterpartyDuplicateCase)
            .where(CounterpartyDuplicateCase.delivery_state.in_([DELIVERY_PENDING, DELIVERY_RETRY]))
            .order_by(
                CounterpartyDuplicateCase.risk_level.asc(),
                CounterpartyDuplicateCase.detected_at.asc(),
            )
        )
        .scalars()
        .all()
    )


def get_counterparty_duplicate_case(
    session: Session, case_id: int
) -> CounterpartyDuplicateCase | None:
    return session.get(CounterpartyDuplicateCase, case_id)


def acknowledge_counterparty_duplicate_case(
    session: Session,
    *,
    case_id: int,
    delivered_at: datetime | None = None,
    external_case_id: str | None = None,
    external_status: str | None = None,
    external_url: str | None = None,
    status: str | None = None,
) -> CounterpartyDuplicateCase:
    row = session.get(CounterpartyDuplicateCase, case_id)
    if row is None:
        raise ValueError(f"counterparty duplicate case {case_id} not found")
    timestamp = delivered_at or datetime.now()
    row.delivery_state = DELIVERY_ACKED
    row.delivered_at = timestamp
    row.last_notified_at = timestamp
    if external_case_id is not None:
        row.external_case_id = external_case_id
    if external_status is not None:
        row.external_status = external_status
    if external_url is not None:
        row.external_url = external_url
    if status is not None:
        row.status = status
    session.add(row)
    session.flush()
    return row


def build_counterparty_duplicate_health(session: Session) -> dict[str, Any]:
    latest_detected_at = session.execute(
        select(func.max(CounterpartyDuplicateCase.last_seen_at))
    ).scalar_one_or_none()
    pending_count = session.execute(
        select(func.count())
        .select_from(CounterpartyDuplicateCase)
        .where(CounterpartyDuplicateCase.delivery_state.in_([DELIVERY_PENDING, DELIVERY_RETRY]))
    ).scalar_one()
    stale_cutoff = datetime.now() - timedelta(
        hours=get_settings().counterparty_duplicate_detection_window_hours * 2
    )
    freshness_status = (
        "missing"
        if latest_detected_at is None
        else "stale" if latest_detected_at < stale_cutoff else "fresh"
    )
    source_status = "empty" if latest_detected_at is None else "ready"
    status = "healthy" if freshness_status == "fresh" else "degraded"
    return {
        "status": status,
        "freshness_status": freshness_status,
        "source_status": source_status,
        "components": [
            {
                "component": "counterparty_duplicate_cases",
                "freshness_status": freshness_status,
                "source_status": source_status,
                "latest_detected_at": latest_detected_at,
                "metrics": {"pending_cases": pending_count},
            }
        ],
    }


def run_counterparty_duplicate_detection(
    session: Session,
    *,
    run_at: datetime | None = None,
    onec_engine: Engine | None = None,
    sql_text: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.counterparty_duplicate_enabled and sql_text is None:
        return {"enabled": False, "records": 0, "detected": 0, "new": 0, "pending": 0}

    anchor = run_at or datetime.now()
    window_start = anchor - timedelta(hours=settings.counterparty_duplicate_detection_window_hours)
    extractor = CounterpartyDuplicateExtractor(
        onec_engine or create_engine(settings.onec_database_url or "sqlite:///:memory:"),
        sql_text=sql_text or _load_counterparty_duplicate_sql(),
    )
    records = extractor.fetch_rows(window_start=window_start, window_end=anchor)
    detected = detect_counterparty_duplicate_cases(
        records,
        p2_enabled=settings.counterparty_duplicate_p2_enabled,
        fuzzy_threshold=settings.counterparty_duplicate_fuzzy_threshold,
    )
    persisted = upsert_counterparty_duplicate_cases(
        session,
        detected,
        detected_at=anchor,
        anti_duplicate_window_hours=settings.counterparty_duplicate_antiduplicate_hours,
    )
    return {
        "enabled": True,
        "records": len(records),
        "detected": len(detected),
        "new": len(persisted["new"]),
        "pending": len(persisted["pending"]),
        "window_start": window_start,
        "window_end": anchor,
    }
