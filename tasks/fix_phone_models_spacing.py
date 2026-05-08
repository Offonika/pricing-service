"""Нормализует пробелы между буквами и цифрами в phone_models."""

import json
import logging
import re

from sqlalchemy import and_, create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import PhoneModel

logger = logging.getLogger("app.fix_phone_models_spacing")


def _insert_spaces(value: str) -> str:
    """Вставляет пробелы между границами буква↔цифра и схлопывает повторные."""
    s = value.strip()
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)
    s = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def fix_spacing(session: Session, brand: str | None = None) -> dict:
    query = session.query(PhoneModel)
    if brand:
        query = query.filter(PhoneModel.brand.ilike(brand))
    models = query.all()
    updated = []
    skipped_same = 0
    skipped_conflict = 0
    for pm in models:
        new_name = _insert_spaces(pm.model_name or "")
        if not new_name or new_name == pm.model_name:
            skipped_same += 1
            continue
        conflict = (
            session.query(PhoneModel)
            .filter(
                and_(
                    PhoneModel.id != pm.id,
                    PhoneModel.brand == pm.brand,
                    PhoneModel.model_name == new_name,
                    PhoneModel.variant == pm.variant,
                )
            )
            .first()
        )
        if conflict:
            skipped_conflict += 1
            continue
        pm.model_name = new_name
        session.add(pm)
        updated.append({"id": pm.id, "brand": pm.brand, "old": pm.model_name, "new": new_name})
    if updated:
        session.commit()
    return {
        "updated": len(updated),
        "skipped_same": skipped_same,
        "skipped_conflict": skipped_conflict,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        result = fix_spacing(session, brand="apple")
    logger.info("spacing fix completed", extra=result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
