#!/usr/bin/env python3
"""Run reproducible read-only Codex navigation benchmarks for pricing-service."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = REPO_ROOT / "tests" / "fixtures" / "llm_context_pilot_tasks.json"
DEFAULT_OUTPUT = Path("/data/llm-context-bench/results")
TOOL_ITEM_TYPES = {
    "command_execution",
    "dynamic_tool_call",
    "file_change",
    "mcp_tool_call",
    "tool_call",
}
PATH_RE = re.compile(
    r"(?<![\w.-])((?:\.agents|app|docs|infra|scripts|tasks|tests|ui)/[^\s'\";&|)]+)"
)
RUN_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def load_tasks(path: Path, *, graph_only: bool, selected: set[str]) -> list[dict[str, Any]]:
    tasks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks file must contain a non-empty JSON array")
    result = []
    for task in tasks:
        if selected and task["id"] not in selected:
            continue
        if graph_only and not task.get("structural", False):
            continue
        result.append(task)
    if not result:
        raise ValueError("no benchmark tasks selected")
    return result


def build_prompt(
    task: dict[str, Any],
    mode: str,
    graph_project: str,
    repo: Path,
) -> str:
    mode_instruction = ""
    if mode in {"router", "graph"}:
        query = shlex.quote(str(task["query"]))
        project_root = shlex.quote(f"pricing-service={repo}")
        mode_instruction = (
            "Запусти root-router ровно один раз: `cd /opt/MM && python "
            f"scripts/mm_context.py --query {query} --project auto --limit 3 "
            f"--max-bytes 6144 --format text --project-root {project_root}`. "
            "Это read-only navigation: не загружай `$pricing-service-workflows` и "
            "его references. Пути `pricing-service/` сопоставляй с текущим checkout; "
            "не читай `/opt/MM/pricing-service`. Мягкий ориентир — до 8 tool calls, "
            "один canonical document, три code paths и три test paths. Используй "
            "адресные `rg -l` или `rg -n --max-count 20` и окна до 160 строк. "
        )
    if mode == "baseline":
        mode_instruction = (
            "Это baseline без root-router и code graph: не запускай "
            "`/opt/MM/scripts/mm_context.py` и не используй MCP. Следуй только "
            "инструкциям текущего репозитория. "
        )
    if mode == "graph":
        mode_instruction += (
            "Для первичного структурного поиска используй только MCP "
            f"`codebase_memory_pilot` и его project `{graph_project}`; затем обязательно "
            "открой найденный исходник обычной адресной командой. "
        )
    return (
        "Benchmark run, только read-only. Ничего не изменяй, не запускай тесты и не "
        "обращайся к внешним системам. "
        f"{mode_instruction}"
        f"Задача: {task['query']} "
        "В финале дай один canonical документ, не более трёх code paths и трёх test paths."
    )


def codex_command(
    *, repo: Path, mode: str, model: str, prompt: str
) -> tuple[list[str], dict[str, str]]:
    common = [
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "-m",
        model,
        "-c",
        'model_reasoning_effort="medium"',
        "-s",
        "read-only",
        "--json",
        prompt,
    ]
    env = os.environ.copy()
    if mode == "graph":
        cbm_bin = env.get("CBM_BIN", str(Path.home() / ".local" / "bin" / "codebase-memory-mcp"))
        cache_dir = env.get("CBM_CACHE_DIR", "/data/llm-context-bench/cbm-cache")
        command = [
            "codex",
            "-c",
            f"mcp_servers.codebase_memory_pilot.command={json.dumps(cbm_bin)}",
            "-c",
            "mcp_servers.codebase_memory_pilot.env="
            f"{{CBM_ALLOWED_ROOT={json.dumps(str(repo))},"
            f"CBM_CACHE_DIR={json.dumps(cache_dir)}}}",
            "-c",
            "mcp_servers.codebase_memory_pilot.enabled_tools="
            '["index_repository","index_status","search_graph","trace_path",'
            '"detect_changes","get_code_snippet","get_architecture"]',
            *common,
        ]
    else:
        command = ["codex", *common]
    return command, env


def parse_events(raw: str, task: dict[str, Any]) -> dict[str, Any]:
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    final_message = ""
    tool_calls = 0
    commands: list[str] = []
    tool_output_bytes = 0
    max_tool_output_bytes = 0
    usage: dict[str, int] = {}
    for event in events:
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message":
                final_message = str(item.get("text") or "")
            if item_type in TOOL_ITEM_TYPES:
                tool_calls += 1
            if item_type == "command_execution" and item.get("command"):
                commands.append(str(item["command"]))
                output_bytes = len(str(item.get("aggregated_output") or "").encode("utf-8"))
                tool_output_bytes += output_bytes
                max_tool_output_bytes = max(max_tool_output_bytes, output_bytes)
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}

    files = sorted(
        {
            match.group(1).rstrip(".,:]}>")
            for command in commands
            for match in PATH_RE.finditer(command)
        }
    )
    expected = {
        "document": task["expected_document"],
        "code": task["expected_code"],
        "test": task["expected_test"],
    }
    lower_message = final_message.lower()
    checks = {key: value.lower() in lower_message for key, value in expected.items()}
    warnings = navigation_budget_warnings(commands, tool_calls)
    return {
        "usage": usage,
        "tool_calls": tool_calls,
        "files": files,
        "file_count": len(files),
        "tool_output_bytes": tool_output_bytes,
        "max_tool_output_bytes": max_tool_output_bytes,
        "navigation_budget_warnings": warnings,
        "correctness": all(checks.values()),
        "correctness_checks": checks,
        "final_message": final_message,
        "event_count": len(events),
    }


def navigation_budget_warnings(commands: list[str], tool_calls: int) -> list[str]:
    warnings: list[str] = []
    if tool_calls > 8:
        warnings.append(f"tool_calls={tool_calls} exceeds soft limit 8")
    for index, command in enumerate(commands, start=1):
        match = re.search(r"(?:^|[\s'\"])(?:rg|ripgrep)(?:[\s'\"])", command)
        if not match:
            continue
        rg_arguments = command[match.end() :]
        if not (
            re.search(r"(?:^|\s)-l(?:\s|$)", rg_arguments)
            or "--files-with-matches" in rg_arguments
            or "--max-count" in rg_arguments
        ):
            warnings.append(f"command {index}: rg output is not bounded")
        if re.search(r"(?:^|\s)(?:\.|app/?|tests/?)(?:[\s'\"]|$)", rg_arguments):
            warnings.append(f"command {index}: rg uses a broad top-level path")
    return warnings


def run_task(
    *,
    task: dict[str, Any],
    mode: str,
    repo: Path,
    output_dir: Path,
    model: str,
    timeout: int,
    graph_project: str,
) -> dict[str, Any]:
    prompt = build_prompt(task, mode, graph_project, repo)
    command, env = codex_command(repo=repo, mode=mode, model=model, prompt=prompt)
    raw_dir = output_dir / "raw" / mode
    raw_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
        error = None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        exit_code = 124
        error = f"timeout after {timeout}s"
    wall_seconds = round(time.monotonic() - started, 3)
    (raw_dir / f"{task['id']}.jsonl").write_text(stdout, encoding="utf-8")
    if stderr:
        (raw_dir / f"{task['id']}.stderr.txt").write_text(stderr, encoding="utf-8")

    parsed = parse_events(stdout, task)
    return {
        "task_id": task["id"],
        "mode": mode,
        "query": task["query"],
        "expected_document": task["expected_document"],
        "expected_code": task["expected_code"],
        "expected_test": task["expected_test"],
        "structural": bool(task.get("structural", False)),
        "model": model,
        "reasoning_effort": "medium",
        "exit_code": exit_code,
        "error": error,
        "wall_seconds": wall_seconds,
        **parsed,
    }


def median(results: list[dict[str, Any]], key: str) -> float | None:
    values = [result[key] for result in results if isinstance(result.get(key), (int, float))]
    return statistics.median(values) if values else None


def build_summary(results: list[dict[str, Any]], *, mode: str, model: str) -> dict[str, Any]:
    normalized = []
    for result in results:
        usage = result.get("usage") or {}
        normalized.append(
            {
                **result,
                "input_tokens": usage.get("input_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }
        )
    completed = [result for result in normalized if result["exit_code"] == 0]
    return {
        "mode": mode,
        "model": model,
        "reasoning_effort": "medium",
        "tasks": len(normalized),
        "completed": len(completed),
        "correct": sum(bool(result["correctness"]) for result in normalized),
        "median_input_tokens": median(completed, "input_tokens"),
        "median_cached_input_tokens": median(completed, "cached_input_tokens"),
        "median_tool_calls": median(completed, "tool_calls"),
        "median_file_count": median(completed, "file_count"),
        "median_tool_output_bytes": median(completed, "tool_output_bytes"),
        "max_tool_output_bytes": max(
            (result["max_tool_output_bytes"] for result in completed),
            default=0,
        ),
        "navigation_budget_warning_count": sum(
            len(result["navigation_budget_warnings"]) for result in completed
        ),
        "median_wall_seconds": median(completed, "wall_seconds"),
        "results": normalized,
    }


def percent_change(current: float, baseline: float) -> float:
    if baseline == 0:
        raise ValueError("baseline metric must be non-zero")
    return round((current - baseline) * 100 / baseline, 3)


def add_baseline_comparison(
    summary: dict[str, Any],
    baseline: dict[str, Any],
    baseline_path: Path,
) -> None:
    baseline_tokens = float(baseline["median_input_tokens"])
    baseline_calls = float(baseline["median_tool_calls"])
    token_limit = baseline_tokens * 0.70
    tool_call_limit = baseline_calls * 0.70
    current_tokens = float(summary["median_input_tokens"])
    current_calls = float(summary["median_tool_calls"])
    checks = {
        "all_tasks_completed": summary["completed"] == summary["tasks"],
        "median_input_tokens_reduced_30pct": current_tokens <= token_limit,
        "median_tool_calls_reduced_30pct": current_calls <= tool_call_limit,
        "correctness_at_least_9_of_10": summary["correct"] >= 9,
    }
    summary["comparison"] = {
        "baseline_summary": str(baseline_path),
        "input_tokens_change_pct": percent_change(current_tokens, baseline_tokens),
        "tool_calls_change_pct": percent_change(current_calls, baseline_calls),
        "limits": {
            "median_input_tokens": token_limit,
            "median_tool_calls": tool_call_limit,
            "correct": 9,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def validate_run_label(value: str) -> str:
    if not RUN_LABEL_RE.fullmatch(value):
        raise ValueError("run-label must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "router", "graph"), required=True)
    parser.add_argument("--run-label", required=True, type=validate_run_label)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--tasks-file", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--graph-project", default="pricing-service-router-bench")
    parser.add_argument("--baseline-summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    output_dir = args.output_dir.resolve() / args.run_label
    tasks = load_tasks(
        args.tasks_file.resolve(),
        graph_only=args.mode == "graph",
        selected=set(args.only),
    )
    summary_path = output_dir / f"summary-{args.mode}.json"
    raw_dir = output_dir / "raw" / args.mode
    if summary_path.exists() or raw_dir.exists():
        raise ValueError(f"run output already exists for label={args.run_label}, mode={args.mode}")
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as executor:
        futures = {
            executor.submit(
                run_task,
                task=task,
                mode=args.mode,
                repo=repo,
                output_dir=output_dir,
                model=args.model,
                timeout=args.timeout,
                graph_project=args.graph_project,
            ): task["id"]
            for task in tasks
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                json.dumps(
                    {
                        "task_id": result["task_id"],
                        "exit_code": result["exit_code"],
                        "correctness": result["correctness"],
                        "usage": result["usage"],
                        "tool_calls": result["tool_calls"],
                        "wall_seconds": result["wall_seconds"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    results.sort(key=lambda item: item["task_id"])
    summary = build_summary(results, mode=args.mode, model=args.model)
    if args.baseline_summary:
        baseline_path = args.baseline_summary.resolve()
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        add_baseline_comparison(summary, baseline, baseline_path)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, indent=2))
    print(f"summary: {summary_path}")
    completed = summary["completed"] == summary["tasks"]
    gate_passed = summary.get("comparison", {}).get("passed", True)
    return 0 if completed and gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
