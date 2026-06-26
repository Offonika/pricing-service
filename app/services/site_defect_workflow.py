from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha1
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.services.expertise_bitrix import BitrixRestClient
from app.services.site_defect_archive import (
    PROBLEM_TYPE_DELIVERY,
    PROBLEM_TYPE_EXPERTISE,
    PROBLEM_TYPE_LABELS,
    PROBLEM_TYPE_MONEY_REFUND,
    PROBLEM_TYPE_OTHER,
    SiteDefectArchiveFilters,
    classify_site_defect_problem_type,
    extract_site_defect_numbers,
    search_archive_cases,
)

PRIORITY_NORMAL = "обычный"
PRIORITY_URGENT = "срочно"
PRIORITY_CONFLICT_RISK = "риск конфликта"

TASK_ROLE_RESPONSIBLE = "responsible"
TASK_ROLE_OKK = "okk"
TASK_ROLE_LOGISTICS = "logistics"
TASK_ROLE_FINANCE = "finance"
TASK_ROLE_LEADER = "leader"

HIGH_RISK_MARKERS = (
    "верните деньги",
    "вернуть деньги",
    "возврат денег",
    "суд",
    "жалоб",
    "роспотреб",
    "обман",
)
EXPERTISE_MARKERS = ("экспертиз", "не включается", "не работает", "брак", "бракованный")
LOGISTICS_MARKERS = (
    "доставка дороже",
    "возврат в пути",
    "не забираем",
    "не забирать",
    "курьер",
    "сдэк",
    "доставка",
)
MONEY_MARKERS = ("деньг", "верните", "вернуть", "возврат денег", "компенсац")
TOKEN_RE = re.compile(r"[\wА-Яа-яЁё-]{4,}", re.IGNORECASE)

RETURN_ECONOMICS_TAKE_BACK = "take_back"
RETURN_ECONOMICS_LEAVE = "leave"
RETURN_ECONOMICS_NEEDS_MANAGER = "needs_manager"
RETURN_ECONOMICS_MISSING_DATA = "missing_data"

RETURN_DECISION_NEEDS_EVALUATION = "needs_evaluation"
RETURN_DECISION_RETURN_GOODS = "return_goods"
RETURN_DECISION_LEAVE_WITH_CLIENT = "leave_with_client"
RETURN_DECISION_EXPERTISE_FIRST = "expertise_first"

RETURN_STATUS_NEEDS_CREATE = "needs_create"
RETURN_STATUS_CREATED = "created"
RETURN_STATUS_SENT_TO_CLIENT = "sent_to_client"
RETURN_STATUS_IN_TRANSIT = "in_transit"
RETURN_STATUS_RECEIVED = "received"
RETURN_STATUS_CANCELLED = "cancelled"

RETURN_ECONOMICS_LABELS = {
    RETURN_ECONOMICS_TAKE_BACK: "Возврат экономически оправдан",
    RETURN_ECONOMICS_LEAVE: "Не забирать",
    RETURN_ECONOMICS_NEEDS_MANAGER: "Нужна оценка старшего",
    RETURN_ECONOMICS_MISSING_DATA: "Нужны стоимость и доставка",
}

RETURN_DECISION_LABELS = {
    RETURN_DECISION_NEEDS_EVALUATION: "Нужна оценка",
    RETURN_DECISION_RETURN_GOODS: "Забрать товар",
    RETURN_DECISION_LEAVE_WITH_CLIENT: "Оставить у клиента",
    RETURN_DECISION_EXPERTISE_FIRST: "Сначала экспертиза",
}

RETURN_LEAVE_REASON_LABELS = {
    "return_cost_too_high": "Доставка дороже товара",
    "low_item_value": "Низкая полезная стоимость",
    "client_keep_after_refund": "Клиент оставляет товар после решения",
    "photo_video_enough": "Фото/видео достаточно",
    "other": "Другое",
}

RETURN_CARRIER_LABELS = {
    "cdek": "СДЭК",
    "post": "Почта",
    "courier": "Курьер",
    "other": "Другое",
}

RETURN_STATUS_LABELS = {
    RETURN_STATUS_NEEDS_CREATE: "Нужно создать",
    RETURN_STATUS_CREATED: "Создан",
    RETURN_STATUS_SENT_TO_CLIENT: "Передан клиенту",
    RETURN_STATUS_IN_TRANSIT: "В пути",
    RETURN_STATUS_RECEIVED: "Получен",
    RETURN_STATUS_CANCELLED: "Отменен",
}

RETURN_ECONOMICS_MANAGER_RATIO = 0.7
RETURN_IN_TRANSIT_OVERDUE_DAYS = 7


@dataclass(frozen=True)
class SiteDefectWorkingBitrixConfig:
    webhook_url: str | None
    entity_type_id: int | None
    working_category_id: int | None
    working_stage_map: dict[str, str]
    field_map: dict[str, str]
    enum_map: dict[str, dict[str, str]]
    created_by_user_id: int | None
    okk_user_ids: list[int]
    finance_user_ids: list[int]
    logistics_user_ids: list[int]
    leader_user_ids: list[int]

    @classmethod
    def from_settings(cls, settings: Settings) -> SiteDefectWorkingBitrixConfig:
        mapping = _load_site_defect_mapping()
        return cls(
            webhook_url=(
                settings.site_defect_archive_bitrix_webhook_url
                or settings.expertise_bitrix_webhook_url
            ),
            entity_type_id=(
                settings.site_defect_archive_bitrix_entity_type_id
                or _mapping_entity_type_id(mapping)
            ),
            working_category_id=(
                settings.site_defect_archive_bitrix_working_category_id
                or _mapping_working_category_id(mapping)
            ),
            working_stage_map=dict(
                settings.site_defect_archive_bitrix_working_stage_map
                or _mapping_working_stage_map(mapping)
            ),
            field_map=dict(
                settings.site_defect_archive_bitrix_field_map or _mapping_field_map(mapping)
            ),
            enum_map=_mapping_enum_map(mapping),
            created_by_user_id=settings.site_defect_workflow_created_by_user_id,
            okk_user_ids=list(settings.site_defect_workflow_okk_user_ids or []),
            finance_user_ids=list(settings.site_defect_workflow_finance_user_ids or []),
            logistics_user_ids=list(settings.site_defect_workflow_logistics_user_ids or []),
            leader_user_ids=list(settings.site_defect_workflow_leader_user_ids or []),
        )

    @property
    def can_read_bitrix(self) -> bool:
        return bool(self.webhook_url and self.entity_type_id)


def analyze_working_reclamation_text(
    session: Session,
    *,
    title: str | None = None,
    customer_contact: str | None = None,
    order_refs: str | None = None,
    product_model: str | None = None,
    problem_description: str | None = None,
    customer_request: str | None = None,
    comments_text: str | None = None,
    item_value: Any = None,
    estimated_return_cost: Any = None,
    return_goods_decision: str | None = None,
    return_leave_reason: str | None = None,
    return_decision_approved_by: str | None = None,
    return_tracking_number: str | None = None,
    return_status: str | None = None,
    similar_limit: int = 5,
) -> dict[str, Any]:
    text = _join_text(
        title,
        customer_contact,
        order_refs,
        product_model,
        problem_description,
        customer_request,
        comments_text,
    )
    numbers = extract_site_defect_numbers(text)
    problem_type = classify_site_defect_problem_type(text)
    priority = _detect_priority(text)
    recommended_stage = _recommend_stage(text, problem_type)
    return_economics = evaluate_return_economics(
        item_value=item_value,
        estimated_return_cost=estimated_return_cost,
    )
    normalized_return_decision = _normalize_enum_key(
        return_goods_decision,
        RETURN_DECISION_LABELS,
    )
    normalized_leave_reason = _normalize_enum_key(return_leave_reason, RETURN_LEAVE_REASON_LABELS)
    normalized_return_status = _normalize_enum_key(return_status, RETURN_STATUS_LABELS)
    tracking_number = str(return_tracking_number or "").strip()
    return_status_update = _recommended_return_status_update(
        decision=normalized_return_decision,
        tracking_number=tracking_number,
        current_status=normalized_return_status,
    )
    tasks = _recommend_tasks(
        text,
        problem_type,
        priority,
        recommended_stage,
        return_economics=return_economics,
        return_goods_decision=normalized_return_decision,
        return_leave_reason=normalized_leave_reason,
        return_decision_approved_by=return_decision_approved_by,
        return_tracking_number=tracking_number,
    )
    similar = find_similar_archive_cases(
        session,
        text=text,
        numbers=numbers,
        product_model=product_model,
        problem_type=problem_type,
        limit=similar_limit,
    )
    result = {
        "numbers": numbers,
        "problem_type": problem_type,
        "problem_type_label": PROBLEM_TYPE_LABELS.get(problem_type, problem_type),
        "priority": priority,
        "recommended_stage": recommended_stage,
        "recommended_stage_label": _stage_label(recommended_stage),
        "return_economics": return_economics,
        "return_goods_decision": normalized_return_decision,
        "return_goods_decision_label": RETURN_DECISION_LABELS.get(
            normalized_return_decision or "",
            normalized_return_decision or "",
        ),
        "return_leave_reason": normalized_leave_reason,
        "return_decision_approved_by": str(return_decision_approved_by or "").strip(),
        "return_tracking_number": tracking_number,
        "return_status": normalized_return_status,
        "return_status_update": return_status_update,
        "recommended_tasks": tasks,
        "similar_archive_cases": similar,
    }
    analysis_key = _analysis_key(result)
    result["analysis_key"] = analysis_key
    result["comment"] = build_analysis_comment(
        numbers=numbers,
        problem_type=problem_type,
        priority=priority,
        recommended_stage=recommended_stage,
        return_economics=return_economics,
        return_goods_decision=result["return_goods_decision_label"],
        return_tracking_number=tracking_number,
        return_status_update=return_status_update,
        tasks=tasks,
        similar=similar,
        analysis_key=analysis_key,
    )
    return result


@lru_cache(maxsize=1)
def _load_site_defect_mapping() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "build/bitrix/site_defect_archive_mapping.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _mapping_entity_type_id(mapping: dict[str, Any]) -> int | None:
    try:
        return int((mapping.get("process") or {}).get("entity_type_id"))
    except (TypeError, ValueError):
        return None


def _mapping_working_category_id(mapping: dict[str, Any]) -> int | None:
    try:
        return int(((mapping.get("categories") or {}).get("working") or {}).get("id"))
    except (TypeError, ValueError):
        return None


def _mapping_working_stage_map(mapping: dict[str, Any]) -> dict[str, str]:
    raw = (mapping.get("stage_map") or {}).get("working") or {}
    return (
        {str(key): str(value) for key, value in raw.items() if value}
        if isinstance(raw, dict)
        else {}
    )


def _mapping_field_map(mapping: dict[str, Any]) -> dict[str, str]:
    raw = mapping.get("field_map") or {}
    return (
        {str(key): str(value) for key, value in raw.items() if value}
        if isinstance(raw, dict)
        else {}
    )


def _mapping_enum_map(mapping: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = mapping.get("enum_map") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(field_key): {str(key): str(value) for key, value in values.items() if value}
        for field_key, values in raw.items()
        if isinstance(values, dict)
    }


def find_similar_archive_cases(
    session: Session,
    *,
    text: str,
    numbers: list[str],
    product_model: str | None,
    problem_type: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add_items(query: str | None, *, number: str | None = None) -> None:
        if len(result) >= limit:
            return
        filters = SiteDefectArchiveFilters(
            query=query,
            number=number,
            problem_type=(
                None if number else problem_type if problem_type != PROBLEM_TYPE_OTHER else None
            ),
            limit=limit,
        )
        items, _total = search_archive_cases(session, filters)
        for item in items:
            item_id = int(item["id"])
            if item_id in seen:
                continue
            seen.add(item_id)
            result.append(
                {
                    "id": item_id,
                    "title": item.get("title"),
                    "summary": item.get("summary"),
                    "numbers": item.get("extracted_numbers") or [],
                    "problem_type": item.get("problem_type"),
                    "bitrix_detail_url": item.get("bitrix_detail_url"),
                    "bitrix_disk_folder_url": item.get("bitrix_disk_folder_url"),
                }
            )
            if len(result) >= limit:
                break

    for number in numbers[:5]:
        add_items(number, number=number)
    if product_model:
        add_items(product_model[:120])
    for query in _search_queries_from_text(text):
        add_items(query)
        if len(result) >= limit:
            break
    return result[:limit]


def evaluate_return_economics(*, item_value: Any, estimated_return_cost: Any) -> dict[str, Any]:
    parsed_item_value = _float_or_none(item_value)
    parsed_return_cost = _float_or_none(estimated_return_cost)
    if parsed_item_value is None or parsed_item_value <= 0 or parsed_return_cost is None:
        return {
            "item_value": parsed_item_value,
            "estimated_return_cost": parsed_return_cost,
            "ratio": None,
            "result": RETURN_ECONOMICS_MISSING_DATA,
            "label": RETURN_ECONOMICS_LABELS[RETURN_ECONOMICS_MISSING_DATA],
            "detail": "Заполните полезную стоимость товара и оценку обратной доставки.",
        }
    ratio = parsed_return_cost / parsed_item_value
    if ratio >= 1:
        result = RETURN_ECONOMICS_LEAVE
        detail = "Обратная доставка дороже или равна полезной стоимости товара."
    elif ratio >= RETURN_ECONOMICS_MANAGER_RATIO:
        result = RETURN_ECONOMICS_NEEDS_MANAGER
        detail = "Обратная доставка близка к стоимости товара, нужна оценка старшего."
    else:
        result = RETURN_ECONOMICS_TAKE_BACK
        detail = "Обратная доставка заметно дешевле полезной стоимости товара."
    return {
        "item_value": parsed_item_value,
        "estimated_return_cost": parsed_return_cost,
        "ratio": ratio,
        "result": result,
        "label": RETURN_ECONOMICS_LABELS[result],
        "detail": detail,
    }


def build_working_reclamations_report(
    session: Session,
    *,
    settings: Settings | None = None,
    client: BitrixRestClient | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = SiteDefectWorkingBitrixConfig.from_settings(settings or get_settings())
    if not config.can_read_bitrix:
        return {
            "status": "blocked",
            "reason": "Bitrix webhook/entity type is not configured",
            "items_checked": 0,
            "buckets": _empty_report_buckets(),
        }
    bitrix = client or BitrixRestClient(config.webhook_url or "")
    items = _list_working_bitrix_items(bitrix, config=config, limit=limit)
    return build_working_reclamations_report_from_items(
        session,
        items=items,
        config=config,
        now=now,
    )


def build_working_reclamations_report_from_items(
    session: Session,
    *,
    items: list[dict[str, Any]],
    config: SiteDefectWorkingBitrixConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    buckets = _empty_report_buckets()
    for item in items:
        item_summary = _working_item_summary(item, config=config)
        assigned_by_id = _item_value(item, "assignedById")
        if not _int_or_none(assigned_by_id):
            buckets["without_responsible"].append(item_summary)

        deadline = _parse_datetime(_field_value(item, config, "reaction_deadline"))
        if deadline and deadline < _ensure_aware(current_time) and not _is_closed(item, config):
            buckets["overdue"].append(item_summary)

        stage_id = str(_item_value(item, "stageId") or "")
        linked_expertise = _linked_expertise_value(item, config)
        if stage_id == config.working_stage_map.get("need_expertise") and not linked_expertise:
            buckets["need_expertise_without_link"].append(item_summary)

        decision = _field_value(item, config, "decision_result")
        if stage_id == config.working_stage_map.get("refund_or_decision") and not decision:
            buckets["refund_without_decision"].append(item_summary)

        analysis = analyze_working_bitrix_item(session, item=item, config=config)
        if analysis["priority"] == PRIORITY_CONFLICT_RISK:
            buckets["risk_conflict"].append({**item_summary, "analysis": analysis})
        return_economics = analysis.get("return_economics") or {}
        return_decision = analysis.get("return_goods_decision")
        return_status = analysis.get("return_status")
        return_tracking_number = analysis.get("return_tracking_number")
        return_leave_reason = analysis.get("return_leave_reason")
        return_approved_by = analysis.get("return_decision_approved_by")
        if return_economics.get("result") == RETURN_ECONOMICS_MISSING_DATA:
            buckets["needs_return_economics"].append({**item_summary, "analysis": analysis})
        if (
            return_economics.get("result") == RETURN_ECONOMICS_LEAVE
            or return_decision == RETURN_DECISION_LEAVE_WITH_CLIENT
        ) and (not return_leave_reason or not return_approved_by):
            buckets["leave_with_client_needs_approval"].append(
                {**item_summary, "analysis": analysis}
            )
        if return_decision == RETURN_DECISION_RETURN_GOODS and not return_tracking_number:
            buckets["return_track_required"].append({**item_summary, "analysis": analysis})
        if return_tracking_number and return_status in {
            RETURN_STATUS_CREATED,
            RETURN_STATUS_SENT_TO_CLIENT,
            RETURN_STATUS_IN_TRANSIT,
        }:
            buckets["return_track_created_waiting_goods"].append(
                {**item_summary, "analysis": analysis}
            )
        tracking_created_at = _parse_datetime(
            _field_value(item, config, "return_tracking_created_at")
        )
        if (
            return_status == RETURN_STATUS_IN_TRANSIT
            and tracking_created_at
            and tracking_created_at
            < _ensure_aware(current_time) - timedelta(days=RETURN_IN_TRANSIT_OVERDUE_DAYS)
        ):
            buckets["return_in_transit_overdue"].append({**item_summary, "analysis": analysis})
    return {
        "status": "ready",
        "items_checked": len(items),
        "buckets": buckets,
    }


def analyze_working_bitrix_item(
    session: Session,
    *,
    item: dict[str, Any],
    config: SiteDefectWorkingBitrixConfig,
) -> dict[str, Any]:
    return analyze_working_reclamation_text(
        session,
        title=str(_item_value(item, "title") or ""),
        customer_contact=str(_field_value(item, config, "customer_contact") or ""),
        order_refs=str(_field_value(item, config, "order_refs") or ""),
        product_model=str(_field_value(item, config, "product_model") or ""),
        problem_description=str(_field_value(item, config, "problem_description") or ""),
        customer_request=_join_text(
            _enum_choice_label(item, config=config, key="customer_request_choice"),
            _enum_choice_label(item, config=config, key="problem_type_choice"),
            _enum_choice_label(item, config=config, key="priority_choice"),
            _field_value(item, config, "customer_request"),
        ),
        comments_text=str(_field_value(item, config, "analysis_hints") or ""),
        item_value=_field_value(item, config, "item_value"),
        estimated_return_cost=_field_value(item, config, "estimated_return_cost"),
        return_goods_decision=_enum_choice_key(item, config=config, key="return_goods_decision"),
        return_leave_reason=_enum_choice_key(item, config=config, key="return_leave_reason"),
        return_decision_approved_by=str(
            _field_value(item, config, "return_decision_approved_by") or ""
        ),
        return_tracking_number=str(_field_value(item, config, "return_tracking_number") or ""),
        return_status=_enum_choice_key(item, config=config, key="return_status"),
    )


def analyze_bitrix_working_reclamations(
    session: Session,
    *,
    settings: Settings | None = None,
    client: BitrixRestClient | None = None,
    item_id: str | None = None,
    limit: int = 5,
    apply: bool = False,
) -> dict[str, Any]:
    config = SiteDefectWorkingBitrixConfig.from_settings(settings or get_settings())
    if not config.can_read_bitrix:
        return {
            "status": "blocked",
            "reason": "Bitrix webhook/entity type is not configured",
            "items": [],
        }
    bitrix = client or BitrixRestClient(config.webhook_url or "")
    if item_id:
        items = [
            bitrix.get_smart_process_item(
                entity_type_id=config.entity_type_id or 0, item_id=item_id
            )
        ]
    else:
        items = _list_working_bitrix_items(bitrix, config=config, limit=limit)
    results = []
    for item in items:
        if not item:
            continue
        if not _is_working_item(item, config=config):
            results.append(
                {
                    "item": _working_item_summary(item, config=config),
                    "skipped": "not_working_category",
                }
            )
            continue
        analysis = analyze_working_bitrix_item(session, item=item, config=config)
        applied = (
            _apply_analysis(bitrix, item=item, config=config, analysis=analysis) if apply else {}
        )
        results.append(
            {
                "item": _working_item_summary(item, config=config),
                "analysis": analysis,
                "applied": applied,
            }
        )
    return {
        "status": "ready",
        "apply": apply,
        "items": results,
    }


def build_analysis_comment(
    *,
    numbers: list[str],
    problem_type: str,
    priority: str,
    recommended_stage: str,
    return_economics: dict[str, Any],
    return_goods_decision: str,
    return_tracking_number: str,
    return_status_update: str | None,
    tasks: list[dict[str, Any]],
    similar: list[dict[str, Any]],
    analysis_key: str,
) -> str:
    lines = [
        "Подсказка по рекламации:",
        f"- Тип проблемы: {PROBLEM_TYPE_LABELS.get(problem_type, problem_type)}",
        f"- Приоритет: {priority}",
        f"- Рекомендуемая стадия: {_stage_label(recommended_stage)}",
        f"- Экономика возврата: {return_economics.get('label')}",
    ]
    if return_economics.get("detail"):
        lines.append(f"  {return_economics['detail']}")
    if return_goods_decision:
        lines.append(f"- Решение по возврату: {return_goods_decision}")
    if return_tracking_number:
        lines.append(f"- Трек-номер возврата: {return_tracking_number}")
    if return_status_update:
        lines.append(
            f"- Рекомендуемый статус возврата: {RETURN_STATUS_LABELS[return_status_update]}"
        )
    if numbers:
        lines.append(f"- Найденные номера: {', '.join(numbers[:10])}")
    if tasks:
        lines.append("- Рекомендуемые задачи:")
        lines.extend(f"  - {task['title']}" for task in tasks[:6])
    if similar:
        lines.append("- Похожие архивные случаи:")
        for item in similar[:5]:
            link = item.get("bitrix_detail_url") or item.get("bitrix_disk_folder_url") or ""
            suffix = f" — {link}" if link else ""
            lines.append(f"  - {item.get('title') or item.get('id')}{suffix}")
    else:
        lines.append("- Похожие архивные случаи: не найдены")
    lines.append(f"- Ключ анализа: {analysis_key}")
    return "\n".join(lines)


def _apply_analysis(
    client: BitrixRestClient,
    *,
    item: dict[str, Any],
    config: SiteDefectWorkingBitrixConfig,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    item_id = str(_item_value(item, "id") or "")
    if not item_id or not config.entity_type_id:
        return {"status": "skipped", "reason": "missing item id/entity type"}
    existing_hints = str(_field_value(item, config, "analysis_hints") or "")
    if analysis["analysis_key"] in existing_hints:
        return {"status": "skipped", "reason": "analysis_already_applied"}
    update_fields = _analysis_update_fields(item, config=config, analysis=analysis)
    if update_fields:
        client.update_smart_process_item(
            entity_type_id=config.entity_type_id,
            item_id=item_id,
            fields=update_fields,
        )
    comment_id = client.add_crm_timeline_comment(
        entity_type_id=config.entity_type_id,
        item_id=item_id,
        comment=analysis["comment"],
    )
    task_results = _create_recommended_tasks(client, item=item, config=config, analysis=analysis)
    return {
        "status": "applied",
        "updated_fields": sorted(update_fields),
        "comment_id": comment_id,
        "tasks": task_results,
    }


def _analysis_key(analysis: dict[str, Any]) -> str:
    payload = {
        "numbers": analysis.get("numbers") or [],
        "problem_type": analysis.get("problem_type"),
        "priority": analysis.get("priority"),
        "recommended_stage": analysis.get("recommended_stage"),
        "return_economics": (analysis.get("return_economics") or {}).get("result"),
        "return_goods_decision": analysis.get("return_goods_decision"),
        "return_tracking_number": analysis.get("return_tracking_number"),
        "return_status": analysis.get("return_status"),
        "return_status_update": analysis.get("return_status_update"),
        "similar": [item.get("id") for item in analysis.get("similar_archive_cases") or []],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"site-defect-analysis:{sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _analysis_update_fields(
    item: dict[str, Any],
    *,
    config: SiteDefectWorkingBitrixConfig,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    _put_if_field_exists(result, config, "numbers", ", ".join(analysis["numbers"]))
    _put_if_field_exists(result, config, "problem_type", analysis["problem_type_label"])
    _put_if_field_exists(result, config, "priority", analysis["priority"])
    _put_if_field_exists(result, config, "analysis_hints", analysis["comment"])
    _put_enum_if_field_exists(result, config, "problem_type_choice", analysis["problem_type"])
    _put_enum_if_field_exists(
        result, config, "priority_choice", _priority_enum_key(analysis["priority"])
    )
    _put_enum_if_field_exists(
        result,
        config,
        "return_economics_result",
        (analysis.get("return_economics") or {}).get("result"),
    )
    return_status_update = analysis.get("return_status_update")
    if return_status_update:
        _put_enum_if_field_exists(result, config, "return_status", return_status_update)
        if return_status_update == RETURN_STATUS_CREATED and not _field_value(
            item, config, "return_tracking_created_at"
        ):
            _put_if_field_exists(
                result,
                config,
                "return_tracking_created_at",
                datetime.now(timezone.utc),
            )
    if not _field_value(item, config, "order_refs") and analysis["numbers"]:
        _put_if_field_exists(result, config, "order_refs", ", ".join(analysis["numbers"]))
    return result


def _create_recommended_tasks(
    client: BitrixRestClient,
    *,
    item: dict[str, Any],
    config: SiteDefectWorkingBitrixConfig,
    analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    item_title = str(_item_value(item, "title") or f"Рекламация #{_item_value(item, 'id')}")
    description = _task_description(item=item, analysis=analysis)
    for task in analysis["recommended_tasks"]:
        responsible_id = _resolve_task_responsible(task.get("role"), item=item, config=config)
        if responsible_id is None:
            result.append({"title": task["title"], "status": "skipped", "reason": "no_user_id"})
            continue
        deadline = datetime.now(timezone.utc) + timedelta(hours=int(task.get("due_hours") or 24))
        task_id = client.add_task(
            title=f"{task['title']}: {item_title}"[:255],
            description=description,
            created_by_id=config.created_by_user_id,
            responsible_id=responsible_id,
            deadline=deadline,
            accomplice_ids=[],
            auditor_ids=config.leader_user_ids,
        )
        result.append({"title": task["title"], "status": "created", "task_id": task_id})
    return result


def _list_working_bitrix_items(
    client: BitrixRestClient,
    *,
    config: SiteDefectWorkingBitrixConfig,
    limit: int,
) -> list[dict[str, Any]]:
    filters: dict[str, Any] = {}
    if config.working_category_id is not None:
        filters["categoryId"] = config.working_category_id
    return client.list_smart_process_items(
        entity_type_id=config.entity_type_id or 0,
        filters=filters,
        select=_bitrix_select_fields(config),
        order={"id": "DESC"},
        limit=limit,
    )


def _bitrix_select_fields(config: SiteDefectWorkingBitrixConfig) -> list[str]:
    fields = ["id", "title", "stageId", "categoryId", "assignedById", "createdTime", "updatedTime"]
    fields.extend(
        value
        for key, value in config.field_map.items()
        if key
        in {
            "customer_contact",
            "order_refs",
            "product_model",
            "problem_description",
            "customer_request",
            "customer_request_choice",
            "problem_type",
            "problem_type_choice",
            "priority",
            "priority_choice",
            "next_action",
            "reaction_deadline",
            "linked_expertise",
            "linked_expertise_crm",
            "decision_result",
            "working_files_url",
            "analysis_hints",
            "numbers",
            "item_value",
            "estimated_return_cost",
            "return_economics_result",
            "return_goods_decision",
            "return_leave_reason",
            "return_decision_approved_by",
            "return_carrier",
            "return_tracking_number",
            "return_tracking_created_at",
            "return_status",
        }
    )
    return fields


def _recommend_tasks(
    text: str,
    problem_type: str,
    priority: str,
    recommended_stage: str,
    *,
    return_economics: dict[str, Any],
    return_goods_decision: str | None,
    return_leave_reason: str | None,
    return_decision_approved_by: str | None,
    return_tracking_number: str | None,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = [
        {
            "role": TASK_ROLE_RESPONSIBLE,
            "title": "Разобрать рекламацию",
            "due_hours": 4,
            "reason": "Новая рабочая рекламация",
        }
    ]
    normalized = text.casefold()
    if priority == PRIORITY_CONFLICT_RISK:
        tasks.append(
            {
                "role": TASK_ROLE_OKK,
                "title": "Проверить риск конфликта с клиентом",
                "due_hours": 8,
                "reason": "В тексте есть рискованные формулировки",
            }
        )
    if recommended_stage == "need_expertise" or problem_type == PROBLEM_TYPE_EXPERTISE:
        tasks.append(
            {
                "role": TASK_ROLE_OKK,
                "title": "Создать или связать экспертизу",
                "due_hours": 24,
                "reason": "Похоже, нужна экспертиза",
            }
        )
    if problem_type == PROBLEM_TYPE_MONEY_REFUND or any(
        marker in normalized for marker in MONEY_MARKERS
    ):
        tasks.append(
            {
                "role": TASK_ROLE_FINANCE,
                "title": "Согласовать решение по деньгам/замене",
                "due_hours": 24,
                "reason": "Клиент просит деньги или решение по возврату",
            }
        )
    if problem_type == PROBLEM_TYPE_DELIVERY or any(
        marker in normalized for marker in LOGISTICS_MARKERS
    ):
        tasks.append(
            {
                "role": TASK_ROLE_LOGISTICS,
                "title": "Проверить статус возврата/доставки",
                "due_hours": 24,
                "reason": "Есть логистический риск",
            }
        )
    economics_result = str(return_economics.get("result") or "")
    if economics_result == RETURN_ECONOMICS_MISSING_DATA:
        tasks.append(
            {
                "role": TASK_ROLE_RESPONSIBLE,
                "title": "Заполнить стоимость товара и обратной доставки",
                "due_hours": 4,
                "reason": "Для решения по возврату не хватает экономики",
            }
        )
    if economics_result == RETURN_ECONOMICS_NEEDS_MANAGER:
        tasks.append(
            {
                "role": TASK_ROLE_OKK,
                "title": "Оценить экономику возврата",
                "due_hours": 8,
                "reason": "Обратная доставка близка к стоимости товара",
            }
        )
    if (
        economics_result == RETURN_ECONOMICS_LEAVE
        or return_goods_decision == RETURN_DECISION_LEAVE_WITH_CLIENT
    ) and (
        not return_leave_reason
        or not str(return_decision_approved_by or "").strip()
        or return_goods_decision != RETURN_DECISION_LEAVE_WITH_CLIENT
    ):
        tasks.append(
            {
                "role": TASK_ROLE_OKK,
                "title": "Согласовать решение оставить товар у клиента",
                "due_hours": 8,
                "reason": "Возврат экономически невыгоден или выбран вариант не забирать",
            }
        )
    if (
        return_goods_decision == RETURN_DECISION_RETURN_GOODS
        and not str(return_tracking_number or "").strip()
    ):
        tasks.append(
            {
                "role": TASK_ROLE_LOGISTICS,
                "title": "Создать трек-номер возврата",
                "due_hours": 4,
                "reason": "Принято решение забрать товар у клиента",
            }
        )
    return tasks


def _detect_priority(text: str) -> str:
    normalized = text.casefold()
    if any(marker in normalized for marker in HIGH_RISK_MARKERS):
        return PRIORITY_CONFLICT_RISK
    if "срочно" in normalized:
        return PRIORITY_URGENT
    return PRIORITY_NORMAL


def _recommend_stage(text: str, problem_type: str) -> str:
    normalized = text.casefold()
    if problem_type == PROBLEM_TYPE_EXPERTISE or any(
        marker in normalized for marker in EXPERTISE_MARKERS
    ):
        return "need_expertise"
    if problem_type == PROBLEM_TYPE_MONEY_REFUND:
        return "refund_or_decision"
    if problem_type == PROBLEM_TYPE_DELIVERY:
        return "waiting_client_or_goods"
    return "clarify"


def _stage_label(stage: str) -> str:
    return {
        "new": "Новая",
        "clarify": "Разобраться",
        "waiting_client_or_goods": "Ожидаем клиента / товар",
        "need_expertise": "Нужна экспертиза",
        "linked_expertise": "Связано с экспертизой",
        "refund_or_decision": "Возврат денег / решение",
        "closed": "Закрыто",
    }.get(stage, stage)


def _search_queries_from_text(text: str) -> list[str]:
    tokens = [
        token
        for token in TOKEN_RE.findall(text)
        if token.casefold()
        not in {
            "клиент",
            "рекламация",
            "товар",
            "модель",
            "деньги",
            "возврат",
            "нужно",
            "надо",
        }
    ]
    queries: list[str] = []
    for token in tokens[:12]:
        if token not in queries:
            queries.append(token)
    return queries


def _join_text(*values: Any) -> str:
    return "\n".join(str(value).strip() for value in values if str(value or "").strip())


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        normalized = str(value).strip().replace(" ", "").replace(",", ".")
        if not normalized:
            return None
        try:
            parsed = float(normalized)
        except ValueError:
            return None
    return parsed if parsed >= 0 else None


def _normalize_enum_key(value: Any, labels: dict[str, str]) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized in labels:
        return normalized
    normalized_folded = normalized.casefold()
    for key, label in labels.items():
        if normalized_folded == label.casefold():
            return key
    return normalized


def _recommended_return_status_update(
    *,
    decision: str | None,
    tracking_number: str,
    current_status: str | None,
) -> str | None:
    if (
        decision == RETURN_DECISION_RETURN_GOODS
        and not tracking_number
        and current_status in (None, "", RETURN_STATUS_NEEDS_CREATE)
    ):
        return RETURN_STATUS_NEEDS_CREATE
    if tracking_number and current_status in (None, "", RETURN_STATUS_NEEDS_CREATE):
        return RETURN_STATUS_CREATED
    return None


def _rest_field_name(field_name: str) -> str:
    normalized = field_name.strip()
    if not normalized.upper().startswith("UF_"):
        return normalized
    parts = normalized.lower().split("_")
    head, *tail = parts
    return head + "".join(part.capitalize() for part in tail)


def _item_value(item: dict[str, Any], field_name: str) -> Any:
    if field_name in item:
        return item.get(field_name)
    rest_name = _rest_field_name(field_name)
    if rest_name in item:
        return item.get(rest_name)
    upper_name = field_name.upper()
    if upper_name in item:
        return item.get(upper_name)
    return None


def _field_value(item: dict[str, Any], config: SiteDefectWorkingBitrixConfig, key: str) -> Any:
    field_name = config.field_map.get(key)
    if not field_name:
        return None
    return _item_value(item, field_name)


def _linked_expertise_value(item: dict[str, Any], config: SiteDefectWorkingBitrixConfig) -> Any:
    return _field_value(item, config, "linked_expertise_crm") or _field_value(
        item,
        config,
        "linked_expertise",
    )


def _enum_labels_for_key(key: str) -> dict[str, str]:
    return {
        "customer_request_choice": {
            "clarify": "Разобраться",
            "refund_money": "Вернуть деньги",
            "replacement": "Замена товара",
            "expertise": "Нужна экспертиза",
            "logistics_return": "Доставка / возврат",
            "other": "Другое",
        },
        "problem_type_choice": {
            "model_mismatch": "Перепутали модель",
            "return": "Возврат",
            "money_refund": "Деньги",
            "delivery": "Доставка",
            "expertise": "Экспертиза",
            "other": "Прочее",
        },
        "priority_choice": {
            "normal": "Обычный",
            "urgent": "Срочно",
            "conflict_risk": "Риск конфликта",
        },
        "return_economics_result": RETURN_ECONOMICS_LABELS,
        "return_goods_decision": RETURN_DECISION_LABELS,
        "return_leave_reason": RETURN_LEAVE_REASON_LABELS,
        "return_carrier": RETURN_CARRIER_LABELS,
        "return_status": RETURN_STATUS_LABELS,
    }.get(key, {})


def _enum_choice_key(
    item: dict[str, Any],
    *,
    config: SiteDefectWorkingBitrixConfig,
    key: str,
) -> str | None:
    value = str(_field_value(item, config, key) or "").strip()
    if not value:
        return None
    inverse = {enum_id: enum_key for enum_key, enum_id in (config.enum_map.get(key) or {}).items()}
    enum_key = inverse.get(value)
    if enum_key:
        return enum_key
    return _normalize_enum_key(value, _enum_labels_for_key(key))


def _enum_choice_label(
    item: dict[str, Any],
    *,
    config: SiteDefectWorkingBitrixConfig,
    key: str,
) -> str:
    value = str(_field_value(item, config, key) or "").strip()
    if not value:
        return ""
    enum_key = _enum_choice_key(item, config=config, key=key)
    if enum_key:
        return _enum_labels_for_key(key).get(enum_key, enum_key)
    return value


def _put_if_field_exists(
    target: dict[str, Any],
    config: SiteDefectWorkingBitrixConfig,
    key: str,
    value: Any,
) -> None:
    field_name = config.field_map.get(key)
    if field_name and value not in (None, ""):
        target[field_name] = value


def _put_enum_if_field_exists(
    target: dict[str, Any],
    config: SiteDefectWorkingBitrixConfig,
    key: str,
    enum_key: str | None,
) -> None:
    if not enum_key:
        return
    field_name = config.field_map.get(key)
    enum_id = (config.enum_map.get(key) or {}).get(enum_key)
    if field_name and enum_id:
        target[field_name] = enum_id


def _priority_enum_key(priority: str) -> str:
    return {
        PRIORITY_NORMAL: "normal",
        PRIORITY_URGENT: "urgent",
        PRIORITY_CONFLICT_RISK: "conflict_risk",
    }.get(priority, "normal")


def _working_item_summary(
    item: dict[str, Any],
    *,
    config: SiteDefectWorkingBitrixConfig,
) -> dict[str, Any]:
    return {
        "id": _item_value(item, "id"),
        "title": _item_value(item, "title"),
        "stage_id": _item_value(item, "stageId"),
        "category_id": _item_value(item, "categoryId"),
        "assigned_by_id": _item_value(item, "assignedById"),
        "reaction_deadline": _field_value(item, config, "reaction_deadline"),
        "order_refs": _field_value(item, config, "order_refs"),
        "priority": _field_value(item, config, "priority"),
        "linked_expertise": _linked_expertise_value(item, config),
        "item_value": _field_value(item, config, "item_value"),
        "estimated_return_cost": _field_value(item, config, "estimated_return_cost"),
        "return_goods_decision": _enum_choice_label(
            item,
            config=config,
            key="return_goods_decision",
        ),
        "return_tracking_number": _field_value(item, config, "return_tracking_number"),
        "return_status": _enum_choice_label(item, config=config, key="return_status"),
    }


def _is_working_item(item: dict[str, Any], *, config: SiteDefectWorkingBitrixConfig) -> bool:
    if config.working_category_id is None:
        return True
    return _int_or_none(_item_value(item, "categoryId")) == config.working_category_id


def _is_closed(item: dict[str, Any], config: SiteDefectWorkingBitrixConfig) -> bool:
    return str(_item_value(item, "stageId") or "") in {
        config.working_stage_map.get("closed"),
    }


def _empty_report_buckets() -> dict[str, list[dict[str, Any]]]:
    return {
        "without_responsible": [],
        "overdue": [],
        "need_expertise_without_link": [],
        "refund_without_decision": [],
        "risk_conflict": [],
        "needs_return_economics": [],
        "leave_with_client_needs_approval": [],
        "return_track_required": [],
        "return_track_created_waiting_goods": [],
        "return_in_transit_overdue": [],
    }


def _resolve_task_responsible(
    role: str | None,
    *,
    item: dict[str, Any],
    config: SiteDefectWorkingBitrixConfig,
) -> int | None:
    if role == TASK_ROLE_RESPONSIBLE:
        return _int_or_none(_item_value(item, "assignedById"))
    if role == TASK_ROLE_OKK:
        return _first_int(config.okk_user_ids)
    if role == TASK_ROLE_LOGISTICS:
        return _first_int(config.logistics_user_ids)
    if role == TASK_ROLE_FINANCE:
        return _first_int(config.finance_user_ids)
    if role == TASK_ROLE_LEADER:
        return _first_int(config.leader_user_ids)
    return None


def _task_description(*, item: dict[str, Any], analysis: dict[str, Any]) -> str:
    lines = [
        f"Карточка CRM: #{_item_value(item, 'id')}",
        f"Тип проблемы: {analysis['problem_type_label']}",
        f"Приоритет: {analysis['priority']}",
        f"Экономика возврата: {(analysis.get('return_economics') or {}).get('label')}",
        "",
        analysis["comment"],
    ]
    return "\n".join(lines)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return _ensure_aware(datetime.fromisoformat(normalized))
        except ValueError:
            return None
    return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _first_int(values: list[int]) -> int | None:
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None
