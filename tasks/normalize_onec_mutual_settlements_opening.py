from __future__ import annotations

import argparse
from pathlib import Path

from app.services.importers.onec_mutual_settlements import (
    export_onec_mutual_settlements_opening_csv,
    parse_onec_mutual_settlements_opening_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize 1C mutual settlements report into flat opening-balance CSV"
    )
    parser.add_argument("input_path", type=Path, help="Path to source .xlsx report from 1C")
    parser.add_argument(
        "--output-path",
        type=Path,
        help="Where to write normalized CSV; defaults to <input>.normalized.csv",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    input_path: Path = args.input_path
    output_path: Path = args.output_path or input_path.with_suffix(".normalized.csv")

    rows = parse_onec_mutual_settlements_opening_file(input_path)
    export_onec_mutual_settlements_opening_csv(rows, output_path)

    print(f"normalized_rows={len(rows)}")
    print(f"output_path={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
