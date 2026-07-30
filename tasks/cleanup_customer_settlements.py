from __future__ import annotations

import json

from app.workers.customer_settlements import run_customer_settlement_cleanup


def main() -> int:
    result = run_customer_settlement_cleanup()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
