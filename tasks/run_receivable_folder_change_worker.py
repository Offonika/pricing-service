from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infrastructure.db.engines import get_application_engine
from app.models import ReceivableFolderChangeOperation
from app.services.exporters.ut103_exchange import resolve_ut103_exchange_root
from app.services.receivable_folder_changes import (
    publish_folder_change_dry_run,
    sync_folder_change_results,
)


def run(*, exchange_root: str | Path | None = None) -> dict[str, int]:
    engine = get_application_engine()
    root = resolve_ut103_exchange_root(exchange_root)
    published = 0
    try:
        with Session(engine) as db:
            drafts = (
                db.execute(
                    select(ReceivableFolderChangeOperation).where(
                        ReceivableFolderChangeOperation.state == "draft"
                    )
                )
                .scalars()
                .all()
            )
            for operation in drafts:
                publish_folder_change_dry_run(db, operation, exchange_root=root)
                published += 1
            result = sync_folder_change_results(db, exchange_root=root)
            return {"published_dry_runs": published, **result}
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange-root", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(exchange_root=args.exchange_root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
