#!/usr/bin/env python3
"""Run disposable implementation smoke tasks for the pricing context pilot."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from benchmark_llm_context import parse_events, validate_run_label

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("/data/llm-context-bench/results")
DEFAULT_WORKTREES = Path("/opt/MM/.local/worktrees/llm-context-smoke")
DEFAULT_PYTHON = Path("/opt/MM/pricing-service/.venv/bin/python")


@dataclass(frozen=True)
class SmokeCase:
    task_id: str
    source_path: str
    original: str
    seeded: str
    test_node: str
    prompt: str
    expected_document: str
    expected_test: str


CASES = (
    SmokeCase(
        task_id="sku-uppercase-regression",
        source_path="app/services/sku.py",
        original="    cleaned = cleaned.upper()\n",
        seeded="    cleaned = cleaned.lower()\n",
        test_node="tests/test_sku.py::test_build_sku_and_validate",
        prompt=(
            "Исправь регрессию SKU: test_build_sku_and_validate ожидает uppercase SKU, "
            "но получает lowercase. Изменяй только production source, не тест."
        ),
        expected_document="docs/sku_policy.md",
        expected_test="tests/test_sku.py",
    ),
    SmokeCase(
        task_id="demand-impressions-regression",
        source_path="app/services/market_research/yandex_direct.py",
        original="                        impressions=stat.impressions,\n",
        seeded="                        impressions=stat.clicks or 0,\n",
        test_node="tests/test_demand_service.py::test_demand_service_saves_stats",
        prompt=(
            "Исправь регрессию DemandService: test_demand_service_saves_stats получает "
            "impressions=10 вместо 123. Изменяй только production source, не тест."
        ),
        expected_document="docs/TechDesign.AgentsMarketDemand.md",
        expected_test="tests/test_demand_service.py",
    ),
)


def run(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_test(python: Path, worktree: Path, node: str, timeout: int) -> dict[str, Any]:
    completed = run(
        [str(python), "-m", "pytest", "-q", node],
        cwd=worktree,
        timeout=timeout,
    )
    return {
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def apply_seed(worktree: Path, case: SmokeCase) -> None:
    path = worktree / case.source_path
    text = path.read_text(encoding="utf-8")
    if text.count(case.original) != 1:
        raise RuntimeError(f"seed source is not unique: {case.source_path}")
    path.write_text(text.replace(case.original, case.seeded, 1), encoding="utf-8")


def codex_command(case: SmokeCase, *, model: str, python: Path) -> list[str]:
    prompt = (
        "Implementation smoke в одноразовом Git worktree. Используй "
        "`$pricing-service-workflows`, root-router и один основной reference. "
        f"{case.prompt} Запусти `{python} -m pytest -q {case.test_node}`. "
        "Не обращайся к внешним системам и не читай `/opt/MM/pricing-service`; "
        "виртуальное окружение оттуда разрешено использовать только как Python runner."
    )
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "-m",
        model,
        "-c",
        'model_reasoning_effort="medium"',
        "-s",
        "workspace-write",
        "--json",
        prompt,
    ]


def run_case(
    case: SmokeCase,
    *,
    repo: Path,
    base_ref: str,
    worktree: Path,
    raw_dir: Path,
    python: Path,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    added = run(
        ["git", "worktree", "add", "--detach", str(worktree), base_ref],
        cwd=repo,
        timeout=timeout,
    )
    if added.returncode != 0:
        raise RuntimeError(f"cannot create smoke worktree: {added.stderr}")

    started = time.monotonic()
    result: dict[str, Any] = {"task_id": case.task_id, "case": asdict(case)}
    try:
        baseline_test = run_test(python, worktree, case.test_node, timeout)
        result["baseline_test"] = baseline_test
        if baseline_test["exit_code"] != 0:
            raise RuntimeError(f"baseline test is not green: {case.task_id}")

        apply_seed(worktree, case)
        seeded_test = run_test(python, worktree, case.test_node, timeout)
        result["seeded_test"] = seeded_test
        if seeded_test["exit_code"] == 0:
            raise RuntimeError(f"seed did not make the test red: {case.task_id}")

        completed = run(
            codex_command(case, model=model, python=python),
            cwd=worktree,
            timeout=timeout,
        )
        raw = completed.stdout
        (raw_dir / f"{case.task_id}.jsonl").write_text(raw, encoding="utf-8")
        if completed.stderr:
            (raw_dir / f"{case.task_id}.stderr.txt").write_text(
                completed.stderr,
                encoding="utf-8",
            )

        metrics = parse_events(
            raw,
            {
                "expected_document": case.expected_document,
                "expected_code": case.source_path,
                "expected_test": case.expected_test,
            },
        )
        post_test = run_test(python, worktree, case.test_node, timeout)
        status = run(["git", "status", "--porcelain"], cwd=worktree, timeout=timeout)
        clean = status.returncode == 0 and not status.stdout.strip()
        result.update(
            {
                "codex_exit_code": completed.returncode,
                "metrics": metrics,
                "post_test": post_test,
                "git_status": status.stdout,
                "clean_against_base": clean,
                "passed": completed.returncode == 0
                and post_test["exit_code"] == 0
                and clean,
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
        result["passed"] = False
    finally:
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        removed = run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo,
            timeout=timeout,
        )
        result["worktree_removed"] = removed.returncode == 0
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", required=True, type=validate_run_label)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--worktree-dir", type=Path, default=DEFAULT_WORKTREES)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    python = args.python.resolve()
    if not python.is_file():
        raise ValueError(f"python runner does not exist: {python}")

    output_dir = args.output_dir.resolve() / args.run_label / "smoke"
    raw_dir = output_dir / "raw"
    worktree_root = args.worktree_dir.resolve() / args.run_label
    summary_path = output_dir / "summary.json"
    if summary_path.exists() or raw_dir.exists() or worktree_root.exists():
        raise ValueError(f"smoke output already exists for label={args.run_label}")
    raw_dir.mkdir(parents=True)
    worktree_root.mkdir(parents=True)

    results = [
        run_case(
            case,
            repo=repo,
            base_ref=args.base_ref,
            worktree=worktree_root / case.task_id,
            raw_dir=raw_dir,
            python=python,
            model=args.model,
            timeout=args.timeout,
        )
        for case in CASES
    ]
    summary = {
        "model": args.model,
        "reasoning_effort": "medium",
        "base_ref": args.base_ref,
        "passed": all(result["passed"] for result in results),
        "results": results,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        worktree_root.rmdir()
    except OSError:
        pass
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary: {summary_path}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
