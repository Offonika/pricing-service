#!/usr/bin/env python3
"""Run reproducible read-only Codex navigation benchmarks for pricing-service."""

from __future__ import annotations

import argparse
import json
import os
import re
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


def build_prompt(task: dict[str, Any], mode: str, graph_project: str) -> str:
    mode_instruction = ""
    if mode in {"router", "graph"}:
        mode_instruction = (
            "Обязательно запусти root-router командой `cd /opt/MM && python "
            'scripts/mm_context.py --query "<задача>" --project auto --limit 8 '
            "--max-bytes 12288 --format text`. Пути с префиксом `pricing-service/` "
            "сопоставляй с файлами текущего репозитория; не читай рабочий каталог "
            "`/opt/MM/pricing-service`. Используй `$pricing-service-workflows` и "
            "ровно один основной reference. "
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
        "В финале дай не более двух canonical документов, четырёх code paths и "
        "четырёх test paths."
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
        command = [str(repo / "scripts" / "codex_cbm_pilot.sh"), *common]
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
    return {
        "usage": usage,
        "tool_calls": tool_calls,
        "files": files,
        "file_count": len(files),
        "correctness": all(checks.values()),
        "correctness_checks": checks,
        "final_message": final_message,
        "event_count": len(events),
    }


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
    prompt = build_prompt(task, mode, graph_project)
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
        "median_wall_seconds": median(completed, "wall_seconds"),
        "results": normalized,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "router", "graph"), required=True)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--tasks-file", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--graph-project", default="pricing-service-router-bench")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    output_dir = args.output_dir.resolve()
    tasks = load_tasks(
        args.tasks_file.resolve(),
        graph_only=args.mode == "graph",
        selected=set(args.only),
    )
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
    summary_path = output_dir / f"summary-{args.mode}.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, indent=2))
    print(f"summary: {summary_path}")
    return 0 if summary["completed"] == summary["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
