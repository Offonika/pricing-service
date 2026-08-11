"""Build a read-only order-entry lamp preview from a returns portrait CSV."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from app.domains.customer_price_types.advisories import build_order_return_lamp


def build_shadow(source: Path, output: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        source.open("r", encoding="utf-8-sig", newline="") as src,
        output.open("w", encoding="utf-8-sig", newline="") as dst,
    ):
        reader = csv.DictReader(src)
        writer = csv.DictWriter(
            dst,
            fieldnames=(
                "код_1с",
                "контрагент",
                "lamp_key",
                "severity",
                "title",
                "manager_action",
                "visible",
                "blocks_fulfillment",
            ),
        )
        writer.writeheader()
        for row in reader:
            lamp = build_order_return_lamp(
                character=row.get("характер"),
                period_mismatch=row.get("несоответствие_периодов"),
                behavior_group=row.get("группа_поведения"),
            )
            counts[lamp.key] += 1
            if not lamp.visible:
                continue
            writer.writerow(
                {
                    "код_1с": row.get("код_1с", ""),
                    "контрагент": row.get("контрагент", ""),
                    "lamp_key": lamp.key,
                    "severity": lamp.severity,
                    "title": lamp.title,
                    "manager_action": lamp.manager_action,
                    "visible": "true",
                    "blocks_fulfillment": "false",
                }
            )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    counts = build_shadow(args.source, args.output)
    print(f"shadow preview -> {args.output}")
    for key, count in sorted(counts.items()):
        print(f"  {key}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
