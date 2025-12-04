"""CLI для матчинга FTP-цен конкурентов с товарами TopControl."""

import json
import logging
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.competitor_matching import match_competitor_ftp_records


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    engine = create_engine(settings.database_url)
    subject_whitelist = None
    if settings.match_subject_whitelist:
        subject_whitelist = [s.strip() for s in settings.match_subject_whitelist.split(",") if s.strip()]
    with Session(engine) as session:
        result = match_competitor_ftp_records(session, subject_whitelist=subject_whitelist)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    exit_code = 0 if not result.get("errors") else 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
