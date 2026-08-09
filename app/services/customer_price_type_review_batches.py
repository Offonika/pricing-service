"""Versioned acceptance batches for customer price-type portfolio review."""

from __future__ import annotations

import csv
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer_price_type import (
    CustomerPriceTypeProfile,
    CustomerPriceTypeReviewBatch,
    CustomerPriceTypeReviewBatchItem,
)

WORKING_BRONZE_BUCKET = "working_bronze"
REVIEW_QUEUE_BUCKET = "review_queue"
DEFAULT_BATCH_KEY = "reviewed-working-contracts-2026-07"
EXPECTED_BUCKET_COUNTS = {WORKING_BRONZE_BUCKET: 50, REVIEW_QUEUE_BUCKET: 32}
EXPECTED_REVIEW_PRICE_TYPE_COUNTS = {
    "Розница": 18,
    "2.Бронзовый бн": 3,
    "3.Серебряный": 1,
    "4.Золотой": 1,
    None: 9,
}


class CustomerPriceTypeReviewBatchConflict(RuntimeError):
    """Raised when a versioned batch key is reused for different source files."""


@dataclass(frozen=True, slots=True)
class ReviewBatchSourceRow:
    counterparty_code: str
    expected_bucket: str
    expected_price_type: str | None
    source_name: str
    source_row: int


@dataclass(frozen=True, slots=True)
class ReviewBatchImportResult:
    batch_key: str
    source_sha256: str
    total: int
    counts: dict[str, int]
    created: bool
    applied: bool


def _normalized_code(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _read_rows(path: Path, *, expected_bucket: str) -> list[ReviewBatchSourceRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter=";")
        if reader.fieldnames is None or "Код" not in reader.fieldnames:
            raise ValueError(f"{path.name}: CSV must contain the 'Код' column")
        rows: list[ReviewBatchSourceRow] = []
        for source_row, item in enumerate(reader, start=2):
            code = " ".join(str(item.get("Код") or "").split())
            if not code:
                raise ValueError(f"{path.name}:{source_row}: empty counterparty code")
            price_type = " ".join(str(item.get("Итоговый тип цены") or "").split()) or None
            if expected_bucket == WORKING_BRONZE_BUCKET and price_type != "2.Бронзовый":
                raise ValueError(
                    f"{path.name}:{source_row}: working bronze row has type {price_type!r}"
                )
            rows.append(
                ReviewBatchSourceRow(
                    counterparty_code=code,
                    expected_bucket=expected_bucket,
                    expected_price_type=price_type,
                    source_name=path.name,
                    source_row=source_row,
                )
            )
    return rows


def load_review_batch_sources(
    *, working_bronze_csv: Path, review_queue_csv: Path
) -> tuple[list[ReviewBatchSourceRow], str]:
    working = _read_rows(working_bronze_csv, expected_bucket=WORKING_BRONZE_BUCKET)
    review = _read_rows(review_queue_csv, expected_bucket=REVIEW_QUEUE_BUCKET)
    rows = [*working, *review]
    normalized_codes = [_normalized_code(item.counterparty_code) for item in rows]
    duplicates = sorted(code for code, count in Counter(normalized_codes).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate counterparty codes in review batch: {', '.join(duplicates)}")
    counts = Counter(item.expected_bucket for item in rows)
    if counts != EXPECTED_BUCKET_COUNTS:
        raise ValueError(
            "review batch must contain exactly 50 working_bronze and 32 review_queue rows"
        )
    review_price_types = Counter(item.expected_price_type for item in review)
    if review_price_types != EXPECTED_REVIEW_PRICE_TYPE_COUNTS:
        raise ValueError(
            "review_queue must contain exactly 18 retail, 3 bronze cashless, "
            "1 silver, 1 gold and 9 manual-review rows"
        )
    digest = hashlib.sha256()
    for label, path in (
        (WORKING_BRONZE_BUCKET, working_bronze_csv),
        (REVIEW_QUEUE_BUCKET, review_queue_csv),
    ):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return rows, digest.hexdigest()


def import_review_batch(
    session: Session,
    *,
    working_bronze_csv: Path,
    review_queue_csv: Path,
    batch_key: str = DEFAULT_BATCH_KEY,
    label: str = "Проверка рабочих договоров — июль 2026",
    apply: bool = False,
) -> ReviewBatchImportResult:
    rows, source_sha256 = load_review_batch_sources(
        working_bronze_csv=working_bronze_csv,
        review_queue_csv=review_queue_csv,
    )
    existing = session.scalar(
        select(CustomerPriceTypeReviewBatch).where(
            CustomerPriceTypeReviewBatch.batch_key == batch_key
        )
    )
    if existing is not None:
        if existing.source_sha256 != source_sha256:
            raise CustomerPriceTypeReviewBatchConflict(
                "batch_key is already bound to different source files"
            )
        counts = {
            str(key): int(value) for key, value in dict(existing.expected_counts or {}).items()
        }
        stored_items = session.scalars(
            select(CustomerPriceTypeReviewBatchItem).where(
                CustomerPriceTypeReviewBatchItem.batch_id == existing.id
            )
        ).all()
        stored_bucket_counts = Counter(item.expected_bucket for item in stored_items)
        stored_review_price_types = Counter(
            item.expected_price_type
            for item in stored_items
            if item.expected_bucket == REVIEW_QUEUE_BUCKET
        )
        if (
            counts != EXPECTED_BUCKET_COUNTS
            or stored_bucket_counts != EXPECTED_BUCKET_COUNTS
            or stored_review_price_types != EXPECTED_REVIEW_PRICE_TYPE_COUNTS
        ):
            raise CustomerPriceTypeReviewBatchConflict(
                "stored review batch is incomplete or inconsistent with its checksum"
            )
        return ReviewBatchImportResult(
            batch_key=batch_key,
            source_sha256=source_sha256,
            total=sum(counts.values()),
            counts=counts,
            created=False,
            applied=False,
        )

    normalized_to_rows: dict[str, list[CustomerPriceTypeProfile]] = defaultdict(list)
    codes = {_normalized_code(item.counterparty_code) for item in rows}
    profiles = session.scalars(
        select(CustomerPriceTypeProfile).where(
            CustomerPriceTypeProfile.counterparty_code.is_not(None)
        )
    ).all()
    for profile in profiles:
        normalized = _normalized_code(profile.counterparty_code)
        if normalized in codes:
            normalized_to_rows[normalized].append(profile)

    unresolved: list[str] = []
    ambiguous: list[str] = []
    resolved: list[tuple[ReviewBatchSourceRow, CustomerPriceTypeProfile]] = []
    for item in rows:
        matches = normalized_to_rows.get(_normalized_code(item.counterparty_code), [])
        if not matches:
            unresolved.append(item.counterparty_code)
        elif len(matches) > 1:
            ambiguous.append(item.counterparty_code)
        else:
            resolved.append((item, matches[0]))
    if unresolved or ambiguous:
        messages = []
        if unresolved:
            messages.append(f"unresolved codes: {', '.join(sorted(unresolved))}")
        if ambiguous:
            messages.append(f"ambiguous codes: {', '.join(sorted(ambiguous))}")
        raise ValueError("; ".join(messages))

    refs = [profile.counterparty_ref for _, profile in resolved]
    if len(set(refs)) != len(refs):
        raise ValueError("multiple source rows resolve to the same counterparty_ref")

    counts = dict(Counter(item.expected_bucket for item, _ in resolved))
    result = ReviewBatchImportResult(
        batch_key=batch_key,
        source_sha256=source_sha256,
        total=len(resolved),
        counts={str(key): int(value) for key, value in counts.items()},
        created=apply,
        applied=apply,
    )
    if not apply:
        return result

    batch = CustomerPriceTypeReviewBatch(
        batch_key=batch_key,
        label=label,
        source_sha256=source_sha256,
        source_files=[working_bronze_csv.name, review_queue_csv.name],
        expected_counts=result.counts,
        status="ready",
    )
    session.add(batch)
    session.flush()
    session.add_all(
        [
            CustomerPriceTypeReviewBatchItem(
                batch_id=batch.id,
                counterparty_ref=profile.counterparty_ref,
                counterparty_code=item.counterparty_code,
                expected_bucket=item.expected_bucket,
                expected_price_type=item.expected_price_type,
                source_name=item.source_name,
                source_row=item.source_row,
            )
            for item, profile in resolved
        ]
    )
    session.commit()
    return result
