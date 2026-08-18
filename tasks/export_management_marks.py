"""Выгрузка управленческой метки ассортимента в УТ 10.3.

По умолчанию задача только показывает пакет (`dry_run`) и ничего не отправляет.
Запись в обменный каталог включается флагом `--write`, режим `apply` —
`--mode apply` вместе с `--approved-by`; и то и другое требует отдельного
разрешения пользователя, потому что это внешнее действие в 1С.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.exporters.ut103_exchange import (
    load_ut103_env_file,
    resolve_ut103_exchange_root,
)
from app.services.exporters.ut103_nomenclature_properties import (
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)
from app.services.procurement_management_marks_export import (
    build_management_marks_message,
    collect_management_marks,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export assortment management marks (Допродаём, Взамен ведём) to UT 10.3."
    )
    parser.add_argument("--exchange-root")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--approved-by", default="")
    parser.add_argument(
        "--limit",
        type=int,
        help="Take only the first N cards: используется для пилота на нескольких карточках.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the XML package into the exchange folder instead of printing it.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_ut103_env_file()
    args = parse_args()
    settings = get_settings()
    engine = build_engine(settings.database_url)
    with Session(engine) as db:
        marks = collect_management_marks(db)
    if args.limit is not None:
        marks = marks[: max(args.limit, 0)]
    if not marks:
        print("management marks: нет согласованных решений для выгрузки")
        return 0

    message = build_management_marks_message(
        marks,
        mode=args.mode,
        approved_by=args.approved_by,
    )
    written_path = None
    if args.write:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
        written_path = str(write_nomenclature_property_updates_message(exchange_root, message))

    payload = {
        "message_id": message.message_id,
        "mode": message.mode,
        "cards": len(marks),
        "rows": len(message.rows),
        "written_path": written_path,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif written_path:
        print(
            f"management marks: {payload['cards']} карточек, {payload['rows']} строк, "
            f"пакет {payload['message_id']} -> {written_path}"
        )
    else:
        print(build_nomenclature_property_updates_xml(message).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
