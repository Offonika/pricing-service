from __future__ import annotations

import json

from app.workers.customer_settlements import run_customer_settlement_mapping_sync


def main() -> int:
    result = run_customer_settlement_mapping_sync()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    status = result.get("status")
    if status in {"activated", "unchanged", "skipped_lock", "disabled"}:
        return 0
    if status == "error":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
