from __future__ import annotations

import json

from app.workers.customer_settlements import run_customer_settlement_cleanup


def main() -> int:
    try:
        result = run_customer_settlement_cleanup()
    except Exception:
        result = {"status": "error", "reason": "cleanup_failed"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
