from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "benchmark_llm_context",
    ROOT / "scripts/benchmark_llm_context.py",
)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


TASK = {
    "query": "Где правила SKU?",
    "expected_document": "docs/sku_policy.md",
    "expected_code": "app/services/sku.py",
    "expected_test": "tests/test_sku.py",
}

ROUTING_CASES = (
    (
        "Найди реализацию сопоставления товаров конкурентов и релевантные тесты.",
        "pricing-service/docs/TechDesign.CompetitorMatching.md",
    ),
    (
        "Где находятся правила генерации SKU, реализация и базовые тесты?",
        "pricing-service/docs/sku_policy.md",
    ),
    (
        "В pricing-service найди market research agents demand DeviceModelRepository "
        "и Yandex Direct: дизайн, backend и тесты.",
        "pricing-service/docs/TechDesign.AgentsMarketDemand.md",
    ),
    (
        "Где зафиксировано архитектурное оздоровление pricing-service, какие "
        "dependency boundaries меняются и чем они проверяются?",
        "pricing-service/docs/specs/pricing-service-architecture-hardening.md",
    ),
)


def test_router_prompt_uses_lightweight_worktree_context() -> None:
    repo = ROOT.resolve()
    prompt = benchmark.build_prompt(TASK, "router", "unused", repo)

    assert "--limit 3" in prompt
    assert "--max-bytes 6144" in prompt
    assert f"pricing-service={repo}" in prompt
    assert "не загружай `$pricing-service-workflows`" in prompt


@pytest.mark.parametrize(("query", "expected_document"), ROUTING_CASES)
def test_problem_routes_return_canonical_document_first(
    query: str,
    expected_document: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "/opt/MM/scripts/mm_context.py",
            "--query",
            query,
            "--project",
            "pricing-service",
            "--limit",
            "3",
            "--max-bytes",
            "6144",
            "--format",
            "json",
            "--project-root",
            f"pricing-service={ROOT}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["documents"][0]["path"] == expected_document


def test_parse_events_tracks_tool_output_and_warnings() -> None:
    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "/bin/bash -lc 'rg pattern app'",
                        "aggregated_output": "result\n",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "docs/sku_policy.md app/services/sku.py tests/test_sku.py"
                        ),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 123, "output_tokens": 10},
                }
            ),
        ]
    )

    parsed = benchmark.parse_events(raw, TASK)

    assert parsed["correctness"] is True
    assert parsed["tool_output_bytes"] == len(b"result\n")
    assert parsed["max_tool_output_bytes"] == len(b"result\n")
    assert parsed["navigation_budget_warnings"]


def test_baseline_comparison_applies_fixed_gate(tmp_path: Path) -> None:
    summary = {
        "tasks": 10,
        "completed": 10,
        "correct": 9,
        "median_input_tokens": 130_000,
        "median_tool_calls": 11.5,
    }
    baseline = {"median_input_tokens": 190_228.5, "median_tool_calls": 16.5}
    baseline_path = tmp_path / "baseline.json"

    benchmark.add_baseline_comparison(summary, baseline, baseline_path)

    assert summary["comparison"]["passed"] is True
    assert summary["comparison"]["limits"]["median_input_tokens"] == pytest.approx(
        133_159.95
    )
    assert summary["comparison"]["limits"]["median_tool_calls"] == pytest.approx(11.55)


@pytest.mark.parametrize("value", ["../bad", "has space", "", "-leading"])
def test_run_label_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="run-label"):
        benchmark.validate_run_label(value)
