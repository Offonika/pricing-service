"""Build UT 10.3 property updates for nomenclature rows with empty 1C subject."""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Product
from app.services.competitor_category import DEFAULT_FALLBACK_CATEGORY, CategoryClassifier
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    DEFAULT_SOURCE,
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)
from tasks.sync_onec_product_catalog import (
    detect_item_folder_value,
    fetch_general_catalog_item_ids,
    fetch_onec_products,
    fetch_subject_values,
)

logger = logging.getLogger("tasks.build_missing_onec_subject_updates")

SUBJECT_PROPERTY_NAME = "Предмет"
UNKNOWN_SUBJECT_VALUES = {
    "unknown",
    "undefined",
    "неизвестно",
    "не определено",
    "н/д",
    "нет",
    "-",
}


class SubjectClassifier(Protocol):
    def classify(self, name: str | None) -> str | None: ...


@dataclass(frozen=True)
class SubjectUpdateBuildResult:
    rows: tuple[NomenclaturePropertyUpdateRow, ...]
    skipped: tuple[dict[str, str], ...]
    source_counts: dict[str, int]
    subject_counts: dict[str, int]


def load_missing_onec_subject_candidates(engine_onec) -> list[dict[str, str]]:
    """Return live 1C general-catalog items where property Предмет is not filled."""

    item_folder_value = detect_item_folder_value(engine_onec)
    allowed_item_ids = fetch_general_catalog_item_ids(engine_onec, item_folder_value)
    products = fetch_onec_products(engine_onec, item_folder_value, sorted(allowed_item_ids))
    filled_subject_by_article = fetch_subject_values(
        engine_onec,
        item_folder_value,
        sorted(allowed_item_ids),
    )

    candidates: list[dict[str, str]] = []
    for row in products:
        article = _clean(row.get("article"))
        if not article or article in filled_subject_by_article:
            continue
        code_1c = _clean(row.get("code_1c"))
        name = _clean(row.get("name"))
        if not code_1c or not name:
            continue
        candidates.append(
            {
                "article": article,
                "nomenclature_code": code_1c,
                "name": name,
            }
        )
    return sorted(candidates, key=lambda item: (item["article"], item["nomenclature_code"]))


def load_onec_subject_catalog_values(engine_onec) -> set[str]:
    """Read existing value names for 1C property Предмет."""

    query = text("""
        SELECT DISTINCT LTRIM(RTRIM(v._Description)) AS subject_value
        FROM _Reference42 v
        JOIN _Chrc401 ch ON ch._IDRRef = v._OwnerIDRRef
        WHERE ch._Description = 'Предмет'
          AND v._Marked = 0
          AND LTRIM(RTRIM(v._Description)) <> ''
        ORDER BY LTRIM(RTRIM(v._Description))
    """)
    with engine_onec.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query)]
    return {value for row in rows if (value := _clean(row.get("subject_value")))}


def load_generated_subjects(session: Session, articles: Iterable[str]) -> dict[str, str]:
    article_values = sorted(
        {article for article in (_clean(value) for value in articles) if article}
    )
    if not article_values:
        return {}

    result: dict[str, str] = {}
    for chunk in _chunks(article_values, 1000):
        query = select(Product.article, Product.subject_generated).where(Product.article.in_(chunk))
        for article, subject_generated in session.execute(query):
            article_clean = _clean(article)
            subject_clean = _clean(subject_generated)
            if article_clean and subject_clean:
                result[article_clean] = subject_clean
    return result


def build_missing_subject_update_rows(
    candidates: Sequence[Mapping[str, Any]],
    generated_subjects: Mapping[str, str],
    subject_catalog_values: Iterable[str],
    *,
    classifier: SubjectClassifier | None,
    run_date: date | None = None,
    limit: int | None = None,
    skip_unknown: bool = True,
) -> SubjectUpdateBuildResult:
    catalog_by_normalized = {
        _normalize_subject(value): value
        for value in subject_catalog_values
        if _normalize_subject(value)
    }
    rows: list[NomenclaturePropertyUpdateRow] = []
    skipped: list[dict[str, str]] = []
    source_counts: Counter[str] = Counter()
    subject_counts: Counter[str] = Counter()
    effective_date = run_date or date.today()

    for candidate in candidates:
        if limit is not None and len(rows) >= limit:
            break

        article = _clean(candidate.get("article"))
        nomenclature_code = _clean(candidate.get("nomenclature_code") or candidate.get("code_1c"))
        name = _clean(candidate.get("name"))
        if not article or not nomenclature_code or not name:
            skipped.append(_skip(candidate, "missing_required_fields"))
            continue

        subject = _clean(generated_subjects.get(article))
        source = "subject_generated"
        if not subject or _is_unknown_subject(subject):
            source = "classifier"
            subject = _clean(classifier.classify(name) if classifier is not None else None)

        if not subject:
            skipped.append(_skip(candidate, "subject_not_classified"))
            continue
        if skip_unknown and _is_unknown_subject(subject):
            skipped.append(_skip(candidate, "subject_unknown", subject=subject))
            continue

        canonical_subject = catalog_by_normalized.get(_normalize_subject(subject))
        if not canonical_subject:
            skipped.append(_skip(candidate, "subject_catalog_value_missing", subject=subject))
            continue

        rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=(
                    f"nom-prop:{nomenclature_code}:{SUBJECT_PROPERTY_NAME}:"
                    f"{effective_date.isoformat()}:r1"
                ),
                nomenclature_code=nomenclature_code,
                property_name=SUBJECT_PROPERTY_NAME,
                value_type="property_value",
                new_value_name=canonical_subject,
                reason=f"Автоклассификация пустого свойства Предмет ({source})",
            )
        )
        source_counts[source] += 1
        subject_counts[canonical_subject] += 1

    return SubjectUpdateBuildResult(
        rows=tuple(rows),
        skipped=tuple(skipped),
        source_counts=dict(sorted(source_counts.items())),
        subject_counts=dict(sorted(subject_counts.items())),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("pytds").setLevel(logging.WARNING)
    load_ut103_env_file()
    args = _parse_args()

    settings = get_settings()
    if not settings.onec_database_url:
        raise SystemExit("ONEC_DATABASE_URL is required")

    engine_app = create_engine(settings.database_url)
    engine_onec = create_engine(settings.onec_database_url)

    candidates = load_missing_onec_subject_candidates(engine_onec)
    subject_catalog_values = load_onec_subject_catalog_values(engine_onec)
    with Session(engine_app) as session:
        generated_subjects = load_generated_subjects(
            session,
            (candidate["article"] for candidate in candidates),
        )

    classifier = CategoryClassifier.from_env(
        use_llm=args.llm,
        llm_limit=args.llm_limit,
        llm_only=args.llm_only,
        force_llm=args.force_llm,
        default_category=args.default_category,
    )
    try:
        result = build_missing_subject_update_rows(
            candidates,
            generated_subjects,
            subject_catalog_values,
            classifier=classifier,
            limit=args.limit,
            skip_unknown=not args.include_unknown,
        )
    finally:
        classifier.close()

    message_id = (
        args.message_id or f"missing-onec-subject-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    message = None
    if result.rows:
        message = NomenclaturePropertyUpdateMessage(
            message_id=message_id,
            rows=result.rows,
            mode=args.mode,
            approved_by=args.approved_by,
            source=args.source,
        )

    payload = _payload(
        result,
        candidates_count=len(candidates),
        generated_subjects_count=len(generated_subjects),
        subject_catalog_values_count=len(subject_catalog_values),
        message_id=message_id,
        llm_used=classifier.llm_calls,
        llm_failed=classifier.llm_failed,
    )

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.print_xml:
        if message is None:
            if args.allow_empty:
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                else:
                    print(_human_summary(payload))
                return 0
            raise SystemExit("No update rows to export")
        print(build_nomenclature_property_updates_xml(message).decode("windows-1251"))
        return 0

    if args.write_ready:
        if message is None:
            if args.allow_empty:
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                else:
                    print(_human_summary(payload))
                return 0
            raise SystemExit("No update rows to export")
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
        output_path = write_nomenclature_property_updates_message(
            exchange_root,
            message,
            overwrite=args.overwrite,
        )
        payload["path"] = str(output_path)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(_human_summary(payload))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Classify live 1C nomenclature rows with empty Предмет and build "
            "nomenclature_property_updates.v1 rows."
        )
    )
    parser.add_argument("--limit", type=int, help="Limit exported update rows")
    parser.add_argument("--llm", action="store_true", help="Use LLM fallback for unclear names")
    parser.add_argument("--llm-limit", type=int, default=0, help="Max LLM calls (0 = no limit)")
    parser.add_argument(
        "--llm-only",
        action="store_true",
        help="Skip rule-based classification and use only LLM/default fallback",
    )
    parser.add_argument(
        "--force-llm",
        action="store_true",
        help="Ask LLM even when rules matched, falling back to rules",
    )
    parser.add_argument(
        "--default-category",
        default=DEFAULT_FALLBACK_CATEGORY,
        help="Fallback subject for classifier misses",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="Allow fallback/unknown subject values if they exist in 1C catalog",
    )
    parser.add_argument("--message-id", help="Stable message id for the export package")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--approved-by", default="", help="Required for apply mode")
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Source value for XML header",
    )
    parser.add_argument("--output-json", type=Path, help="Write preview payload as JSON")
    parser.add_argument("--print-xml", action="store_true", help="Print XML without writing file")
    parser.add_argument(
        "--write-ready", action="store_true", help="Write ready XML to exchange root"
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit successfully without writing XML when no subject rows are ready for 1C.",
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing ready XML")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    return parser.parse_args()


def _payload(
    result: SubjectUpdateBuildResult,
    *,
    candidates_count: int,
    generated_subjects_count: int,
    subject_catalog_values_count: int,
    message_id: str,
    llm_used: int,
    llm_failed: int,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "candidates": candidates_count,
        "rows": len(result.rows),
        "skipped": len(result.skipped),
        "generated_subjects": generated_subjects_count,
        "subject_catalog_values": subject_catalog_values_count,
        "source_counts": result.source_counts,
        "subject_counts": result.subject_counts,
        "llm_used": llm_used,
        "llm_failed": llm_failed,
        "items": [asdict(row) for row in result.rows],
        "skipped_items": list(result.skipped),
    }


def _human_summary(payload: Mapping[str, Any]) -> str:
    lines = [
        f"message_id: {payload['message_id']}",
        f"candidates: {payload['candidates']}",
        f"rows: {payload['rows']}",
        f"skipped: {payload['skipped']}",
        f"generated_subjects: {payload['generated_subjects']}",
        f"subject_catalog_values: {payload['subject_catalog_values']}",
        f"source_counts: {payload['source_counts']}",
        f"subject_counts: {payload['subject_counts']}",
        f"llm_used: {payload['llm_used']}",
        f"llm_failed: {payload['llm_failed']}",
    ]
    if payload.get("path"):
        lines.append(f"path: {payload['path']}")
    return "\n".join(lines)


def _skip(candidate: Mapping[str, Any], reason: str, *, subject: str = "") -> dict[str, str]:
    return {
        "article": _clean(candidate.get("article")),
        "nomenclature_code": _clean(candidate.get("nomenclature_code") or candidate.get("code_1c")),
        "name": _clean(candidate.get("name")),
        "subject": subject,
        "reason": reason,
    }


def _is_unknown_subject(value: str | None) -> bool:
    normalized = _normalize_subject(value)
    return not normalized or normalized in UNKNOWN_SUBJECT_VALUES


def _normalize_subject(value: str | None) -> str:
    return " ".join(_clean(value).lower().split())


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


if __name__ == "__main__":
    raise SystemExit(main())
