from __future__ import annotations

from app.models import CompetitorItem
from app.models.competitor_item import CompetitorItemParseStatus
from tasks.extract_competitor_attrs import extract_attrs


def test_extract_attrs_parser_only_fills_display_attrs(db_session):
    item = CompetitorItem(
        competitor="moba",
        external_id="LCD-IP17",
        name="Дисплей для iPhone 17 Pro OLED в рамке черный GX ORIG 120Hz",
        category="Дисплеи",
    )
    db_session.add(item)
    db_session.commit()

    stats = extract_attrs(
        db_session,
        source=None,
        category=None,
        name_contains=None,
        first_seen_date=None,
        first_seen_after=None,
        only_null=True,
        only_bad=False,
        only_parse_version_missing=False,
        overwrite=False,
        rerun_errors=False,
        limit=None,
        offset=None,
        min_llm_confidence=0.6,
        min_confidence_bump=0.1,
        repair_attempts=0,
        llm_timeout=1.0,
        dry_run=False,
        parse_version="parser_v1",
        sample_limit=0,
        samples_file=None,
        parser_only=True,
    )

    db_session.refresh(item)
    assert stats["updated"] == 1
    assert item.item_type == "display"
    assert item.attrs_json
    assert item.attrs_json["refresh_rate_hz"] == 120
    assert item.has_frame is True
    assert item.screen_matrix_type != "UNKNOWN"
    assert item.parse_status == CompetitorItemParseStatus.OK
    assert item.parse_version == "parser_v1"
