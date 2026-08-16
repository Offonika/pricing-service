from __future__ import annotations

from tasks.bootstrap_display_family_registry import build_parser, run


def test_apply_requires_explicit_audit_actor_before_database_access() -> None:
    args = build_parser().parse_args(["--apply"])

    exit_code, payload = run(args)

    assert exit_code == 2
    assert payload == {"status": "blocked", "error": "--actor is required with --apply"}


def test_rollback_apply_requires_reason_before_database_access() -> None:
    args = build_parser().parse_args(
        ["--apply", "--actor", "test-user", "--rollback-to-version", "1"]
    )

    exit_code, payload = run(args)

    assert exit_code == 2
    assert payload == {"status": "blocked", "error": "--reason is required for rollback"}
