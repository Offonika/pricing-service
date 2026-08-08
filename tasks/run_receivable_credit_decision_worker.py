from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import get_application_engine
from app.services.exporters.ut103_exchange import (
    load_ut103_env_file,
    resolve_ut103_exchange_root,
)
from app.services.receivable_credit_decisions import (
    run_credit_decision_worker_once,
)


def main() -> int:
    load_ut103_env_file()
    parser = argparse.ArgumentParser(
        description="Run one safe Bitrix -> UT 10.3 credit-decision worker cycle."
    )
    parser.add_argument("--exchange-root")
    args = parser.parse_args()
    settings = get_settings()
    exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    with Session(get_application_engine()) as db:
        result = run_credit_decision_worker_once(
            db,
            exchange_root=exchange_root,
            settings=settings,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
