"""Scheduled adapter for the site-order fulfillment synchronization contour."""

from __future__ import annotations

from infra.cron.order_fulfillment_sync import main

if __name__ == "__main__":
    raise SystemExit(main())
