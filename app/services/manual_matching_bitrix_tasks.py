from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.services.manual_matching_control import (
    DEFAULT_MANAGERS,
    MOSCOW_TZ,
    MatchingManager,
)

DEFAULT_CREATED_BY_ID = 115204
DEFAULT_GROUP_ID = 0
DEFAULT_DEADLINE_HOUR = 18


@dataclass(frozen=True)
class ManualMatchingBitrixTaskDraft:
    key: str
    title: str
    description: str
    responsible_id: int
    responsible_name: str
    group_id: int
    created_by_id: int
    deadline: str
    plan: int
    task_focus: str = "general"
    auditors: tuple[int, ...] = ()

    def bitrix_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "TITLE": self.title,
            "DESCRIPTION": self.description,
            "DESCRIPTION_IN_BBCODE": "N",
            "RESPONSIBLE_ID": self.responsible_id,
            "GROUP_ID": self.group_id,
            "CREATED_BY": self.created_by_id,
            "DEADLINE": self.deadline,
            "ALLOW_CHANGE_DEADLINE": "Y",
        }
        if self.auditors:
            fields["AUDITORS"] = list(self.auditors)
        return fields


def build_manual_matching_bitrix_task_drafts(
    report: dict[str, Any],
    *,
    managers: tuple[MatchingManager, ...] = DEFAULT_MANAGERS,
    group_id: int = DEFAULT_GROUP_ID,
    created_by_id: int = DEFAULT_CREATED_BY_ID,
    auditors: tuple[int, ...] = (),
    matching_url: str | None = None,
    report_path: str | Path | None = None,
) -> list[ManualMatchingBitrixTaskDraft]:
    report_date = date.fromisoformat(str(report["date"]))
    deadline = _deadline_iso(report_date)
    summary = report["summary"]
    manager_rows = {str(row["user_id"]): row for row in report["managers"]}
    default_report_path = Path("reports/manual_matching_control") / f"{report_date.isoformat()}.md"
    report_path_text = str(report_path or default_report_path)

    drafts: list[ManualMatchingBitrixTaskDraft] = []
    for manager in managers:
        row = manager_rows.get(manager.user_id)
        plan = int(row["plan"] if row else manager.daily_plan)
        title = (
            "[Закупки][Матчинг] Ручное сопоставление товаров конкурентов - "
            f"{manager.name} - {report_date.isoformat()}"
        )
        key = f"manual_matching:{report_date.isoformat()}:{manager.user_id}:plan{plan}"
        description = render_manual_matching_bitrix_task_description(
            manager_name=manager.name,
            plan=plan,
            report_date=report_date,
            queue_total=int(summary.get("queue_total") or 0),
            queue_display=int(summary.get("queue_display") or 0),
            report_path=report_path_text,
            task_focus=manager.task_focus,
            matching_url=matching_url,
        )
        drafts.append(
            ManualMatchingBitrixTaskDraft(
                key=key,
                title=title,
                description=description,
                responsible_id=int(manager.user_id),
                responsible_name=manager.name,
                group_id=group_id,
                created_by_id=created_by_id,
                deadline=deadline,
                plan=plan,
                task_focus=manager.task_focus,
                auditors=auditors,
            )
        )
    return drafts


def render_manual_matching_bitrix_task_description(
    *,
    manager_name: str,
    plan: int,
    report_date: date,
    queue_total: int,
    queue_display: int,
    report_path: str,
    task_focus: str = "general",
    matching_url: str | None = None,
) -> str:
    queue_other = max(queue_total - queue_display, 0)
    normalized_focus = (task_focus or "general").strip().lower()
    if normalized_focus == "display":
        plan_line = (
            f"{manager_name}, план на {report_date.isoformat()}: {plan} ручных решений по дисплеям."
        )
        work_steps = [
            "1. Открыть интерфейс ручного сопоставления.",
            "2. Поставить фильтр на дисплеи / item_type=display.",
            "3. Разобрать кандидаты по дисплеям; обычные товары не брать.",
            "4. По каждому кандидату принять решение: Принять / Отклонить / Снять.",
            "5. Если дисплеи закончились, оставить фактический результат; обычную очередь заберут закупщики.",
        ]
        queue_lines = [
            f"- всего: {queue_total}",
            f"- дисплеи для Омара: {queue_display}",
            f"- остальная очередь: {queue_other}",
        ]
    else:
        plan_line = (
            f"{manager_name}, план на {report_date.isoformat()}: "
            f"{plan} ручных решений по обычной очереди без дисплеев."
        )
        work_steps = [
            "1. Открыть интерфейс ручного сопоставления.",
            "2. Разбирать товары без типа display: батареи, камеры, шлейфы, корпуса, разъемы и прочее.",
            "3. Дисплеи не брать: их разбирает Омар.",
            "4. По каждому кандидату принять решение: Принять / Отклонить / Снять.",
            "5. Если уверенности нет, лучше отклонить или оставить комментарий к разбору.",
        ]
        queue_lines = [
            f"- всего: {queue_total}",
            f"- дисплеи: {queue_display} (Омар)",
            f"- остальная очередь: {queue_other}",
        ]

    lines = [
        "Ручное сопоставление товаров конкурентов",
        "",
        plan_line,
        "",
        "Что сделать:",
        *work_steps,
        "",
        "Как считается выполнение:",
        "- факт считается по действиям в интерфейсе: accept / reject / revoke;",
        "- закрытие этой задачи само по себе не засчитывает план;",
        "- вечером система сверит план/факт и отдельно покажет подозрительные принятия.",
        "",
        "Текущая очередь на момент постановки:",
        *queue_lines,
        "",
        "Служебно:",
        f"- план: {plan}",
        f"- фокус: {normalized_focus}",
        f"- дата контроля: {report_date.isoformat()}",
        f"- контрольный отчет: {report_path}",
    ]
    if matching_url:
        lines.insert(4, f"Интерфейс: {matching_url}")
        lines.insert(5, "")
    return "\n".join(lines)


def _deadline_iso(report_date: date) -> str:
    deadline = datetime.combine(
        report_date,
        time(hour=DEFAULT_DEADLINE_HOUR),
        tzinfo=MOSCOW_TZ,
    )
    return deadline.isoformat()
