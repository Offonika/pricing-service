from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

KIND_OLD = "11111111-1111-1111-1111-111111111111"
KIND_NEW = "22222222-2222-2222-2222-222222222222"
GROUP_OLD = "33333333-3333-3333-3333-333333333333"
GROUP_NEW = "44444444-4444-4444-4444-444444444444"
CATEGORY_OLD = "55555555-5555-5555-5555-555555555555"
CATEGORY_NEW = "66666666-6666-6666-6666-666666666666"
NOMENCLATURE = "77777777-7777-7777-7777-777777777777"


def _input_payload() -> dict[str, object]:
    return {
        "items": [
            {
                "idempotency_key": "nom-class:РБ000001:decision-42:r1",
                "nomenclature_code": "РБ000001",
                "nomenclature_guid": NOMENCLATURE,
                "expected_kind": {"guid": KIND_OLD, "code": "OLD-KIND"},
                "target_kind": {"guid": KIND_NEW, "code": "NEW-KIND"},
                "expected_group": {"guid": GROUP_OLD, "code": "OLD-GROUP"},
                "target_group": {"guid": GROUP_NEW, "code": "NEW-GROUP"},
                "group_mode": "set",
                "category_mode": "replace_expected",
                "expected_category": {"guid": CATEGORY_OLD, "code": "OLD-CATEGORY"},
                "target_category": {"guid": CATEGORY_NEW, "code": "NEW-CATEGORY"},
                "reason": "Утверждённое наведение порядка",
            }
        ]
    }


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tasks.export_ut103_nomenclature_classifications", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_validate_only_computes_hash_without_database_or_exchange(tmp_path: Path) -> None:
    input_path = tmp_path / "classification.json"
    input_path.write_text(json.dumps(_input_payload(), ensure_ascii=False), encoding="utf-8")

    result = _run(
        "validate-only",
        "--input-json",
        str(input_path),
        "--approved-by",
        "115204",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["validated_only"] is True
    assert len(payload["command_hash"]) == 64
    assert len(payload["items"][0]["decision_hash"]) == 64
    assert not list(tmp_path.rglob("*.xml"))


def test_input_cannot_supply_service_managed_identity(tmp_path: Path) -> None:
    payload = _input_payload()
    payload["items"][0]["decision_hash"] = "a" * 64  # type: ignore[index]
    input_path = tmp_path / "classification.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _run(
        "validate-only",
        "--input-json",
        str(input_path),
        "--approved-by",
        "115204",
    )

    assert result.returncode != 0
    assert "service-managed input fields are forbidden" in result.stderr


def test_legacy_direct_apply_flags_are_not_available() -> None:
    result = _run("--mode", "apply", "--message-id", "arbitrary")

    assert result.returncode == 2
    assert "invalid choice" in result.stderr or "required" in result.stderr


def test_row_level_approved_by_is_rejected(tmp_path: Path) -> None:
    payload = _input_payload()
    payload["items"][0]["ApprovedBy"] = "115204"  # type: ignore[index]
    input_path = tmp_path / "classification.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _run(
        "validate-only",
        "--input-json",
        str(input_path),
        "--approved-by",
        "115204",
    )

    assert result.returncode != 0
    assert "ApprovedBy is allowed only at command/header level" in result.stderr


def test_validate_only_accepts_explicit_recovery_with_empty_targets(tmp_path: Path) -> None:
    payload = _input_payload()
    item = payload["items"][0]  # type: ignore[index]
    item["idempotency_key"] = "nom-class:РБ000001:restore:r2"
    item["expected_group"] = {"guid": GROUP_NEW, "code": "NEW-GROUP"}
    item["target_group"] = {}
    item["group_mode"] = "clear_expected"
    item["expected_category"] = {"guid": CATEGORY_NEW, "code": "NEW-CATEGORY"}
    item["target_category"] = {}
    item["category_mode"] = "remove_expected"
    input_path = tmp_path / "restore.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _run(
        "validate-only",
        "--input-json",
        str(input_path),
        "--approved-by",
        "115204",
    )

    assert result.returncode == 0, result.stderr
    canonical = json.loads(result.stdout)["canonical_payload"]["items"][0]
    assert canonical["group_mode"] == "clear_expected"
    assert canonical["target_group"] == {"code": "", "guid": "", "name": ""}
    assert canonical["category_mode"] == "remove_expected"
    assert canonical["target_category"] == {"code": "", "guid": "", "name": ""}
