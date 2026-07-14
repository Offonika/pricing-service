#!/usr/bin/env python3
"""Export and validate the FastAPI OpenAPI contract."""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_OUTPUT = Path("openapi.yaml")


def render_openapi() -> str:
    """Return a deterministic YAML representation of the FastAPI OpenAPI schema."""

    from app.main import app

    schema: dict[str, Any] = app.openapi()
    return yaml.safe_dump(
        schema,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    )


def write_openapi(output: Path) -> None:
    output.write_text(render_openapi(), encoding="utf-8")


def check_openapi(output: Path) -> int:
    expected = render_openapi()
    if not output.exists():
        print(f"error: {output} does not exist; run scripts/export_openapi.py", file=sys.stderr)
        return 1

    actual = output.read_text(encoding="utf-8")
    if actual == expected:
        print(f"ok: {output} matches FastAPI schema")
        return 0

    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=str(output),
        tofile="generated-openapi.yaml",
    )
    print(
        f"error: {output} is out of date; run scripts/export_openapi.py",
        file=sys.stderr,
    )
    sys.stderr.writelines(diff)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="OpenAPI YAML path to write or check.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that the output file matches the generated schema without writing it.",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_openapi(args.output)

    write_openapi(args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
