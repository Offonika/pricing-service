from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import CompetitorItem, Product, ProductCompetitorItemDecision
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchStatus,
)
from app.services.manual_matching_control import (
    MOSCOW_TZ,
    build_manual_matching_control_report,
    render_manual_matching_markdown,
    suspicious_accept_reasons,
)
from tasks.manual_matching_control import main as manual_matching_control_main

REPORT_DATE = date(2026, 5, 26)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 26, hour, minute, tzinfo=MOSCOW_TZ)


def _actor(user_id: str) -> str:
    return f"bitrix:member:{user_id}"


def _product(**kwargs) -> Product:
    defaults = {
        "article": f"P-{kwargs.get('article_suffix', '1')}",
        "name": "Дисплей для Apple iPhone 11 + тачскрин (черный) (OLED)",
        "subject": "дисплей",
        "subject_1c": "дисплей",
        "color": "черный",
        "display_quality": "OLED",
        "display_type": "OLED",
        "display_has_frame": True,
        "display_refresh_rate_hz": 60,
    }
    defaults.update(kwargs)
    defaults.pop("article_suffix", None)
    return Product(**defaults)


def _display_item(**kwargs) -> CompetitorItem:
    defaults = {
        "competitor": "moba",
        "external_id": f"LCD-{kwargs.get('external_suffix', '1')}",
        "name": "Дисплей для iPhone 11 в сборе Черный OLED 60Hz",
        "item_type": "display",
        "attrs_model": "iPhone 11",
        "attrs_color": "черный",
        "attrs_quality": "OLED",
        "screen_matrix_type": "OLED",
        "has_frame": True,
        "refresh_rate_hz": 60,
    }
    defaults.update(kwargs)
    defaults.pop("external_suffix", None)
    return CompetitorItem(**defaults)


def _decision(
    product: Product,
    item: CompetitorItem,
    *,
    action: str,
    user_id: str,
    created_at: datetime | None = None,
) -> ProductCompetitorItemDecision:
    return ProductCompetitorItemDecision(
        product_id=product.id,
        competitor_item_id=item.id,
        action=action,
        created_by=_actor(user_id),
        created_at=created_at or _dt(10),
    )


def test_manual_matching_report_counts_plan_fact_for_four_managers(db_session: Session) -> None:
    product = _product()
    item = _display_item()
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add_all(
        [
            _decision(product, item, action="accept", user_id="130757", created_at=_dt(8)),
            _decision(product, item, action="accept", user_id="130756", created_at=_dt(9)),
            _decision(product, item, action="reject", user_id="130756", created_at=_dt(10)),
            _decision(product, item, action="revoke", user_id="130917", created_at=_dt(11)),
            _decision(product, item, action="accept", user_id="130747", created_at=_dt(12)),
            _decision(product, item, action="accept", user_id="999999", created_at=_dt(13)),
        ]
    )
    db_session.commit()

    report = build_manual_matching_control_report(db_session, report_date=REPORT_DATE)

    by_id = {row["user_id"]: row for row in report["managers"]}
    assert by_id["130757"]["total"] == 1
    assert by_id["130757"]["accept"] == 1
    assert by_id["130756"]["total"] == 2
    assert by_id["130756"]["accept"] == 1
    assert by_id["130756"]["reject"] == 1
    assert by_id["130917"]["total"] == 1
    assert by_id["130917"]["revoke"] == 1
    assert by_id["130747"]["total"] == 1
    assert report["summary"]["total_done"] == 5
    assert report["summary"]["total_plan"] == 40
    assert report["summary"]["unmatched_decisions"] == 1


def test_manual_matching_report_counts_review_queue_by_status_and_display(
    db_session: Session,
) -> None:
    product = _product()
    display = _display_item()
    battery = CompetitorItem(
        competitor="moba",
        external_id="BTT-1",
        name="Аккумулятор для iPhone 11",
        item_type="battery",
    )
    accepted = _display_item(external_suffix="accepted")
    db_session.add_all([product, display, battery, accepted])
    db_session.flush()
    db_session.add_all(
        [
            CompetitorItemMatch(
                competitor_item_id=display.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.SUGGESTED,
            ),
            CompetitorItemMatch(
                competitor_item_id=battery.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.NEEDS_REVIEW,
            ),
            CompetitorItemMatch(
                competitor_item_id=accepted.id,
                product_id=product.id,
                status=CompetitorItemMatchStatus.ACCEPTED,
            ),
        ]
    )
    db_session.commit()

    report = build_manual_matching_control_report(db_session, report_date=REPORT_DATE)

    assert report["queue"]["total"] == 2
    assert report["queue"]["display"] == 1
    assert report["queue"]["by_status"] == {"needs_review": 1, "suggested": 1}
    assert report["queue"]["by_item_type"] == {"battery": 1, "display": 1}


def test_suspicious_accept_reasons_detect_display_conflicts(db_session: Session) -> None:
    product = _product(
        article="P-CONFLICT",
        name="Дисплей для Samsung A320 Galaxy A3 (2017) + тачскрин (черный) (OLED)",
        color="черный",
        display_quality="OLED",
        display_type="OLED",
        display_has_frame=True,
        display_refresh_rate_hz=60,
    )
    item = _display_item(
        external_id="LCD-CONFLICT",
        name="LCD дисплей для Samsung A520 Galaxy A5 белый TFT 90Hz",
        attrs_model="Samsung A520",
        attrs_color="белый",
        attrs_quality="TFT",
        screen_matrix_type="TFT",
        has_frame=False,
        refresh_rate_hz=90,
    )
    db_session.add_all([product, item])
    db_session.flush()
    decision = _decision(product, item, action="accept", user_id="130747")
    db_session.add(decision)
    db_session.commit()

    reasons = suspicious_accept_reasons(db_session, decision=decision, product=product, item=item)

    assert "display_model_conflict" in reasons
    assert "display_color_conflict" in reasons
    assert "display_frame_conflict" in reasons
    assert "display_quality_conflict" in reasons
    assert "display_matrix_conflict" in reasons
    assert "display_refresh_rate_conflict" in reasons


def test_suspicious_accept_reasons_detect_later_reject(db_session: Session) -> None:
    product = _product()
    item = _display_item()
    db_session.add_all([product, item])
    db_session.flush()
    accept = _decision(product, item, action="accept", user_id="130747", created_at=_dt(9))
    reject = _decision(product, item, action="reject", user_id="130747", created_at=_dt(10))
    db_session.add_all([accept, reject])
    db_session.commit()

    reasons = suspicious_accept_reasons(db_session, decision=accept, product=product, item=item)

    assert "later_rejected_or_revoked" in reasons


def test_suspicious_accept_reasons_detect_guardrail_catalog_conflict(
    db_session: Session,
) -> None:
    product = Product(
        article="056286",
        name="Держатель сим-карты для Huawei Honor X6 (VNE-LX1) (синий)",
        category="Держатели SIM-карт",
        subject="держатель сим-карты",
    )
    item = CompetitorItem(
        competitor="liberti",
        external_id="451224",
        name="Задняя крышка для Huawei Honor X6 (VNE-LX1) (синий)",
        item_type="housing",
    )
    db_session.add_all([product, item])
    db_session.flush()
    decision = _decision(product, item, action="accept", user_id="130747")
    db_session.add(decision)
    db_session.commit()

    reasons = suspicious_accept_reasons(db_session, decision=decision, product=product, item=item)

    assert "guardrail_catalog_family_conflict" in reasons


def test_manual_matching_cli_writes_markdown_and_json(
    db_session: Session, sqlite_engine, tmp_path, capsys
) -> None:
    product = _product()
    item = _display_item()
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(_decision(product, item, action="reject", user_id="130756", created_at=_dt(15)))
    db_session.commit()

    report = manual_matching_control_main(
        [
            "--date",
            REPORT_DATE.isoformat(),
            "--database-url",
            str(sqlite_engine.url),
            "--output-dir",
            str(tmp_path),
            "--json",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    report_path = tmp_path / f"{REPORT_DATE.isoformat()}.md"
    assert payload["date"] == REPORT_DATE.isoformat()
    assert payload["summary"]["total_done"] == 1
    assert report["report_path"] == str(report_path)
    assert report_path.exists()
    markdown = report_path.read_text(encoding="utf-8")
    assert "Ручное сопоставление за 2026-05-26" in markdown
    assert "postgresql://" not in output
    assert "secret" not in output.lower()


def test_manual_matching_markdown_contains_suspicious_details(db_session: Session) -> None:
    product = _product(article="P-DETAIL")
    item = _display_item(external_id="LCD-DETAIL", attrs_color="белый")
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(_decision(product, item, action="accept", user_id="130756", created_at=_dt(16)))
    db_session.commit()

    report = build_manual_matching_control_report(db_session, report_date=REPORT_DATE)
    markdown = render_manual_matching_markdown(report)

    assert "Подозрительные принятия:" in markdown
    assert "P-DETAIL" in markdown
    assert "display_color_conflict" in markdown
