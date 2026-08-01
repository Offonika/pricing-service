from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def _input_payload() -> dict[str, object]:
    return {
        "idempotency_key": "receivable-decision:1200:2494:7",
        "decision_id": "2494",
        "decision_hash": "a" * 64,
        "revision": "7",
        "counterparty_ref": "0X8FDA0025901E48EE11ED222EA7D9B21E",
        "counterparty_guid": "a7d9b21e-222e-11ed-8fda-0025901e48ee",
        "counterparty_code": "РБ030337",
        "counterparty_name": "Тестовый контрагент",
        "expected_current_limit": "100000.00",
        "expected_current_depth": 7,
        "new_limit": "150000.00",
        "new_depth": 14,
        "currency": "RUB",
        "reason": "Утверждено",
        "approved_by": "115204",
        "approved_at": "2026-07-28T10:00:00+03:00",
    }


def test_export_credit_terms_task_writes_atomic_ready_xml(tmp_path: Path) -> None:
    input_path = tmp_path / "decision.json"
    input_path.write_text(
        json.dumps(_input_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_credit_terms",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--entity-type-id",
            "1200",
            "--input-json",
            str(input_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    output = Path(summary["path"])
    revision_hash = hashlib.sha256(b"7").hexdigest()[:12]
    assert output.name == (
        f"onec_commands_rcd-1200-2494-{revision_hash}-" f"{'a' * 12}-dry-run.ready.xml"
    )
    root = ET.fromstring(output.read_bytes())
    assert root.findtext("Commands/Command/CommandType") == "set_credit_terms"
    assert root.findtext("Commands/Command/NewLimit") == "150000.00"
    assert root.findtext("Commands/Command/NewDepth") == "14"


def test_export_credit_terms_task_rejects_fractional_depth(tmp_path: Path) -> None:
    payload = _input_payload()
    payload["new_depth"] = 1.5
    input_path = tmp_path / "decision.json"
    input_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.export_ut103_credit_terms",
            "--exchange-root",
            str(tmp_path / "exchange"),
            "--input-json",
            str(input_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "new_depth must be an integer" in result.stderr
    assert not (tmp_path / "exchange" / "to_1c" / "new").exists()
