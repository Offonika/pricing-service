"""Safely bootstrap or roll back the accepted display-family registry."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.db import get_application_session_factory
from app.services.display_family_registry import (
    APPROVED_BUNDLE_PATH,
    DisplayFamilyRegistryError,
    active_display_family_registry_version,
    apply_display_family_bootstrap,
    build_display_family_bootstrap_plan,
    load_approved_display_family_bundle,
    plan_display_family_registry_rollback,
    readback_display_family_registry_version,
    rollback_display_family_registry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and bootstrap the exact accepted display-family preflight v2 bundle. "
            "The default mode is a read-only dry-run."
        )
    )
    parser.add_argument("--bundle", type=Path, default=APPROVED_BUNDLE_PATH)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the validated bootstrap or rollback atomically to the application DB",
    )
    parser.add_argument("--actor", help="Required audit actor for --apply")
    parser.add_argument("--reason", help="Required for rollback; optional bootstrap override")
    parser.add_argument(
        "--readback",
        action="store_true",
        help="Read back the current active version without loading a bundle",
    )
    parser.add_argument(
        "--rollback-to-version",
        type=int,
        help="Plan or apply a non-destructive switch to an existing version number",
    )
    parser.add_argument(
        "--effective-at",
        type=date.fromisoformat,
        default=date.today(),
        help="Effective date for a rollback event (YYYY-MM-DD)",
    )
    return parser


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str, sort_keys=True))


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.readback and args.rollback_to_version is not None:
        return 2, {
            "status": "blocked",
            "error": "choose either --readback or --rollback-to-version",
        }
    if args.readback and args.apply:
        return 2, {"status": "blocked", "error": "--readback never accepts --apply"}
    if args.apply and not str(args.actor or "").strip():
        return 2, {"status": "blocked", "error": "--actor is required with --apply"}
    if args.apply and args.rollback_to_version is not None and not str(args.reason or "").strip():
        return 2, {"status": "blocked", "error": "--reason is required for rollback"}

    session_factory = get_application_session_factory()
    try:
        if args.readback:
            with session_factory() as session:
                version = active_display_family_registry_version(session)
                if version is None:
                    return 2, {"status": "blocked", "error": "active registry version is missing"}
                readback = readback_display_family_registry_version(session, version)
            return (0 if readback["ok"] else 2), {
                "status": "ok" if readback["ok"] else "blocked",
                "dry_run": True,
                "readback": readback,
            }

        if args.rollback_to_version is not None:
            if args.rollback_to_version <= 0:
                return 2, {"status": "blocked", "error": "rollback version must be positive"}
            if args.apply:
                with session_factory() as session:
                    result = rollback_display_family_registry(
                        session,
                        args.rollback_to_version,
                        actor=str(args.actor),
                        reason=str(args.reason),
                        effective_at=args.effective_at,
                    )
                return 0, {"status": "applied", "dry_run": False, **result}
            with session_factory() as session:
                plan = plan_display_family_registry_rollback(session, args.rollback_to_version)
            return (0 if plan["ready"] else 2), {
                "status": "ready" if plan["ready"] else "blocked",
                "dry_run": True,
                "plan": plan,
            }

        bundle = load_approved_display_family_bundle(args.bundle)
        if args.apply:
            with session_factory() as session:
                kwargs: dict[str, Any] = {"actor": str(args.actor)}
                if args.reason:
                    kwargs["reason"] = str(args.reason)
                result = apply_display_family_bootstrap(session, bundle, **kwargs)
            return 0, {
                "status": "applied" if result["applied"] else "already_active",
                "dry_run": False,
                **result,
            }
        with session_factory() as session:
            plan = build_display_family_bootstrap_plan(session, bundle)
        return (0 if plan.ready else 2), {
            "status": "ready" if plan.ready else "blocked",
            "dry_run": True,
            "plan": plan.as_dict(),
        }
    except (DisplayFamilyRegistryError, SQLAlchemyError) as exc:
        return 2, {
            "status": "blocked",
            "dry_run": not args.apply,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code, payload = run(args)
    _print(payload)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
