from __future__ import annotations

import json
import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem
from app.models.competitor_item import CompetitorItemParseStatus

logger = logging.getLogger("tasks.cleanup_llm_blocked_wearables")


def run_cleanup(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(CompetitorItem).where(
            CompetitorItem.parse_version == "llm_parse_v2",
            CompetitorItem.parse_status == CompetitorItemParseStatus.CONFLICT,
            CompetitorItem.parse_error == "llm_blocked_wearable",
        )
    ).scalars()

    processed = 0
    updated = 0
    for item in rows:
        processed += 1
        changed = False
        if item.parsed_device_brand is not None:
            item.parsed_device_brand = None
            changed = True
        if item.parsed_device_model is not None:
            item.parsed_device_model = None
            changed = True
        if item.parsed_device_variant is not None:
            item.parsed_device_variant = None
            changed = True
        if item.parse_confidence is not None:
            item.parse_confidence = None
            changed = True
        if changed:
            session.add(item)
            updated += 1

    session.commit()
    result = {"processed": processed, "updated": updated}
    logger.info("cleanup llm blocked wearables completed", extra=result)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        result = run_cleanup(session)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
