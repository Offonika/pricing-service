"""Detect whether new cargo dropoff dates should refresh supplier lead time."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "procurement_cargo_dropoff_lead_time_refresh_detection.v1"
STATE_SCHEMA = "procurement_cargo_dropoff_lead_time_refresh_state.v1"
EMPTY_DATE_PREFIXES = ("0001-01-01", "1753-01-01")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _orders(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw_orders = payload.get("orders")
    if not isinstance(raw_orders, list):
        return []
    return [row for row in raw_orders if isinstance(row, dict)]


def _result_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        return []
    return [row for row in raw_rows if isinstance(row, dict)]


def _source_number(row: Mapping[str, Any]) -> str:
    return _clean(row.get("source_number") or row.get("onec_source_number") or row.get("number"))


def _successful_source_numbers(result_payload: Any) -> set[str]:
    numbers: set[str] = set()
    for row in _result_rows(result_payload):
        if _clean(row.get("action")) == "blocked":
            continue
        number = _source_number(row)
        if number:
            numbers.add(number)
    return numbers


def _normalize_cargo_date(value: Any) -> str:
    raw = _clean(value)
    if not raw or raw.startswith(EMPTY_DATE_PREFIXES):
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10] if len(raw) >= 10 else raw
    return parsed.date().isoformat()


def _supplier_value(order: Mapping[str, Any], key: str) -> str:
    supplier = order.get("supplier")
    if not isinstance(supplier, Mapping):
        return ""
    return _clean(supplier.get(key))


def _event_hash(event: Mapping[str, Any]) -> str:
    identity = {
        "cargo_dropoff_date": event.get("cargo_dropoff_date"),
        "onec_ref": event.get("onec_ref"),
        "source_number": event.get("source_number"),
        "supplier_onec_ref": event.get("supplier_onec_ref"),
        "supplier_title": event.get("supplier_title"),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_from_order(order: Mapping[str, Any], *, source_path: Path) -> dict[str, Any] | None:
    cargo_dropoff_date = _normalize_cargo_date(order.get("cargo_dropoff_date"))
    source_number = _source_number(order)
    if not cargo_dropoff_date or not source_number:
        return None
    event: dict[str, Any] = {
        "source_number": source_number,
        "onec_ref": _clean(order.get("onec_ref")),
        "supplier_onec_ref": _supplier_value(order, "onec_ref"),
        "supplier_title": _supplier_value(order, "title"),
        "cargo_dropoff_date": cargo_dropoff_date,
        "source_path": str(source_path),
    }
    event["event_hash"] = _event_hash(event)
    return event


def _input_paths_from_result(result_payload: Any, *, result_path: Path) -> list[Path]:
    if not isinstance(result_payload, Mapping):
        return []
    raw_path = _clean(result_payload.get("input_json"))
    if not raw_path:
        return []
    input_path = Path(raw_path)
    if not input_path.is_absolute():
        input_path = result_path.parent / input_path
    return [input_path]


def collect_cargo_dropoff_events(
    *,
    result_paths: Sequence[Path],
    input_paths: Sequence[Path] = (),
) -> list[dict[str, Any]]:
    events_by_hash: dict[str, dict[str, Any]] = {}
    queued_inputs: list[tuple[Path, set[str]]] = [(path, set()) for path in input_paths]

    for result_path in result_paths:
        if not result_path.exists():
            continue
        result_payload = _load_json(result_path)
        synced_numbers = _successful_source_numbers(result_payload)
        for input_path in _input_paths_from_result(result_payload, result_path=result_path):
            queued_inputs.append((input_path, synced_numbers))

    for input_path, allowed_numbers in queued_inputs:
        if not input_path.exists():
            continue
        input_payload = _load_json(input_path)
        for order in _orders(input_payload):
            source_number = _source_number(order)
            if allowed_numbers and source_number not in allowed_numbers:
                continue
            event = _event_from_order(order, source_path=input_path)
            if event:
                events_by_hash[event["event_hash"]] = event

    return sorted(
        events_by_hash.values(),
        key=lambda item: (
            _clean(item.get("cargo_dropoff_date")),
            _clean(item.get("source_number")),
            _clean(item.get("supplier_title")),
        ),
    )


def load_known_event_hashes(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    payload = _load_json(state_path)
    if not isinstance(payload, Mapping):
        return set()
    raw_hashes = payload.get("event_hashes")
    if not isinstance(raw_hashes, list):
        return set()
    return {_clean(value) for value in raw_hashes if _clean(value)}


def build_detection_payload(
    *,
    state_path: Path,
    result_paths: Sequence[Path],
    input_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    events = collect_cargo_dropoff_events(result_paths=result_paths, input_paths=input_paths)
    known_hashes = load_known_event_hashes(state_path)
    new_events = [event for event in events if event["event_hash"] not in known_hashes]
    return {
        "schema": SCHEMA,
        "refresh_needed": bool(new_events),
        "cargo_event_count": len(events),
        "known_event_count": len(known_hashes),
        "new_event_count": len(new_events),
        "state_path": str(state_path),
        "result_paths": [str(path) for path in result_paths],
        "input_paths": [str(path) for path in input_paths],
        "new_events": [
            {
                "source_number": event["source_number"],
                "supplier_title": event["supplier_title"],
                "cargo_dropoff_date": event["cargo_dropoff_date"],
            }
            for event in new_events[:20]
        ],
    }


def apply_state(
    *,
    state_path: Path,
    result_paths: Sequence[Path],
    input_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    events = collect_cargo_dropoff_events(result_paths=result_paths, input_paths=input_paths)
    known_hashes = load_known_event_hashes(state_path)
    current_hashes = {event["event_hash"] for event in events}
    next_hashes = sorted(known_hashes | current_hashes)
    payload = {
        "schema": STATE_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "event_hashes": next_hashes,
        "last_event_count": len(events),
    }
    _write_json(state_path, payload)
    return {
        "state_updated": True,
        "state_path": str(state_path),
        "saved_event_count": len(next_hashes),
        "current_event_count": len(events),
    }


def _format_env(payload: Mapping[str, Any]) -> str:
    values = {
        "refresh_needed": "true" if payload.get("refresh_needed") else "false",
        "cargo_event_count": str(payload.get("cargo_event_count", 0)),
        "known_event_count": str(payload.get("known_event_count", 0)),
        "new_event_count": str(payload.get("new_event_count", 0)),
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, action="append", default=[])
    parser.add_argument("--input-json", type=Path, action="append", default=[])
    parser.add_argument("--apply-state", action="store_true")
    parser.add_argument("--format", choices=("json", "env"), default="json")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_detection_payload(
        state_path=args.state_path,
        result_paths=args.result_json,
        input_paths=args.input_json,
    )
    if args.apply_state:
        payload.update(
            apply_state(
                state_path=args.state_path,
                result_paths=args.result_json,
                input_paths=args.input_json,
            )
        )
    if args.format == "env":
        print(_format_env(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
