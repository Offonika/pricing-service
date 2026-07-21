from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import ProductCompetitorItemDecision
from app.services.manual_matching_control import (
    MOSCOW_TZ,
    day_bounds,
    pair_conflict_reasons,
    report_date_today,
)

MANUAL_ACTIONS = frozenset({"accept", "reject", "revoke"})


def build_manual_matching_feedback_report(
    db: Session,
    *,
    as_of: date | None = None,
    sample_limit: int = 20,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a read-only audit and clean binary dataset from manual decisions.

    The latest action for each product/item pair wins. Latest ``revoke`` rows and
    latest accepts that conflict with current explainable rules are excluded from
    the binary dataset, but remain visible in the audit.
    """

    target_date = as_of or report_date_today()
    _, end_at = day_bounds(target_date)
    decisions = list(
        db.execute(
            select(ProductCompetitorItemDecision)
            .options(
                joinedload(ProductCompetitorItemDecision.product),
                joinedload(ProductCompetitorItemDecision.competitor_item),
            )
            .where(
                ProductCompetitorItemDecision.action.in_(MANUAL_ACTIONS),
                ProductCompetitorItemDecision.created_at < end_at,
            )
            .order_by(
                ProductCompetitorItemDecision.created_at.asc(),
                ProductCompetitorItemDecision.id.asc(),
            )
        )
        .scalars()
        .all()
    )

    latest_by_pair: dict[tuple[int, int], ProductCompetitorItemDecision] = {}
    raw_actions: Counter[str] = Counter()
    raw_reason_filled: Counter[str] = Counter()
    for decision in decisions:
        action = _action(decision.action)
        raw_actions[action] += 1
        if _has_reason(decision.reason):
            raw_reason_filled[action] += 1
        latest_by_pair[(decision.product_id, decision.competitor_item_id)] = decision

    clean_rows: list[dict[str, Any]] = []
    by_item_type: dict[str, Counter[str]] = {}
    by_competitor: dict[str, Counter[str]] = {}
    diagnostic_reasons: Counter[str] = Counter()
    suspicious_samples: list[dict[str, Any]] = []
    unexplained_reject_samples: list[dict[str, Any]] = []
    summary: Counter[str] = Counter()

    for decision in latest_by_pair.values():
        product = decision.product
        item = decision.competitor_item
        if product is None or item is None:
            summary["skipped_missing_entities"] += 1
            continue

        action = _action(decision.action)
        item_type = _dimension(item.item_type)
        competitor = _dimension(item.competitor)
        type_stats = by_item_type.setdefault(item_type, Counter())
        competitor_stats = by_competitor.setdefault(competitor, Counter())
        for bucket in (type_stats, competitor_stats):
            bucket[f"latest_{action}"] += 1
            if _has_reason(decision.reason):
                bucket["reason_filled"] += 1

        reasons = pair_conflict_reasons(product=product, item=item)
        guardrail_allowed = not any(reason.startswith("guardrail_") for reason in reasons)
        for reason in reasons:
            diagnostic_reasons[reason] += 1

        if action == "revoke":
            summary["excluded_revoked"] += 1
            type_stats["excluded_revoked"] += 1
            competitor_stats["excluded_revoked"] += 1
            continue

        if action == "accept":
            summary["latest_accepts"] += 1
            if guardrail_allowed:
                summary["accepted_allowed_by_guardrails"] += 1
            else:
                summary["accepted_blocked_by_guardrails"] += 1
            if reasons:
                summary["excluded_suspicious_accepts"] += 1
                type_stats["excluded_suspicious_accepts"] += 1
                competitor_stats["excluded_suspicious_accepts"] += 1
                _append_sample(
                    suspicious_samples,
                    _sample(decision, reasons),
                    sample_limit,
                )
                continue
            label = 1
            summary["clean_positive"] += 1
            type_stats["clean_positive"] += 1
            competitor_stats["clean_positive"] += 1
        else:
            summary["latest_rejects"] += 1
            if guardrail_allowed:
                summary["rejected_allowed_by_guardrails"] += 1
            else:
                summary["rejected_blocked_by_guardrails"] += 1
            label = 0
            summary["clean_negative"] += 1
            type_stats["clean_negative"] += 1
            competitor_stats["clean_negative"] += 1
            if reasons:
                summary["negative_with_rule_conflict"] += 1
                type_stats["negative_with_rule_conflict"] += 1
                competitor_stats["negative_with_rule_conflict"] += 1
            else:
                summary["negative_without_rule_conflict"] += 1
                type_stats["negative_without_rule_conflict"] += 1
                competitor_stats["negative_without_rule_conflict"] += 1
                _append_sample(
                    unexplained_reject_samples,
                    _sample(decision, reasons),
                    sample_limit,
                )

        clean_rows.append(
            {
                "decision_id": decision.id,
                "decided_at": _iso(decision.created_at),
                "label": label,
                "action": action,
                "product_id": product.id,
                "product_article": product.article,
                "product_name": product.name,
                "competitor_item_id": item.id,
                "competitor": item.competitor,
                "competitor_external_id": item.external_id,
                "competitor_name": item.name,
                "item_type": item.item_type,
                "manual_reason": decision.reason,
                "created_by": decision.created_by,
                "guardrail_allowed": guardrail_allowed,
                "diagnostic_reasons": "|".join(reasons),
            }
        )

    clean_total = summary["clean_positive"] + summary["clean_negative"]
    latest_total = len(latest_by_pair)
    period_start = _iso(decisions[0].created_at) if decisions else None
    period_end = _iso(decisions[-1].created_at) if decisions else None
    reason_coverage = {
        action: {
            "with_reason": raw_reason_filled[action],
            "total": raw_actions[action],
            "rate": _ratio(raw_reason_filled[action], raw_actions[action]),
        }
        for action in sorted(MANUAL_ACTIONS)
    }

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(MOSCOW_TZ).isoformat(),
        "as_of": target_date.isoformat(),
        "period": {"from": period_start, "to": period_end},
        "summary": {
            "raw_decisions": len(decisions),
            "raw_actions": dict(sorted(raw_actions.items())),
            "unique_pairs": latest_total,
            "duplicate_decisions_collapsed": len(decisions) - latest_total,
            "clean_examples": clean_total,
            "clean_positive": summary["clean_positive"],
            "clean_negative": summary["clean_negative"],
            "positive_rate": _ratio(summary["clean_positive"], clean_total),
            "excluded_revoked": summary["excluded_revoked"],
            "excluded_suspicious_accepts": summary["excluded_suspicious_accepts"],
            "skipped_missing_entities": summary["skipped_missing_entities"],
        },
        "reason_coverage": reason_coverage,
        "guardrail_replay": {
            "accepted_allowed": summary["accepted_allowed_by_guardrails"],
            "accepted_blocked": summary["accepted_blocked_by_guardrails"],
            "accepted_allowed_rate": _ratio(
                summary["accepted_allowed_by_guardrails"], summary["latest_accepts"]
            ),
            "rejected_blocked": summary["rejected_blocked_by_guardrails"],
            "rejected_allowed": summary["rejected_allowed_by_guardrails"],
            "rejected_blocked_rate": _ratio(
                summary["rejected_blocked_by_guardrails"], summary["latest_rejects"]
            ),
            "negative_with_rule_conflict": summary["negative_with_rule_conflict"],
            "negative_without_rule_conflict": summary["negative_without_rule_conflict"],
        },
        "by_item_type": _dimension_rows(by_item_type),
        "by_competitor": _dimension_rows(by_competitor),
        "top_diagnostic_reasons": [
            {"reason": reason, "count": count}
            for reason, count in diagnostic_reasons.most_common(20)
        ],
        "samples": {
            "suspicious_accepts": suspicious_samples,
            "unexplained_rejects": unexplained_reject_samples,
        },
        "limitations": [
            "Проверка воспроизводит текущие базовые guardrails и объяснимые проверки дисплеев, а не исторический top-K embedding matcher.",
            "Журнал не хранит снимок признаков, score и списка кандидатов на момент ручного решения; анализ использует текущее состояние Product и CompetitorItem.",
            "Свободный текст причины заполнен не для всех решений и не нормализован в reason_code.",
        ],
    }
    report["recommendations"] = _recommendations(report)
    return report, clean_rows


def render_manual_matching_feedback_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    replay = report["guardrail_replay"]
    lines = [
        "# Анализ ручной разметки сопоставления товаров",
        "",
        f"Срез на дату: {report['as_of']} (Europe/Moscow).",
        f"Период решений: {report['period']['from'] or 'нет данных'} — "
        f"{report['period']['to'] or 'нет данных'}.",
        "",
        "## Итог",
        "",
        f"- Ручных событий: {summary['raw_decisions']}.",
        f"- Уникальных пар после выбора последнего решения: {summary['unique_pairs']}.",
        f"- Чистая бинарная выборка: {summary['clean_examples']} "
        f"(+{summary['clean_positive']} / -{summary['clean_negative']}).",
        f"- Исключено revoke: {summary['excluded_revoked']}.",
        f"- Исключено подозрительных accept: {summary['excluded_suspicious_accepts']}.",
        "",
        "## Проверка текущих правил",
        "",
        f"- Guardrails блокируют {replay['accepted_blocked']} ручных accept.",
        f"- Guardrails объясняют {replay['rejected_blocked']} ручных reject.",
        f"- Reject без объяснимого конфликта текущих правил: "
        f"{replay['negative_without_rule_conflict']}.",
        "",
        "## Категории",
        "",
        "| Категория | Положительные | Отрицательные | Подозрительные accept | Reject без конфликта |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in report["by_item_type"]:
        lines.append(
            f"| {_md(row['name'])} | {row['clean_positive']} | {row['clean_negative']} | "
            f"{row['excluded_suspicious_accepts']} | {row['negative_without_rule_conflict']} |"
        )

    lines.extend(["", "## Заполнение причин", ""])
    for action, coverage in report["reason_coverage"].items():
        lines.append(
            f"- `{action}`: {coverage['with_reason']} из {coverage['total']} "
            f"({coverage['rate']:.1%})."
        )

    if report["top_diagnostic_reasons"]:
        lines.extend(["", "## Частые диагностические причины", ""])
        for row in report["top_diagnostic_reasons"][:10]:
            lines.append(f"- `{row['reason']}`: {row['count']}.")

    lines.extend(["", "## Рекомендации", ""])
    lines.extend(f"- {value}" for value in report["recommendations"])
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {value}" for value in report["limitations"])
    return "\n".join(lines) + "\n"


def _recommendations(report: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    reject_coverage = report["reason_coverage"]["reject"]["rate"]
    if reject_coverage < 0.8:
        recommendations.append(
            "Сделать структурированную причину обязательной для reject/revoke; текущего заполнения недостаточно для надёжной классификации ошибок."
        )
    categories = report["by_item_type"]
    if categories:
        priority = max(
            categories,
            key=lambda row: (row["negative_without_rule_conflict"], row["clean_examples"]),
        )
        if priority["negative_without_rule_conflict"]:
            recommendations.append(
                f"Первой разбирать категорию `{priority['name']}`: в ней "
                f"{priority['negative_without_rule_conflict']} reject без объяснимого конфликта текущих правил."
            )
    if report["summary"]["excluded_suspicious_accepts"]:
        recommendations.append(
            "Перепроверить подозрительные accept до использования положительных меток в обучении."
        )
    recommendations.append(
        "После исправления повторяющихся правил прогнать тот же срез повторно и сравнить метрики до включения автопринятия."
    )
    return recommendations


def _dimension_rows(values: dict[str, Counter[str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, counts in values.items():
        clean_examples = counts["clean_positive"] + counts["clean_negative"]
        rows.append(
            {
                "name": name,
                "clean_examples": clean_examples,
                "clean_positive": counts["clean_positive"],
                "clean_negative": counts["clean_negative"],
                "positive_rate": _ratio(counts["clean_positive"], clean_examples),
                "latest_accept": counts["latest_accept"],
                "latest_reject": counts["latest_reject"],
                "latest_revoke": counts["latest_revoke"],
                "excluded_revoked": counts["excluded_revoked"],
                "excluded_suspicious_accepts": counts["excluded_suspicious_accepts"],
                "negative_with_rule_conflict": counts["negative_with_rule_conflict"],
                "negative_without_rule_conflict": counts["negative_without_rule_conflict"],
                "reason_filled": counts["reason_filled"],
            }
        )
    return sorted(rows, key=lambda row: (-row["clean_examples"], row["name"]))


def _sample(
    decision: ProductCompetitorItemDecision,
    reasons: list[str],
) -> dict[str, Any]:
    product = decision.product
    item = decision.competitor_item
    return {
        "decision_id": decision.id,
        "decided_at": _iso(decision.created_at),
        "product_id": decision.product_id,
        "product_article": product.article if product else None,
        "product_name": product.name if product else None,
        "competitor_item_id": decision.competitor_item_id,
        "competitor": item.competitor if item else None,
        "competitor_external_id": item.external_id if item else None,
        "competitor_name": item.name if item else None,
        "item_type": item.item_type if item else None,
        "manual_reason": decision.reason,
        "diagnostic_reasons": reasons,
    }


def _append_sample(values: list[dict[str, Any]], value: dict[str, Any], limit: int) -> None:
    if len(values) < max(limit, 0):
        values.append(value)


def _action(value: str | None) -> str:
    return str(value or "").strip().lower()


def _dimension(value: str | None) -> str:
    return str(value or "").strip().lower() or "<empty>"


def _has_reason(value: str | None) -> bool:
    return bool(str(value or "").strip())


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
