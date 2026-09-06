"""Conservative recovery of line decisions from explicit, addressable journal events."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.services.procurement_supply_scenarios import facts_hash


def plan_manual_removal_recovery(
    lines: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_line = defaultdict(list)
    for event in events:
        if event.get("entity_type") == "order_line":
            by_line[str(event.get("entity_id"))].append(event)
    proposals = []
    for line in lines:
        history = sorted(
            by_line.get(str(line["id"]), []),
            key=lambda event: (str(event.get("created_at") or ""), event["id"]),
        )
        relevant = []
        for event in history:
            after = [
                item
                for item in (event.get("after") or {}).get("lines", [])
                if item.get("id") == line["id"]
            ]
            before = [
                item
                for item in (event.get("before") or {}).get("lines", [])
                if item.get("id") == line["id"]
            ]
            if event.get("event_type") in {"order_line_removed", "order_line_restored"} or (
                len(after) == len(before) == 1
                and after[0].get("removed") != before[0].get("removed")
            ):
                relevant.append((event, before, after))
        if not relevant:
            continue
        event, before, after = relevant[-1]
        # An explicit later restore wins; an ordinary recalculation is not a user event.
        if len(after) == 1 and after[0].get("removed") is False:
            continue
        existing = (line.get("payload") or {}).get("manual_removal") or {}
        if existing.get("removed_at") or existing.get("restored_at"):
            continue
        reason = str((event.get("payload") or {}).get("removal_reason") or "").strip()
        exact = (
            event.get("event_type") == "order_line_removed"
            and len(after) == 1
            and after[0].get("removed") is True
            and bool(reason)
            and bool(event.get("actor"))
            and bool(event.get("created_at"))
            and event.get("order_id") == line.get("order_id")
        )
        proposal = {
            "line_id": line["id"],
            "order_id": line["order_id"],
            "expected_line_version": line["version"],
            "event_id": event["id"],
            "status": "recoverable" if exact else "requires_reconciliation",
            "evidence_hash": facts_hash({"line": line, "events": history}),
        }
        if exact:
            proposal["manual_removal"] = {
                "reason": reason,
                "actor": event["actor"],
                "removed_at": str(event["created_at"]),
                "recovered_from_event_id": event["id"],
            }
        proposals.append(proposal)
    return proposals
