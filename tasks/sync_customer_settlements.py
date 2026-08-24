from __future__ import annotations

import json

from app.workers.customer_settlements import run_customer_settlement_financial_sync


def main() -> int:
    try:
        result = run_customer_settlement_financial_sync()
    except Exception:
        result = {"status": "error", "reason": "financial_sync_failed"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # Retry source/sync failures and a transient shared/exclusive context collision.
    # Configuration and rollout gates are operator actions and stay non-retryable.
    retryable = result.get("status") == "error" or (
        result.get("status") == "skipped_lock" and result.get("reason") == "context_lock"
    )
    return 1 if retryable else 0


if __name__ == "__main__":
    raise SystemExit(main())
