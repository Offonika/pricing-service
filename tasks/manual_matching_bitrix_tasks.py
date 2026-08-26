from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from app.infrastructure.db import session_scope
from app.services.manual_matching_bitrix_tasks import (
    DEFAULT_CREATED_BY_ID,
    DEFAULT_GROUP_ID,
    build_manual_matching_bitrix_task_drafts,
)
from app.services.manual_matching_control import (
    build_manual_matching_control_report,
    report_date_today,
)

DEFAULT_STATE_PATH = Path(".local/manual_matching_bitrix_tasks_state.json")
DEFAULT_REPORT_DIR = Path("reports/manual_matching_control")
ENV_KEYS = (
    "CONTRACTOR_PROJECT_REPORT_BITRIX_WEBHOOK_BASE",
    "BITRIX_BOX_WEBHOOK_BASE",
    "BITRIX24_BOX_WEBHOOK_URL",
    "BITRIX_WEBHOOK_BASE",
    "BITRIX24_WEBHOOK_URL",
)
ENV_FILES = (
    Path("/opt/MM/pricing-service/.env"),
    Path("/etc/mm-management-orchestrator.env"),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draft or create daily Bitrix tasks for manual competitor matching."
    )
    parser.add_argument(
        "--date",
        dest="report_date",
        help="Task/report date in YYYY-MM-DD; defaults to today in Europe/Moscow.",
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for tests or one-off local runs.",
    )
    parser.add_argument(
        "--state-path",
        default=str(DEFAULT_STATE_PATH),
        help="JSON state file used for idempotent Bitrix task creation.",
    )
    parser.add_argument(
        "--matching-url",
        help="Optional URL of the manual matching UI to include in task text.",
    )
    parser.add_argument(
        "--group-id",
        type=int,
        default=DEFAULT_GROUP_ID,
        help="Bitrix workgroup/project id. Use 0 to create tasks without a group.",
    )
    parser.add_argument(
        "--created-by-id",
        type=int,
        default=DEFAULT_CREATED_BY_ID,
        help="Bitrix user id used as task creator.",
    )
    parser.add_argument(
        "--auditors",
        default="",
        help="Comma-separated observer ids. Empty by default for this contour.",
    )
    parser.add_argument(
        "--exclude-responsible-ids",
        default=os.getenv("MANUAL_MATCHING_TASK_EXCLUDED_RESPONSIBLE_IDS", ""),
        help=(
            "Comma-separated responsible ids to skip. Defaults to "
            "MANUAL_MATCHING_TASK_EXCLUDED_RESPONSIBLE_IDS."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create tasks in Bitrix. Without this flag the command is a dry-run.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    bitrix_call_func: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    args = parse_args(argv)
    target_date = date.fromisoformat(args.report_date) if args.report_date else report_date_today()
    report_path = DEFAULT_REPORT_DIR / f"{target_date.isoformat()}.md"

    with session_scope(database_url=args.database_url, read_only=True) as session:
        report = build_manual_matching_control_report(session, report_date=target_date)

    drafts = build_manual_matching_bitrix_task_drafts(
        report,
        group_id=args.group_id,
        created_by_id=args.created_by_id,
        auditors=tuple(_parse_csv_ints(args.auditors)),
        matching_url=args.matching_url,
        report_path=report_path,
    )
    excluded_responsible_ids = set(_parse_csv_ints(args.exclude_responsible_ids))
    drafts = [draft for draft in drafts if draft.responsible_id not in excluded_responsible_ids]

    state_path = Path(args.state_path)
    state = _load_state(state_path)
    base_url = resolve_webhook() if args.apply else None
    call = bitrix_call_func or bitrix_call
    tasks = []

    for draft in drafts:
        state_item = state.get("tasks", {}).get(draft.key)
        if state_item:
            tasks.append(_task_result(draft, "skipped_state", task_id=state_item.get("task_id")))
            continue

        if not args.apply:
            tasks.append(_task_result(draft, "would_create"))
            continue

        assert base_url is not None
        existing_id = find_existing_task_id(
            base_url,
            title=draft.title,
            responsible_id=draft.responsible_id,
            report_date=target_date,
            bitrix_call_func=call,
        )
        if existing_id is not None:
            _remember_task(state, draft.key, existing_id, draft=draft)
            tasks.append(_task_result(draft, "skipped_existing_bitrix", task_id=existing_id))
            _save_state(state_path, state)
            continue

        created = call(base_url, "tasks.task.add", {"fields": draft.bitrix_fields()})
        task_id = _extract_task_id(created)
        _remember_task(state, draft.key, task_id, draft=draft)
        tasks.append(_task_result(draft, "created", task_id=task_id))
        _save_state(state_path, state)

    result = {
        "date": target_date.isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "created_by_id": args.created_by_id,
        "group_id": args.group_id,
        "auditors": _parse_csv_ints(args.auditors),
        "excluded_responsible_ids": sorted(excluded_responsible_ids),
        "state_path": str(state_path),
        "summary": {
            "planned_tasks": len(tasks),
            "created": sum(1 for item in tasks if item["action"] == "created"),
            "would_create": sum(1 for item in tasks if item["action"] == "would_create"),
            "skipped": sum(1 for item in tasks if item["action"].startswith("skipped")),
        },
        "queue": {
            "total": report["summary"]["queue_total"],
            "display": report["summary"]["queue_display"],
        },
        "tasks": tasks,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return result


def load_env_files() -> None:
    for path in ENV_FILES:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_webhook() -> str:
    load_env_files()
    for key in ENV_KEYS:
        value = (os.getenv(key) or "").strip().rstrip("/")
        if value:
            return value
    raise RuntimeError("Bitrix webhook is not configured in env")


def bitrix_call(base_url: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + f"/{method}.json",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bitrix HTTP {exc.code} for {method}: {body[:500]}") from exc

    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(
            f"Bitrix {method}: {data.get('error')} {data.get('error_description', '')}".strip()
        )
    return data


def find_existing_task_id(
    base_url: str,
    *,
    title: str,
    responsible_id: int,
    report_date: date,
    bitrix_call_func: Callable[[str, str, dict[str, Any]], dict[str, Any]] = bitrix_call,
) -> int | None:
    response = bitrix_call_func(
        base_url,
        "tasks.task.list",
        {
            "filter": {
                "RESPONSIBLE_ID": responsible_id,
                ">=CREATED_DATE": f"{report_date.isoformat()}T00:00:00+03:00",
                "<=CREATED_DATE": f"{report_date.isoformat()}T23:59:59+03:00",
            },
            "select": ["ID", "TITLE", "RESPONSIBLE_ID", "CREATED_DATE"],
            "order": {"ID": "DESC"},
        },
    )
    for task in _extract_task_list(response):
        if _task_value(task, "title", "TITLE") == title:
            raw_id = _task_value(task, "id", "ID")
            return int(raw_id) if raw_id is not None else None
    return None


def _extract_task_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result")
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        tasks = result.get("tasks") or result.get("TASKS")
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, dict)]
    return []


def _extract_task_id(response: dict[str, Any]) -> int:
    result = response.get("result")
    if isinstance(result, int):
        return result
    if isinstance(result, str) and result.isdigit():
        return int(result)
    if isinstance(result, dict):
        task = result.get("task") or result.get("TASK")
        if isinstance(task, dict):
            raw_id = _task_value(task, "id", "ID")
            if raw_id is not None:
                return int(raw_id)
        for key in ("taskId", "TASK_ID", "ID", "id"):
            raw_id = result.get(key)
            if raw_id is not None:
                return int(raw_id)
    raise RuntimeError("Bitrix tasks.task.add returned empty result")


def _task_value(task: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in task:
            return task.get(name)
    return None


def _task_result(
    draft: Any,
    action: str,
    *,
    task_id: int | str | None = None,
) -> dict[str, Any]:
    return {
        "key": draft.key,
        "action": action,
        "task_id": int(task_id) if task_id not in (None, "") else None,
        "title": draft.title,
        "responsible_id": draft.responsible_id,
        "responsible_name": draft.responsible_name,
        "deadline": draft.deadline,
        "plan": draft.plan,
        "task_focus": draft.task_focus,
        "fields": draft.bitrix_fields(),
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"tasks": {}}
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        data["tasks"] = {}
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _remember_task(
    state: dict[str, Any],
    key: str,
    task_id: int,
    *,
    draft: Any,
) -> None:
    state.setdefault("tasks", {})[key] = {
        "task_id": task_id,
        "title": draft.title,
        "responsible_id": draft.responsible_id,
        "deadline": draft.deadline,
        "plan": draft.plan,
        "task_focus": draft.task_focus,
    }


def _parse_csv_ints(raw: str | None) -> list[int]:
    if not raw:
        return []
    values: list[int] = []
    for chunk in raw.split(","):
        text = chunk.strip()
        if text:
            values.append(int(text))
    return values


if __name__ == "__main__":
    main()
