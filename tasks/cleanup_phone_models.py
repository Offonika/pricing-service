"""CLI для очистки мусорных phone_models и опционального повторного матчинга."""

import argparse
import json
import logging
import re

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import PhoneModel, ProductMatch, ProductMatchOverride
from app.services.competitor_matching import match_competitor_ftp_records

logger = logging.getLogger("app.cleanup.phone_models")


def _junk_reason(model_name: str | None, brand: str | None = None) -> tuple[bool, str | None]:
    if not model_name:
        return False, None
    name = model_name.strip()
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    special_junk = {
        "5sse",
        "mini1",
        "touch1",
        "ipodtouch2ndgeneration",
        "iphoneair",
        "air2",
        "xrxsxsmax",
    }
    if key in special_junk:
        return True, "special_junk"
    # Очень длинная слепленная строка без пробелов
    if len(key) > 25 and " " not in name:
        return True, "long_compact"
    # Цепочки A-кодов (Axxxx) повторяющиеся
    if re.fullmatch(r"(a\d{4,5}){2,}", key):
        return True, "a_code_chain"
    # Мусорные агрегаты из цифр/букв без разделителей: несколько числовых групп слеплены вместе
    digit_groups = re.findall(r"\d+", key)
    if " " not in name and len(key) > 18 and len(digit_groups) > 1:
        return True, "multi_compact"
    # Повтор варианта pro/plus/mini/max без разделителей
    if re.search(r"(pro|promax|plus|mini|max){2,}", key):
        return True, "variant_chain"
    # Повторяющаяся числовая группа (1414plus, 1515plus)
    if re.fullmatch(r"(\d{1,2})\1(plus|pro|promax)?", key):
        return True, "repeat_number_chain"
    # Специфика Apple: слепленные несколько поколений (например, 12pro12promax, 1313mini13pro13promax)
    if brand and brand.lower() == "apple":
        if " " not in name and len(digit_groups) > 1:
            return True, "apple_multi_compact"
        # склейки apple токенов xr/xs/x/max без разделителей
        apple_tokens = ["xr", "xs", "x", "max"]
        hits = sum(1 for t in apple_tokens if t in key)
        if hits >= 2 and " " not in name:
            return True, "apple_token_chain"
    # Мусорные агрегаты из цифр/букв без разделителей (очень длинные ключи)
    if len(key) > 30:
        return True, "too_long"
    return False, None


def cleanup_phone_models(
    session: Session, brand: str | None, dry_run: bool = True, aggressive: bool = False
) -> dict:
    query = session.query(PhoneModel)
    if brand:
        query = query.filter(PhoneModel.brand.ilike(brand))
    candidates = query.all()
    removed = []
    skipped = 0
    for pm in candidates:
        is_junk, reason = _junk_reason(pm.model_name, pm.brand)
        if not is_junk:
            skipped += 1
            continue
        entry = {"id": pm.id, "brand": pm.brand, "model": pm.model_name, "reason": reason}
        if aggressive:
            # удаляем зависимости вручную, чтобы не ломать FK
            pm_matches = session.query(ProductMatch).filter_by(phone_model_id=pm.id).all()
            for m in pm_matches:
                session.delete(m)
            pm_overrides = session.query(ProductMatchOverride).filter_by(phone_model_id=pm.id).all()
            for ov in pm_overrides:
                session.delete(ov)
            for kw in pm.keywords:
                session.delete(kw)
        removed.append(entry)
        if not dry_run:
            session.delete(pm)
    if not dry_run and removed:
        session.commit()
    return {"removed": removed, "skipped": skipped, "dry_run": dry_run, "aggressive": aggressive}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Cleanup junk phone_models and optionally rerun matching."
    )
    parser.add_argument("--brand", help="brand filter (e.g. apple)", default=None)
    parser.add_argument(
        "--no-dry-run", action="store_true", help="apply deletions (default: dry-run)"
    )
    parser.add_argument(
        "--rerun-matching",
        action="store_true",
        help="run match_competitor_ftp_records after cleanup",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="delete related ProductMatch/Overrides/Keywords before model",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        result = cleanup_phone_models(
            session, brand=args.brand, dry_run=not args.no_dry_run, aggressive=args.aggressive
        )
        logger.info(
            "cleanup finished",
            extra={
                "removed": len(result["removed"]),
                "skipped": result["skipped"],
                "dry_run": result["dry_run"],
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if args.rerun_matching:
            logger.info("starting competitor matching rerun after cleanup")
            match_result = match_competitor_ftp_records(session)
            print(json.dumps({"matching": match_result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
