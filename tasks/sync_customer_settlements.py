from __future__ import annotations

import json

from app.workers.customer_settlements import run_customer_settlement_financial_sync


def main() -> int:
    result = run_customer_settlement_financial_sync()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # The cron wrapper retries only a real source/sync error. Configuration and
    # rollout gates are operator actions and must not create a second noisy run.
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
