from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import CompetitorItem, CompetitorItemMatch, Product, ProductCompetitorItemDecision
from app.models.competitor_item_match import CompetitorItemMatchStatus

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
REVIEW_STATUSES = (
    CompetitorItemMatchStatus.SUGGESTED,
    CompetitorItemMatchStatus.AMBIGUOUS,
    CompetitorItemMatchStatus.NEEDS_REVIEW,
)
DEFAULT_DAILY_PLAN = 10
TRAINING_THRESHOLD = 300


@dataclass(frozen=True)
class MatchingManager:
    user_id: str
    name: str
    daily_plan: int = DEFAULT_DAILY_PLAN
    task_focus: str = "general"

    @property
    def actor_suffix(self) -> str:
        return f":{self.user_id}"


DEFAULT_MANAGERS: tuple[MatchingManager, ...] = (
    MatchingManager(user_id="130757", name="Омар", task_focus="display"),
    MatchingManager(user_id="130756", name="Вараздат"),
    MatchingManager(user_id="130917", name="Вячеслав"),
    MatchingManager(user_id="130747", name="Роман"),
)


def report_date_today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def day_bounds(report_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(report_date, time.min, tzinfo=MOSCOW_TZ)
    return start, start + timedelta(days=1)


def build_manual_matching_control_report(
    db: Session,
    *,
    report_date: date | None = None,
    managers: tuple[MatchingManager, ...] = DEFAULT_MANAGERS,
) -> dict:
    target_date = report_date or report_date_today()
    start_at, end_at = day_bounds(target_date)
    manager_by_actor = {manager.actor_suffix: manager for manager in managers}
    manager_rows = {
        manager.user_id: {
            "user_id": manager.user_id,
            "name": manager.name,
            "plan": manager.daily_plan,
            "total": 0,
            "accept": 0,
            "reject": 0,
            "revoke": 0,
            "suspicious_accepts": 0,
            "completion_pct": 0.0,
        }
        for manager in managers
    }

    decisions = _load_decisions_for_day(db, start_at=start_at, end_at=end_at)
    day_accepts: list[ProductCompetitorItemDecision] = []
    unmatched_decisions = 0
    for decision in decisions:
        manager = _manager_for_actor(decision.created_by, manager_by_actor)
        if manager is None:
            unmatched_decisions += 1
            continue
        row = manager_rows[manager.user_id]
        action = (decision.action or "").strip().lower()
        row["total"] += 1
        if action in {"accept", "reject", "revoke"}:
            row[action] += 1
        if action == "accept":
            day_accepts.append(decision)

    suspicious_accepts = _build_suspicious_accepts(db, day_accepts, manager_by_actor)
    for item in suspicious_accepts:
        manager_id = item["manager_user_id"]
        if manager_id in manager_rows:
            manager_rows[manager_id]["suspicious_accepts"] += 1

    for row in manager_rows.values():
        row["completion_pct"] = round(row["total"] / row["plan"] * 100, 1) if row["plan"] else 0.0

    queue = _build_queue_summary(db)
    summary = {
        "total_done": sum(row["total"] for row in manager_rows.values()),
        "total_plan": sum(row["plan"] for row in manager_rows.values()),
        "total_accept": sum(row["accept"] for row in manager_rows.values()),
        "total_reject": sum(row["reject"] for row in manager_rows.values()),
        "total_revoke": sum(row["revoke"] for row in manager_rows.values()),
        "suspicious_accepts": len(suspicious_accepts),
        "queue_total": queue["total"],
        "queue_display": queue["display"],
        "unmatched_decisions": unmatched_decisions,
    }
    total_examples = _manual_examples_count(db)

    return {
        "date": target_date.isoformat(),
        "timezone": "Europe/Moscow",
        "daily_plan": DEFAULT_DAILY_PLAN,
        "managers": list(manager_rows.values()),
        "summary": summary,
        "queue": queue,
        "suspicious_accepts": suspicious_accepts,
        "learning_loop": {
            "positive_examples_today": summary["total_accept"],
            "negative_examples_today": summary["total_reject"],
            "manual_examples_total": total_examples,
            "training_threshold": TRAINING_THRESHOLD,
            "ready_for_rule_analysis": total_examples >= TRAINING_THRESHOLD,
        },
    }


def render_manual_matching_markdown(report: dict) -> str:
    lines = [f"Ручное сопоставление за {report['date']}", ""]
    for row in report["managers"]:
        lines.append(
            f"{row['name']}: {row['total']} / {row['plan']}, "
            f"принято {row['accept']}, отклонено {row['reject']}, "
            f"снято {row['revoke']}, подозрительных {row['suspicious_accepts']}"
        )
    summary = report["summary"]
    lines.extend(
        [
            "",
            f"Итого: {summary['total_done']} / {summary['total_plan']}",
            f"Остаток очереди: {summary['queue_total']}",
            f"Из них дисплеи: {summary['queue_display']}",
            "",
            f"Нужно проверить подозрительные принятия: {summary['suspicious_accepts']}",
        ]
    )
    if report["suspicious_accepts"]:
        lines.extend(["", "Подозрительные принятия:"])
        for item in report["suspicious_accepts"]:
            reasons = ", ".join(item["reasons"])
            lines.append(
                "- "
                f"{item['manager_name']}: {item['product_article']} - "
                f"{item['product_name']} / {item['competitor']} "
                f"{item['competitor_item_id']} - {item['competitor_name']} "
                f"({reasons})"
            )
    learning = report["learning_loop"]
    lines.extend(
        [
            "",
            "Обучающая выборка:",
            f"- положительные примеры за день: {learning['positive_examples_today']}",
            f"- отрицательные примеры за день: {learning['negative_examples_today']}",
            f"- ручных примеров всего: {learning['manual_examples_total']} / "
            f"{learning['training_threshold']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_decisions_for_day(
    db: Session, *, start_at: datetime, end_at: datetime
) -> list[ProductCompetitorItemDecision]:
    return list(
        db.execute(
            select(ProductCompetitorItemDecision)
            .options(
                joinedload(ProductCompetitorItemDecision.product),
                joinedload(ProductCompetitorItemDecision.competitor_item),
            )
            .where(
                ProductCompetitorItemDecision.created_at >= start_at,
                ProductCompetitorItemDecision.created_at < end_at,
            )
            .order_by(ProductCompetitorItemDecision.created_at.asc())
        )
        .scalars()
        .all()
    )


def _manager_for_actor(
    created_by: str | None, manager_by_actor: dict[str, MatchingManager]
) -> MatchingManager | None:
    actor = str(created_by or "").strip()
    for suffix, manager in manager_by_actor.items():
        if actor == manager.user_id or actor.endswith(suffix):
            return manager
    return None


def _build_queue_summary(db: Session) -> dict:
    rows = db.execute(
        select(
            CompetitorItem.item_type,
            CompetitorItemMatch.status,
            func.count(CompetitorItemMatch.id),
        )
        .join(CompetitorItem, CompetitorItem.id == CompetitorItemMatch.competitor_item_id)
        .where(CompetitorItemMatch.status.in_(REVIEW_STATUSES))
        .group_by(CompetitorItem.item_type, CompetitorItemMatch.status)
    ).all()
    by_status: Counter[str] = Counter()
    by_item_type: Counter[str] = Counter()
    total = 0
    display = 0
    for item_type, status, count in rows:
        status_value = _enum_value(status)
        item_type_value = item_type or "<empty>"
        count_int = int(count)
        by_status[status_value] += count_int
        by_item_type[item_type_value] += count_int
        total += count_int
        if item_type_value == "display":
            display += count_int
    return {
        "total": total,
        "display": display,
        "by_status": dict(sorted(by_status.items())),
        "by_item_type": dict(sorted(by_item_type.items())),
    }


def _build_suspicious_accepts(
    db: Session,
    accepts: list[ProductCompetitorItemDecision],
    manager_by_actor: dict[str, MatchingManager],
) -> list[dict]:
    suspicious: list[dict] = []
    for decision in accepts:
        product = decision.product
        item = decision.competitor_item
        if product is None or item is None:
            continue
        reasons = suspicious_accept_reasons(db, decision=decision, product=product, item=item)
        if not reasons:
            continue
        manager = _manager_for_actor(decision.created_by, manager_by_actor)
        suspicious.append(
            {
                "decision_id": decision.id,
                "created_at": decision.created_at.isoformat() if decision.created_at else None,
                "manager_user_id": manager.user_id if manager else "",
                "manager_name": manager.name if manager else str(decision.created_by or ""),
                "product_id": product.id,
                "product_article": product.article,
                "product_name": product.name,
                "competitor_item_id": item.id,
                "competitor": item.competitor,
                "competitor_name": item.name,
                "reasons": reasons,
            }
        )
    return suspicious


def suspicious_accept_reasons(
    db: Session,
    *,
    decision: ProductCompetitorItemDecision,
    product: Product,
    item: CompetitorItem,
) -> list[str]:
    reasons: list[str] = []
    expected_type = _infer_product_item_type(product)
    item_type = (item.item_type or "").strip().lower()
    if expected_type and item_type and expected_type != item_type:
        reasons.append(f"item_type_conflict:{expected_type}!={item_type}")

    is_display_pair = expected_type == "display" or item_type == "display"
    if is_display_pair:
        reasons.extend(_display_conflict_reasons(product, item))

    if _has_later_negative_decision(db, decision):
        reasons.append("later_rejected_or_revoked")

    return reasons


def _display_conflict_reasons(product: Product, item: CompetitorItem) -> list[str]:
    reasons: list[str] = []
    checkable = 0

    item_model = item.attrs_model or item.parsed_device_model or item.product_model
    if item_model:
        checkable += 1
        if not _model_appears_in_product(item_model, product.name):
            reasons.append("display_model_conflict")

    product_color = _normalize_color(product.color)
    item_color = _normalize_color(item.attrs_color or item.color)
    if product_color and item_color:
        checkable += 1
        if product_color != item_color:
            reasons.append("display_color_conflict")

    if product.display_has_frame is not None and item.has_frame is not None:
        checkable += 1
        if bool(product.display_has_frame) != bool(item.has_frame):
            reasons.append("display_frame_conflict")

    product_quality = _normalize_quality(
        product.display_quality or product.display_quality_raw or product.quality
    )
    item_quality = _normalize_quality(item.attrs_quality or item.screen_quality_grade)
    if product_quality and item_quality:
        checkable += 1
        if product_quality != item_quality:
            reasons.append("display_quality_conflict")

    product_matrix = _normalize_display_family(product.display_type or product.display_construction)
    item_matrix = _normalize_display_family(
        item.screen_matrix_type or item.attrs_type or item.screen_construction
    )
    if product_matrix and item_matrix:
        checkable += 1
        if product_matrix != item_matrix:
            reasons.append("display_matrix_conflict")

    item_refresh = item.attrs_refresh_rate_hz or item.refresh_rate_hz
    if product.display_refresh_rate_hz is not None and item_refresh is not None:
        checkable += 1
        if int(product.display_refresh_rate_hz) != int(item_refresh):
            reasons.append("display_refresh_rate_conflict")

    if checkable < 2:
        reasons.append("display_insufficient_attributes")
    return reasons


def _has_later_negative_decision(db: Session, decision: ProductCompetitorItemDecision) -> bool:
    if not decision.created_at:
        return False
    return bool(
        db.scalar(
            select(ProductCompetitorItemDecision.id)
            .where(
                ProductCompetitorItemDecision.product_id == decision.product_id,
                ProductCompetitorItemDecision.competitor_item_id == decision.competitor_item_id,
                ProductCompetitorItemDecision.created_at > decision.created_at,
                ProductCompetitorItemDecision.action.in_(("reject", "revoke")),
            )
            .limit(1)
        )
    )


def _manual_examples_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(ProductCompetitorItemDecision.id)).where(
                ProductCompetitorItemDecision.action.in_(("accept", "reject"))
            )
        )
        or 0
    )


def _infer_product_item_type(product: Product) -> str | None:
    text = _norm(" ".join(filter(None, [product.subject, product.subject_1c, product.name])))
    if any(token in text for token in ("дисп", "экран", "тачскрин", "lcd", "oled", "amoled")):
        return "display"
    if any(token in text for token in ("аккумулятор", "акб", "battery")):
        return "battery"
    if any(token in text for token in ("кабель", "cable")):
        return "cable"
    if any(token in text for token in ("корпус", "крышка", "housing")):
        return "housing"
    if any(token in text for token in ("камера", "camera")):
        return "camera"
    if any(token in text for token in ("шлейф", "flex")):
        return "flex"
    if any(token in text for token in ("разъем", "разъём", "connector")):
        return "connector"
    return None


def _model_appears_in_product(item_model: str, product_name: str | None) -> bool:
    model_tokens = [token for token in _norm(item_model).split() if len(token) >= 2]
    product_text = _norm(product_name)
    significant = [
        token
        for token in model_tokens
        if token not in {"for", "для", "galaxy", "redmi", "iphone", "samsung", "apple"}
    ]
    tokens = significant or model_tokens
    return bool(tokens) and all(token in product_text for token in tokens)


def _normalize_color(value: str | None) -> str | None:
    text = _norm(value)
    if not text:
        return None
    aliases = {
        "black": "black",
        "черный": "black",
        "чёрный": "black",
        "белый": "white",
        "white": "white",
        "золот": "gold",
        "gold": "gold",
        "silver": "silver",
        "сереб": "silver",
        "blue": "blue",
        "синий": "blue",
        "green": "green",
        "зелен": "green",
        "red": "red",
        "красн": "red",
    }
    for needle, normalized in aliases.items():
        if needle in text:
            return normalized
    return text


def _normalize_quality(value: str | None) -> str | None:
    text = _norm(value)
    if not text:
        return None
    if "orig100" in text or "or100" in text or "100" in text:
        return "orig100"
    if "original" in text or "ориг" in text or text == "or":
        return "original"
    if "oled" in text or "amoled" in text:
        return "oled"
    if "tft" in text:
        return "tft"
    if "copy" in text or "коп" in text:
        return "copy"
    if "premium" in text:
        return "premium"
    return text


def _normalize_display_family(value: str | None) -> str | None:
    text = _norm(value)
    if not text:
        return None
    if "oled" in text or "amoled" in text:
        return "oled"
    if "tft" in text:
        return "tft"
    if "incell" in text or "in cell" in text or "in-cell" in text:
        return "incell"
    if "lcd" in text:
        return "lcd"
    return text


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", " ").split())


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)
