from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.services.customer_price_type_external_actions import (
    run_customer_price_type_external_actions_once,
    sync_customer_price_type_bitrix_completions_once,
)
from app.services.exporters.ut103_exchange import (
    load_ut103_env_file,
    resolve_ut103_exchange_root,
)


def main() -> int:
    load_ut103_env_file()
    parser = argparse.ArgumentParser(
        description="Обработать одну порцию согласованных действий по типам цен."
    )
    parser.add_argument("--exchange-root")
    args = parser.parse_args()
    settings = get_settings()
    exchange_root = None
    if settings.customer_price_type_onec_actions_enabled:
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    with Session(get_application_engine()) as db:
        result = run_customer_price_type_external_actions_once(
            db,
            exchange_root=exchange_root,
            settings=settings,
        )
        result["bitrix_completion_sync"] = sync_customer_price_type_bitrix_completions_once(
            db,
            settings=settings,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return (
        1
        if (
            result["errors"]
            or result["technical_review"]
            or result["bitrix_completion_sync"]["errors"]
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
