"""Inspect and exchange immutable assortment lifecycle replay artifacts.

The store is local to the backtest contour and never writes application data,
1C lifecycle properties or supplier orders.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Mapping

from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
)


def _cmd_init(args: argparse.Namespace) -> int:
    store = AssortmentLifecycleReplayStore(args.store_path)
    store.initialize()
    print(json.dumps(store.manifest(), ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    payload = AssortmentLifecycleReplayStore(args.store_path).manifest()
    if args.output_json:
        _write_json(args.output_json, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_put_trajectory(args: argparse.Namespace) -> int:
    rows = _load_rows(args.input_path)
    result = AssortmentLifecycleReplayStore(args.store_path).put_trajectory(
        dataset_hash=args.dataset_hash,
        model_version=args.model_version,
        policy_hash=args.policy_hash,
        period_from=args.period_from,
        period_to=args.period_to,
        rows=rows,
        metadata=_load_optional_object(args.metadata_json),
    )
    payload = {
        "status": "reused" if result.reused else "stored",
        "trajectory_hash": result.key,
        "content_sha256": result.content_sha256,
        "row_count": result.row_count,
        "production_action": "none_read_only",
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_export_trajectory(args: argparse.Namespace) -> int:
    rows = AssortmentLifecycleReplayStore(args.store_path).load_trajectory_rows(
        args.trajectory_hash
    )
    if args.output_path.suffix.casefold() == ".csv":
        _write_csv(args.output_path, rows)
    else:
        _write_json(
            args.output_path,
            {
                "schema": "assortment_lifecycle_replay_trajectory_export.v1",
                "trajectory_hash": args.trajectory_hash,
                "rows": rows,
                "production_action": "none_read_only",
            },
        )
    print(
        json.dumps(
            {
                "status": "exported",
                "trajectory_hash": args.trajectory_hash,
                "row_count": len(rows),
                "output_path": str(args.output_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create the append-only SQLite schema")
    init.set_defaults(func=_cmd_init)

    manifest = commands.add_parser("manifest", help="List stored datasets and trajectories")
    manifest.add_argument("--output-json", type=Path)
    manifest.set_defaults(func=_cmd_manifest)

    put = commands.add_parser(
        "put-trajectory", help="Store a legacy or v2 trajectory for an existing dataset"
    )
    put.add_argument("--dataset-hash", required=True)
    put.add_argument("--model-version", required=True)
    put.add_argument("--policy-hash", required=True)
    put.add_argument("--period-from", type=_date, required=True)
    put.add_argument("--period-to", type=_date, required=True)
    put.add_argument("--input-path", type=Path, required=True)
    put.add_argument("--metadata-json", type=Path)
    put.set_defaults(func=_cmd_put_trajectory)

    export = commands.add_parser("export-trajectory", help="Export by immutable hash")
    export.add_argument("--trajectory-hash", required=True)
    export.add_argument("--output-path", type=Path, required=True)
    export.set_defaults(func=_cmd_export_trajectory)
    return parser.parse_args(argv)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_rows = payload.get("rows") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_rows, list) or not all(isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("replay_trajectory_rows_required")
    return [dict(row) for row in raw_rows]


def _load_optional_object(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("replay_trajectory_metadata_must_be_object")
    return dict(payload)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            {
                key: (
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (list, dict))
                    else value
                )
                for key, value in row.items()
            }
            for row in rows
        )


def _date(value: str):
    from datetime import date

    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
