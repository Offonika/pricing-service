from __future__ import annotations

import csv
import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import CompetitorItem, Product, ProductCompetitorItemDecision
from app.services.manual_matching_control import MOSCOW_TZ
from app.services.manual_matching_feedback import build_manual_matching_feedback_report
from tasks.analyze_manual_matching_feedback import main as feedback_main

REPORT_DATE = date(2026, 5, 26)


def _dt(hour: int) -> datetime:
    return datetime(2026, 5, 26, hour, tzinfo=MOSCOW_TZ)


def _product(*, article: str, model: str = "iPhone 11") -> Product:
    return Product(
        article=article,
        name=f"Дисплей для Apple {model} + тачскрин (черный) (OLED)",
        subject="дисплей",
        subject_1c="дисплей",
        color="черный",
        display_quality="OLED",
        display_type="OLED",
        display_has_frame=True,
        display_refresh_rate_hz=60,
    )


def _item(*, external_id: str, model: str = "iPhone 11", color: str = "черный") -> CompetitorItem:
    quality = "OLED" if color == "черный" else "TFT"
    return CompetitorItem(
        competitor="moba",
        external_id=external_id,
        name=f"Дисплей для {model} в сборе {color} {quality} 60Hz",
        item_type="display",
        attrs_model=model,
        attrs_color=color,
        attrs_quality=quality,
        screen_matrix_type=quality,
        has_frame=True,
        refresh_rate_hz=60,
    )


def _decision(
    product: Product,
    item: CompetitorItem,
    *,
    action: str,
    hour: int,
    reason: str | None = None,
) -> ProductCompetitorItemDecision:
    return ProductCompetitorItemDecision(
        product_id=product.id,
        competitor_item_id=item.id,
        action=action,
        reason=reason,
        created_by="bitrix:member:130756",
        created_at=_dt(hour),
    )


def test_feedback_report_keeps_latest_pair_decision_and_excludes_unsafe_labels(
    db_session: Session,
) -> None:
    product = _product(article="P-1")
    rejected_item = _item(external_id="LCD-REJECT")
    suspicious_item = _item(external_id="LCD-SUSPICIOUS", model="iPhone 12", color="белый")
    revoked_item = _item(external_id="LCD-REVOKED")
    accepted_item = _item(external_id="LCD-ACCEPTED")
    db_session.add_all([product, rejected_item, suspicious_item, revoked_item, accepted_item])
    db_session.flush()
    db_session.add_all(
        [
            _decision(product, rejected_item, action="accept", hour=8),
            _decision(product, rejected_item, action="reject", hour=9),
            _decision(product, suspicious_item, action="accept", hour=10),
            _decision(product, revoked_item, action="accept", hour=11),
            _decision(product, revoked_item, action="revoke", hour=12),
            _decision(product, accepted_item, action="accept", hour=13),
        ]
    )
    db_session.commit()

    report, rows = build_manual_matching_feedback_report(
        db_session,
        as_of=REPORT_DATE,
        sample_limit=5,
    )

    assert report["summary"] == {
        "raw_decisions": 6,
        "raw_actions": {"accept": 4, "reject": 1, "revoke": 1},
        "unique_pairs": 4,
        "duplicate_decisions_collapsed": 2,
        "clean_examples": 2,
        "clean_positive": 1,
        "clean_negative": 1,
        "positive_rate": 0.5,
        "excluded_revoked": 1,
        "excluded_suspicious_accepts": 1,
        "skipped_missing_entities": 0,
    }
    assert {(row["competitor_external_id"], row["label"]) for row in rows} == {
        ("LCD-REJECT", 0),
        ("LCD-ACCEPTED", 1),
    }
    suspicious = report["samples"]["suspicious_accepts"]
    assert suspicious[0]["competitor_external_id"] == "LCD-SUSPICIOUS"
    assert "display_model_conflict" in suspicious[0]["diagnostic_reasons"]
    assert report["guardrail_replay"]["negative_without_rule_conflict"] == 1


def test_feedback_cli_writes_markdown_json_and_clean_csv(
    db_session: Session,
    sqlite_engine,
    tmp_path,
    capsys,
) -> None:
    product = _product(article="P-2")
    accepted_item = _item(external_id="LCD-A")
    rejected_item = _item(external_id="LCD-R")
    db_session.add_all([product, accepted_item, rejected_item])
    db_session.flush()
    db_session.add_all(
        [
            _decision(product, accepted_item, action="accept", hour=9),
            _decision(product, rejected_item, action="reject", hour=10, reason="wrong_quality"),
        ]
    )
    db_session.commit()

    report = feedback_main(
        [
            "--as-of",
            REPORT_DATE.isoformat(),
            "--database-url",
            str(sqlite_engine.url),
            "--output-dir",
            str(tmp_path),
            "--json",
        ]
    )

    stdout = json.loads(capsys.readouterr().out)
    assert stdout["summary"]["clean_examples"] == 2
    assert report["artifacts"]["markdown"].endswith(".md")
    markdown = tmp_path / "manual_matching_feedback_2026-05-26.md"
    payload = tmp_path / "manual_matching_feedback_2026-05-26.json"
    dataset = tmp_path / "manual_matching_feedback_2026-05-26.csv"
    assert markdown.exists()
    assert payload.exists()
    assert dataset.exists()
    assert "Анализ ручной разметки" in markdown.read_text(encoding="utf-8")
    assert json.loads(payload.read_text(encoding="utf-8"))["summary"]["clean_positive"] == 1
    with dataset.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["label"] for row in rows} == {"0", "1"}


def test_feedback_cli_no_files_does_not_create_output_directory(
    db_session: Session,
    sqlite_engine,
    tmp_path,
) -> None:
    output_dir = tmp_path / "not-created"

    report = feedback_main(
        [
            "--as-of",
            REPORT_DATE.isoformat(),
            "--database-url",
            str(sqlite_engine.url),
            "--output-dir",
            str(output_dir),
            "--no-files",
        ]
    )

    assert report["artifacts"] == {}
    assert not output_dir.exists()


def test_feedback_report_explains_battery_premium_tier_reject(
    db_session: Session,
) -> None:
    product = Product(
        article="BTT-PREMIUM",
        name="Аккумулятор для Xiaomi 17 Pro (BM6H) (Premium)",
        subject="аккумулятор",
    )
    item = CompetitorItem(
        competitor="moba",
        external_id="BTT-BM6H",
        name="Аккумулятор для Xiaomi 17 Pro (BM6H)",
        item_type="battery",
    )
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(_decision(product, item, action="reject", hour=14))
    db_session.commit()

    report, rows = build_manual_matching_feedback_report(db_session, as_of=REPORT_DATE)

    assert rows[0]["diagnostic_reasons"] == "battery_premium_tier_conflict"
    assert report["guardrail_replay"]["negative_with_rule_conflict"] == 1
    assert report["guardrail_replay"]["negative_without_rule_conflict"] == 0
