"""Audit first-party-in-transit hidden demand on immutable display history.

The task is deliberately shadow-only.  It reads the checksum-validated frozen
preflight and the append-only lifecycle replay store, writes report artifacts,
and optionally stores the derived daily trajectory in the local replay store.
It never writes application data, 1C lifecycle properties or supplier orders.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
    stable_hash,
)
from tasks.display_auto_order_backtest_preflight import validate_preflight_directory

ZERO = Decimal("0")
KMP4_WEIGHT = Decimal("0.5")
SITE_ORDER_WEIGHT = Decimal("1")
SITE_CART_WEIGHT = Decimal("0.25")
RESERVE_BACKLOG_WEIGHT = Decimal("1")
FALLBACK_P75_DAYS = 52
MODEL_VERSION = "assortment_lifecycle_v2_first_party_in_transit_hidden_demand.v1"


@dataclass(frozen=True)
class FirstPartyMilestone:
    nomenclature_code: str
    name: str
    first_supplier_order_at: date
    first_cargo_at: date
    first_physical_inflow_at: date | None
    first_sale_at: date | None
    first_party_qty: Decimal
    first_party_qty_known: bool


@dataclass(frozen=True)
class HistoricalProfile:
    business_date: date
    launch_typical_min_qty: Decimal
    launch_typical_max_qty: Decimal
    lead_time_p75_days: int
    launch_profile_known: bool
    lead_time_known: bool


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (InvalidOperation, ValueError):
        return ZERO


def _date(value: Any) -> date | None:
    rendered = _clean(value)
    if not rendered:
        return None
    try:
        return date.fromisoformat(rendered[:10])
    except ValueError:
        return None


def _valid_cargo_date(value: Any) -> date | None:
    parsed = _date(value)
    return parsed if parsed is not None and parsed >= date(2000, 1, 1) else None


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _minimum_date(values: Iterable[date | None]) -> date | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _json_dates(value: Any) -> list[date]:
    if not isinstance(value, (list, tuple, set)):
        value = (value,)
    return [parsed for raw in value if (parsed := _date(raw)) is not None]


def _read_json_payload(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise ValueError("replay fact payload must be an object")
    return dict(parsed)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"replay store not found: {path}")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_first_party_milestones(
    *, store_path: Path, dataset_hash: str, date_from: date, date_to: date
) -> tuple[dict[str, FirstPartyMilestone], dict[str, Any]]:
    """Reconstruct the first cargo window without using technical cargo dates."""

    with _readonly_connection(store_path) as connection:
        dataset = connection.execute(
            """
            SELECT scope, observation_from, observation_to, content_sha256, fact_count
            FROM replay_dataset WHERE dataset_hash = ?
            """,
            (dataset_hash,),
        ).fetchone()
        if dataset is None:
            raise ValueError(f"replay dataset not found: {dataset_hash}")
        if date.fromisoformat(dataset["observation_from"]) > date_from:
            raise ValueError("replay dataset does not cover requested period start")
        if date.fromisoformat(dataset["observation_to"]) < date_to:
            raise ValueError("replay dataset does not cover requested period end")

        items: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT nomenclature_code, payload_json
            FROM replay_dataset_fact
            WHERE dataset_hash = ? AND fact_type = 'item'
            """,
            (dataset_hash,),
        ):
            items[_clean(row["nomenclature_code"])] = _read_json_payload(row["payload_json"])

        order_dates: dict[str, list[date]] = defaultdict(list)
        cargo_dates: dict[str, list[date]] = defaultdict(list)
        cargo_lines: dict[tuple[str, date], dict[tuple[str, str, str], Decimal]] = defaultdict(dict)
        technical_cargo_skus: set[str] = set()
        technical_cargo_values = 0
        for row in connection.execute(
            """
            SELECT business_date, nomenclature_code, payload_json
            FROM replay_dataset_fact
            WHERE dataset_hash = ? AND fact_type = 'supplier_order'
            """,
            (dataset_hash,),
        ):
            code = _clean(row["nomenclature_code"])
            payload = _read_json_payload(row["payload_json"])
            created_at = _date(payload.get("created_at")) or _date(row["business_date"])
            if created_at is not None:
                order_dates[code].append(created_at)
            raw_cargo = payload.get("cargo_handoff_at")
            parsed_raw_cargo = _date(raw_cargo)
            cargo_at = _valid_cargo_date(raw_cargo)
            if parsed_raw_cargo is not None and cargo_at is None:
                technical_cargo_values += 1
                technical_cargo_skus.add(code)
            if cargo_at is None:
                continue
            cargo_dates[code].append(cargo_at)
            identity = (
                _clean(payload.get("order_ref")),
                created_at.isoformat() if created_at else "",
                _clean(payload.get("qty")),
            )
            cargo_lines[(code, cargo_at)][identity] = max(ZERO, _decimal(payload.get("qty")))

        first_receipt_fact: dict[str, date] = {}
        for row in connection.execute(
            """
            SELECT nomenclature_code, MIN(business_date) AS first_at
            FROM replay_dataset_fact
            WHERE dataset_hash = ? AND fact_type = 'receipt'
            GROUP BY nomenclature_code
            """,
            (dataset_hash,),
        ):
            parsed = _date(row["first_at"])
            if parsed is not None:
                first_receipt_fact[_clean(row["nomenclature_code"])] = parsed

        first_sale_fact: dict[str, date] = {}
        for row in connection.execute(
            """
            SELECT nomenclature_code, MIN(business_date) AS first_at
            FROM replay_dataset_fact
            WHERE dataset_hash = ? AND fact_type = 'sale'
            GROUP BY nomenclature_code
            """,
            (dataset_hash,),
        ):
            parsed = _date(row["first_at"])
            if parsed is not None:
                first_sale_fact[_clean(row["nomenclature_code"])] = parsed

    milestones: dict[str, FirstPartyMilestone] = {}
    cargo_before_order_skus: set[str] = set()
    cargo_without_order_skus: set[str] = set()
    quantity_unknown_skus: set[str] = set()
    physical_before_cargo_skus: set[str] = set()
    sale_before_physical_skus: set[str] = set()

    for code, item in items.items():
        source_order_at = _date(item.get("first_supplier_order_at"))
        first_order_at = _minimum_date([source_order_at, *order_dates.get(code, ())])
        source_cargo_dates = _json_dates(item.get("supplier_order_cargo_handoff_dates"))
        for cargo_at in source_cargo_dates:
            if cargo_at < date(2000, 1, 1):
                technical_cargo_values += 1
                technical_cargo_skus.add(code)
            else:
                cargo_dates[code].append(cargo_at)
        valid_cargo = sorted(set(cargo_dates.get(code, ())))
        if valid_cargo and first_order_at is None:
            cargo_without_order_skus.add(code)
            continue
        if first_order_at is None:
            continue
        cargo_before_order_skus.update(
            [code] if any(value < first_order_at for value in valid_cargo) else []
        )
        valid_cargo = [value for value in valid_cargo if value >= first_order_at]
        if not valid_cargo:
            continue
        first_cargo_at = min(valid_cargo)

        first_physical_at = _minimum_date(
            [
                _date(item.get("first_receipt_at")),
                _date(item.get("first_stock_inflow_at")),
                first_receipt_fact.get(code),
            ]
        )
        first_sale_at = _minimum_date([_date(item.get("first_sale_at")), first_sale_fact.get(code)])
        if first_physical_at is not None and first_physical_at <= first_cargo_at:
            physical_before_cargo_skus.add(code)
            continue
        if first_sale_at is not None and first_sale_at <= first_cargo_at:
            continue
        if first_sale_at is not None and (
            first_physical_at is None or first_sale_at < first_physical_at
        ):
            sale_before_physical_skus.add(code)

        state_end = _minimum_date([first_physical_at, first_sale_at]) or (
            date_to + timedelta(days=1)
        )
        if first_cargo_at > date_to or state_end <= date_from:
            continue
        line_quantities = cargo_lines.get((code, first_cargo_at), {})
        quantity_known = bool(line_quantities)
        quantity = sum(line_quantities.values(), ZERO)
        if not quantity_known or quantity <= ZERO:
            quantity_unknown_skus.add(code)
        milestones[code] = FirstPartyMilestone(
            nomenclature_code=code,
            name=_clean(item.get("name") or item.get("additional_name_1c")),
            first_supplier_order_at=first_order_at,
            first_cargo_at=first_cargo_at,
            first_physical_inflow_at=first_physical_at,
            first_sale_at=first_sale_at,
            first_party_qty=quantity,
            first_party_qty_known=quantity_known and quantity > ZERO,
        )

    quality = {
        "dataset_hash": dataset_hash,
        "dataset_scope": dataset["scope"],
        "dataset_observation_from": dataset["observation_from"],
        "dataset_observation_to": dataset["observation_to"],
        "dataset_content_sha256": dataset["content_sha256"],
        "dataset_fact_count": int(dataset["fact_count"]),
        "item_count": len(items),
        "first_party_window_sku_count": len(milestones),
        "technical_cargo_value_count": technical_cargo_values,
        "technical_cargo_sku_count": len(technical_cargo_skus),
        "cargo_before_order_sku_count": len(cargo_before_order_skus),
        "cargo_without_order_sku_count": len(cargo_without_order_skus),
        "physical_before_cargo_sku_count": len(physical_before_cargo_skus),
        "sale_before_physical_sku_count": len(sale_before_physical_skus),
        "first_party_quantity_unknown_sku_count": len(quantity_unknown_skus),
    }
    return milestones, quality


def load_historical_profiles(
    *, path: Path, relevant_codes: set[str]
) -> tuple[dict[str, list[HistoricalProfile]], dict[str, set[date]]]:
    profiles: dict[str, list[HistoricalProfile]] = defaultdict(list)
    calculation_dates: dict[str, set[date]] = defaultdict(set)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = _clean(row.get("nomenclature_code"))
            if code not in relevant_codes:
                continue
            business_date = _date(row.get("decision_date"))
            if business_date is None:
                continue
            launch_min_text = _clean(row.get("launch_typical_min_qty"))
            p75_text = _clean(row.get("lead_time_p75_days"))
            profiles[code].append(
                HistoricalProfile(
                    business_date=business_date,
                    launch_typical_min_qty=max(ZERO, _decimal(launch_min_text)),
                    launch_typical_max_qty=max(ZERO, _decimal(row.get("launch_typical_max_qty"))),
                    lead_time_p75_days=max(1, int(_decimal(p75_text) or FALLBACK_P75_DAYS)),
                    launch_profile_known=bool(launch_min_text),
                    lead_time_known=bool(p75_text),
                )
            )
            calculation_dates[code].add(business_date)
    for rows in profiles.values():
        rows.sort(key=lambda row: row.business_date)
    return dict(profiles), dict(calculation_dates)


def latest_profile(rows: Sequence[HistoricalProfile], *, as_of: date) -> HistoricalProfile | None:
    dates = [row.business_date for row in rows]
    position = bisect.bisect_right(dates, as_of) - 1
    return rows[position] if position >= 0 else None


def calculate_daily_audit(
    *,
    fact: Mapping[str, Any],
    milestone: FirstPartyMilestone,
    profile: HistoricalProfile | None,
) -> dict[str, Any]:
    business_date = _date(fact.get("business_date"))
    if business_date is None:
        raise ValueError("daily fact business_date is required")
    launch_min = profile.launch_typical_min_qty if profile else ZERO
    lead_time_p75_days = profile.lead_time_p75_days if profile else FALLBACK_P75_DAYS
    cargo_age_days = (business_date - milestone.first_cargo_at).days
    first_party_reliable_by_age = cargo_age_days <= lead_time_p75_days
    frozen_free_incoming = max(ZERO, _decimal(fact.get("free_incoming_qty")))
    reliable_first_party_qty = (
        min(milestone.first_party_qty, frozen_free_incoming)
        if milestone.first_party_qty_known and first_party_reliable_by_age
        else ZERO
    )
    free_stock = max(
        ZERO,
        _decimal(fact.get("physical_stock_qty"))
        - max(ZERO, _decimal(fact.get("effective_reserve_qty"))),
    )
    source_quantities = {
        "kmp4": max(ZERO, _decimal(fact.get("kmp4_open_qty"))),
        "site_order": max(ZERO, _decimal(fact.get("site_order_open_qty"))),
        "site_cart": max(ZERO, _decimal(fact.get("site_cart_open_qty"))),
        "reserve_backlog": max(ZERO, _decimal(fact.get("reserve_backlog_open_qty"))),
    }
    weighted_sources = {
        "kmp4": source_quantities["kmp4"] * KMP4_WEIGHT,
        "site_order": source_quantities["site_order"] * SITE_ORDER_WEIGHT,
        "site_cart": source_quantities["site_cart"] * SITE_CART_WEIGHT,
        "reserve_backlog": source_quantities["reserve_backlog"] * RESERVE_BACKLOG_WEIGHT,
    }
    hidden_demand = sum(weighted_sources.values(), ZERO)
    strong_sources = [source for source, quantity in source_quantities.items() if quantity > ZERO]
    coverage = free_stock + reliable_first_party_qty
    start_gap = max(ZERO, launch_min - coverage)
    hidden_gap = max(ZERO, hidden_demand - max(ZERO, coverage - launch_min))
    combined_gap = max(ZERO, launch_min + hidden_demand - coverage)
    queue_open = bool(strong_sources and combined_gap > ZERO)
    blocker_codes = []
    if not milestone.first_party_qty_known:
        blocker_codes.append("first_party_quantity_unknown")
    if profile is None or not profile.launch_profile_known:
        blocker_codes.append("launch_profile_unknown")
    if profile is None or not profile.lead_time_known:
        blocker_codes.append("lead_time_p75_fallback")
    if not first_party_reliable_by_age:
        blocker_codes.append("cargo_older_than_p75")
    return {
        "business_date": business_date.isoformat(),
        "nomenclature_code": milestone.nomenclature_code,
        "name": milestone.name,
        "status": _clean(fact.get("status")),
        "first_supplier_order_at": milestone.first_supplier_order_at.isoformat(),
        "first_cargo_at": milestone.first_cargo_at.isoformat(),
        "first_physical_inflow_at": (
            milestone.first_physical_inflow_at.isoformat()
            if milestone.first_physical_inflow_at
            else ""
        ),
        "first_sale_at": milestone.first_sale_at.isoformat() if milestone.first_sale_at else "",
        "first_party_in_transit": 1,
        "cargo_age_days": cargo_age_days,
        "lead_time_p75_days": lead_time_p75_days,
        "first_party_reliable_by_age": int(first_party_reliable_by_age),
        "first_party_qty": str(milestone.first_party_qty),
        "first_party_qty_known": int(milestone.first_party_qty_known),
        "frozen_free_incoming_qty": str(frozen_free_incoming),
        "reliable_first_party_qty": str(reliable_first_party_qty),
        "free_stock_qty": str(free_stock),
        "launch_typical_min_qty": str(launch_min),
        "kmp4_open_qty": str(source_quantities["kmp4"]),
        "site_order_open_qty": str(source_quantities["site_order"]),
        "site_cart_open_qty": str(source_quantities["site_cart"]),
        "reserve_backlog_open_qty": str(source_quantities["reserve_backlog"]),
        "weighted_hidden_demand_qty": str(hidden_demand),
        "strong_signal_sources": "|".join(strong_sources),
        "strong_signal_present": int(bool(strong_sources)),
        "soft_signal_count": int(_decimal(fact.get("site_soft_trigger_count"))),
        "uncovered_start_need_qty": str(start_gap),
        "uncovered_hidden_need_qty": str(hidden_gap),
        "uncovered_combined_need_qty": str(combined_gap),
        "top_up_queue_open": int(queue_open),
        "top_up_review_qty": str(_ceil(combined_gap) if queue_open else ZERO),
        "blocker_codes": "|".join(blocker_codes),
        "production_action": "none_read_only",
    }


def build_queue_episodes(
    daily_rows: Sequence[Mapping[str, Any]],
    *,
    calculation_dates: Mapping[str, set[date]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        grouped[_clean(row.get("nomenclature_code"))].append(row)
    episodes: list[dict[str, Any]] = []
    for code, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: _clean(row.get("business_date")))
        active: list[Mapping[str, Any]] = []
        previous_date: date | None = None

        def close_episode(active_rows: Sequence[Mapping[str, Any]], current_code: str) -> None:
            if not active_rows:
                return
            start = _date(active_rows[0].get("business_date"))
            end = _date(active_rows[-1].get("business_date"))
            if start is None or end is None:
                return
            observed_calculations = sorted(
                value
                for value in calculation_dates.get(current_code, set())
                if start <= value <= end
            )
            boundaries = [start, *observed_calculations, end]
            maximum_gap = max(
                (
                    (right - left).days
                    for left, right in zip(boundaries, boundaries[1:], strict=False)
                ),
                default=0,
            )
            sources = sorted(
                {
                    source
                    for row in active_rows
                    for source in _clean(row.get("strong_signal_sources")).split("|")
                    if source
                }
            )
            maximum_qty = max(
                (_decimal(row.get("top_up_review_qty")) for row in active_rows),
                default=ZERO,
            )
            episodes.append(
                {
                    "nomenclature_code": current_code,
                    "name": _clean(active_rows[0].get("name")),
                    "opened_at": start.isoformat(),
                    "closed_or_observation_ended_at": end.isoformat(),
                    "open_days": (end - start).days + 1,
                    "strong_signal_sources": "|".join(sources),
                    "opening_review_qty": _clean(active_rows[0].get("top_up_review_qty")),
                    "maximum_review_qty": str(maximum_qty),
                    "calculation_row_count": len(observed_calculations),
                    "calculation_on_open_day": int(start in observed_calculations),
                    "maximum_calculation_gap_days": maximum_gap,
                    "calculation_cadence_over_5_days": int(maximum_gap > 5),
                    "required_human_reviews_at_5_day_cadence": 1
                    + math.floor(max(0, (end - start).days) / 5),
                    "human_review_observation": "not_available_in_frozen_history",
                    "production_action": "none_read_only",
                }
            )

        for row in ordered:
            current_date = _date(row.get("business_date"))
            is_open = _clean(row.get("top_up_queue_open")) == "1"
            if not is_open or (
                previous_date is not None
                and current_date is not None
                and (current_date - previous_date).days > 1
            ):
                close_episode(active, code)
                active = []
            if is_open:
                active.append(row)
            previous_date = current_date
        close_episode(active, code)
    return sorted(episodes, key=lambda row: (row["opened_at"], row["nomenclature_code"]))


def _quantile(values: Sequence[int], fraction: Decimal) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((fraction * Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING)))
    return ordered[rank - 1]


def summarize(
    *,
    milestones: Mapping[str, FirstPartyMilestone],
    quality: Mapping[str, Any],
    daily_rows: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
    date_from: date,
    date_to: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        rows_by_code[_clean(row.get("nomenclature_code"))].append(row)
    strong_codes = {
        code
        for code, rows in rows_by_code.items()
        if any(_clean(row.get("strong_signal_present")) == "1" for row in rows)
    }
    soft_only_codes = {
        code
        for code, rows in rows_by_code.items()
        if code not in strong_codes
        and any(int(_decimal(row.get("soft_signal_count"))) > 0 for row in rows)
    }
    queue_codes = {_clean(row.get("nomenclature_code")) for row in episodes}
    stale_codes = {
        code
        for code, rows in rows_by_code.items()
        if any("cargo_older_than_p75" in _clean(row.get("blocker_codes")) for row in rows)
    }
    source_codes: dict[str, set[str]] = defaultdict(set)
    for code, rows in rows_by_code.items():
        for row in rows:
            for source in _clean(row.get("strong_signal_sources")).split("|"):
                if source:
                    source_codes[source].add(code)

    durations = [
        (milestone.first_physical_inflow_at - milestone.first_cargo_at).days
        for milestone in milestones.values()
        if milestone.first_physical_inflow_at is not None
        and milestone.first_physical_inflow_at > milestone.first_cargo_at
    ]
    sku_rows: list[dict[str, Any]] = []
    for code, milestone in milestones.items():
        rows = rows_by_code.get(code, [])
        episode_rows = [row for row in episodes if row["nomenclature_code"] == code]
        max_hidden = max(
            (_decimal(row.get("weighted_hidden_demand_qty")) for row in rows), default=ZERO
        )
        max_review = max((_decimal(row.get("top_up_review_qty")) for row in rows), default=ZERO)
        sources = sorted(
            {
                source
                for row in rows
                for source in _clean(row.get("strong_signal_sources")).split("|")
                if source
            }
        )
        sku_rows.append(
            {
                "nomenclature_code": code,
                "name": milestone.name,
                "first_supplier_order_at": milestone.first_supplier_order_at.isoformat(),
                "first_cargo_at": milestone.first_cargo_at.isoformat(),
                "first_physical_inflow_at": (
                    milestone.first_physical_inflow_at.isoformat()
                    if milestone.first_physical_inflow_at
                    else ""
                ),
                "first_sale_at": (
                    milestone.first_sale_at.isoformat() if milestone.first_sale_at else ""
                ),
                "first_party_qty": str(milestone.first_party_qty),
                "first_party_qty_known": int(milestone.first_party_qty_known),
                "observed_in_transit_days": len(rows),
                "strong_signal_sources": "|".join(sources),
                "strong_signal_found": int(code in strong_codes),
                "soft_signal_only": int(code in soft_only_codes),
                "top_up_episode_count": len(episode_rows),
                "maximum_weighted_hidden_demand_qty": str(max_hidden),
                "maximum_top_up_review_qty": str(max_review),
                "cargo_became_older_than_p75": int(code in stale_codes),
                "maximum_calculation_gap_days": max(
                    (int(row["maximum_calculation_gap_days"]) for row in episode_rows),
                    default=0,
                ),
                "production_action": "none_read_only",
            }
        )
    sku_rows.sort(
        key=lambda row: (
            -_decimal(row["maximum_top_up_review_qty"]),
            row["nomenclature_code"],
        )
    )

    summary = {
        "schema": "display_first_party_in_transit_hidden_demand_audit.v1",
        "period": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        "scope": "display_assortment_only",
        "model_version": MODEL_VERSION,
        "production_action": "none_read_only",
        "parameters": {
            "kmp4_weight": str(KMP4_WEIGHT),
            "site_order_weight": str(SITE_ORDER_WEIGHT),
            "site_cart_weight": str(SITE_CART_WEIGHT),
            "reserve_backlog_weight": str(RESERVE_BACKLOG_WEIGHT),
            "cargo_reliability": "valid cargo date; exact first-party quantity capped by frozen free incoming; age not above historical P75",
            "queue_gate": "at least one strong signal and positive uncovered combined need",
            "quantity_formula": "max(0, launch_typical_min + weighted_hidden_demand - free_stock - reliable_first_party_qty)",
            "weights_status": "backtest_parameters_not_production_rules",
        },
        "coverage": {
            "first_party_window_sku_count": len(milestones),
            "first_party_daily_row_count": len(daily_rows),
            "first_party_sku_with_daily_facts_count": len(rows_by_code),
            "first_party_sku_without_daily_facts_count": len(set(milestones) - set(rows_by_code)),
            "strong_signal_sku_count": len(strong_codes),
            "soft_signal_only_sku_count": len(soft_only_codes),
            "top_up_queue_sku_count": len(queue_codes),
            "top_up_episode_count": len(episodes),
            "strong_signal_fully_covered_sku_count": len(strong_codes - queue_codes),
            "source_sku_counts": {
                source: len(codes) for source, codes in sorted(source_codes.items())
            },
        },
        "quantities": {
            "top_up_review_qty_at_episode_open_total": str(
                sum((_decimal(row.get("opening_review_qty")) for row in episodes), ZERO)
            ),
            "top_up_review_qty_episode_max_total": str(
                sum((_decimal(row.get("maximum_review_qty")) for row in episodes), ZERO)
            ),
        },
        "transit_duration_to_first_physical_inflow_days": {
            "completed_sku_count": len(durations),
            "median": _quantile(durations, Decimal("0.5")),
            "p75": _quantile(durations, Decimal("0.75")),
            "maximum": max(durations) if durations else None,
            "open_or_closed_by_sale_sku_count": len(milestones) - len(durations),
        },
        "review_cadence": {
            "episodes_with_calculation_on_open_day": sum(
                int(row["calculation_on_open_day"]) for row in episodes
            ),
            "episodes_with_calculation_gap_over_5_days": sum(
                int(row["calculation_cadence_over_5_days"]) for row in episodes
            ),
            "required_human_reviews_at_5_day_cadence": sum(
                int(row["required_human_reviews_at_5_day_cadence"]) for row in episodes
            ),
            "actual_human_review_history_available": False,
        },
        "quality": {
            **dict(quality),
            "cargo_older_than_p75_sku_count": len(stale_codes),
            "launch_profile_or_lead_time_blocker_sku_count": len(
                {
                    code
                    for code, rows in rows_by_code.items()
                    if any(_clean(row.get("blocker_codes")) for row in rows)
                }
            ),
            "call_transcript_exact_sku_quantity_history_available": False,
            "preflight_checksum_validation": "passed",
        },
        "conclusion": {
            "baseline_reproducible": True,
            "new_rule_historically_observable": True,
            "ready_for_production": False,
            "reason": "shadow audit only; call signals and actual human confirmations are not present in frozen history; source weights and cargo reliability remain backtest parameters",
        },
    }
    return summary, sku_rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _markdown(summary: Mapping[str, Any], examples: Sequence[Mapping[str, Any]]) -> str:
    coverage = summary["coverage"]
    quality = summary["quality"]
    cadence = summary["review_cadence"]
    durations = summary["transit_duration_to_first_physical_inflow_days"]
    lines = [
        "# Аудит «Первая партия в пути → Добираем»",
        "",
        "Расчёт выполнен на frozen-истории с 2026-01-01 по 2026-07-31. "
        "Никаких заказов, стадий или данных 1С он не изменяет.",
        "",
        "## Результат",
        "",
        f"- SKU с наблюдаемым окном первой партии в пути: `{coverage['first_party_window_sku_count']}`;",
        f"- SKU с сильным сигналом в этом окне: `{coverage['strong_signal_sku_count']}`;",
        f"- SKU, для которых тенево открылась бы очередь `Добираем`: `{coverage['top_up_queue_sku_count']}`;",
        f"- отдельных открытий очереди: `{coverage['top_up_episode_count']}`;",
        f"- сильный сигнал полностью покрыт запасом/надёжной первой партией: `{coverage['strong_signal_fully_covered_sku_count']}` SKU;",
        f"- только мягкие сигналы, без количественного добора: `{coverage['soft_signal_only_sku_count']}` SKU.",
        "",
        "## Срок первой партии",
        "",
        f"До первого физического прихода: медиана `{durations['median']}` дней, "
        f"P75 `{durations['p75']}`, максимум `{durations['maximum']}`.",
        "",
        "## Ограничения качества",
        "",
        f"- технические cargo-даты исключены: `{quality['technical_cargo_sku_count']}` SKU;",
        f"- количество первой партии не удалось доказать: `{quality['first_party_quantity_unknown_sku_count']}` SKU;",
        f"- cargo стало старше P75 хотя бы в один день: `{quality['cargo_older_than_p75_sku_count']}` SKU;",
        "- точная историческая связка `звонок → SKU → количество` отсутствует, поэтому звонки в количество не включены;",
        "- фактические подтверждения человека в frozen-наборе отсутствуют: проверена расчётная выдача карточек, а не действия сотрудников.",
        "",
        "## Частота проверки",
        "",
        f"Из `{coverage['top_up_episode_count']}` открытий расчётная карточка появилась в день открытия у "
        f"`{cadence['episodes_with_calculation_on_open_day']}`. Эпизодов с расчётным разрывом более пяти дней: "
        f"`{cadence['episodes_with_calculation_gap_over_5_days']}`.",
        "",
        "## Примеры с наибольшим теневым добором",
        "",
        "| SKU | Товар | Cargo | Приход | Сигналы | Максимум к проверке |",
        "| --- | --- | --- | --- | --- | ---: |",
    ]
    for row in examples[:10]:
        lines.append(
            f"| {row['nomenclature_code']} | {_clean(row['name']).replace('|', '/')} | "
            f"{row['first_cargo_at']} | {row['first_physical_inflow_at'] or 'нет'} | "
            f"{row['strong_signal_sources'] or 'нет'} | {row['maximum_top_up_review_qty']} |"
        )
    lines.extend(
        [
            "",
            "## Вывод",
            "",
            "Правило исторически воспроизводится, но пока остаётся shadow-кандидатом. "
            "До production нужны привязка звонков, журнал человеческих подтверждений и "
            "отдельное согласование весов сигналов и критерия надёжности cargo.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_audit(
    *,
    preflight_dir: Path,
    store_path: Path,
    dataset_hash: str,
    date_from: date,
    date_to: date,
    output_dir: Path,
    store_trajectory: bool,
) -> dict[str, Any]:
    preflight_manifest = validate_preflight_directory(preflight_dir)
    if date.fromisoformat(preflight_manifest["date_from"]) > date_from:
        raise ValueError("preflight does not cover requested period start")
    if date.fromisoformat(preflight_manifest["date_to"]) < date_to:
        raise ValueError("preflight does not cover requested period end")
    milestones, quality = load_first_party_milestones(
        store_path=store_path,
        dataset_hash=dataset_hash,
        date_from=date_from,
        date_to=date_to,
    )
    profiles, calculation_dates = load_historical_profiles(
        path=preflight_dir / "decision-inputs.csv",
        relevant_codes=set(milestones),
    )

    daily_rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    with (preflight_dir / "daily-facts.csv").open(encoding="utf-8-sig", newline="") as handle:
        for fact in csv.DictReader(handle):
            code = _clean(fact.get("nomenclature_code"))
            milestone = milestones.get(code)
            if milestone is None:
                continue
            business_date = _date(fact.get("business_date"))
            if business_date is None or not date_from <= business_date <= date_to:
                continue
            state_end = _minimum_date(
                [milestone.first_physical_inflow_at, milestone.first_sale_at]
            ) or (date_to + timedelta(days=1))
            if not milestone.first_cargo_at <= business_date < state_end:
                continue
            key = (business_date.isoformat(), code)
            if key in seen_keys:
                raise ValueError(f"duplicate frozen daily fact: {key}")
            seen_keys.add(key)
            daily_rows.append(
                calculate_daily_audit(
                    fact=fact,
                    milestone=milestone,
                    profile=latest_profile(profiles.get(code, ()), as_of=business_date),
                )
            )

    daily_rows.sort(key=lambda row: (row["business_date"], row["nomenclature_code"]))
    episodes = build_queue_episodes(daily_rows, calculation_dates=calculation_dates)
    summary, sku_rows = summarize(
        milestones=milestones,
        quality=quality,
        daily_rows=daily_rows,
        episodes=episodes,
        date_from=date_from,
        date_to=date_to,
    )

    policy = {
        "model_version": MODEL_VERSION,
        "first_party_flag": "first_cargo_at <= day < min(first_physical_inflow_at, first_sale_at)",
        "technical_cargo_before": "2000-01-01",
        "cargo_reliability_boundary": "historical_p75_strict",
        "cargo_quantity_cap": "frozen_free_incoming_qty",
        "weights": {
            "kmp4": str(KMP4_WEIGHT),
            "site_order": str(SITE_ORDER_WEIGHT),
            "site_cart": str(SITE_CART_WEIGHT),
            "reserve_backlog": str(RESERVE_BACKLOG_WEIGHT),
        },
        "queue_gate": "strong_signal_and_positive_combined_gap",
        "production_action": "none_read_only",
    }
    policy_hash = stable_hash(policy)
    trajectory_result = None
    if store_trajectory:
        trajectory_result = AssortmentLifecycleReplayStore(store_path).put_trajectory(
            dataset_hash=dataset_hash,
            model_version=MODEL_VERSION,
            policy_hash=policy_hash,
            period_from=date_from,
            period_to=date_to,
            rows=daily_rows,
            metadata={
                "scope": "display_assortment_only",
                "preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
                "production_action": "none_read_only",
            },
        )
        summary["immutable_trajectory"] = {
            "trajectory_hash": trajectory_result.key,
            "content_sha256": trajectory_result.content_sha256,
            "row_count": trajectory_result.row_count,
            "reused": trajectory_result.reused,
        }
    summary["policy_hash"] = policy_hash

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "summary.json", summary)
    _write_csv(output_dir / "daily-audit.csv", daily_rows)
    _write_csv(output_dir / "top-up-episodes.csv", episodes)
    _write_csv(output_dir / "sku-summary.csv", sku_rows)
    examples = [row for row in sku_rows if _decimal(row["maximum_top_up_review_qty"]) > ZERO][:25]
    _write_csv(output_dir / "examples.csv", examples)
    (output_dir / "FIRST-PARTY-IN-TRANSIT-AUDIT.md").write_text(
        _markdown(summary, examples), encoding="utf-8"
    )
    files = {
        path.name: _sha256(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "analysis-manifest.json"
    }
    analysis_manifest = {
        "schema": "display_first_party_in_transit_hidden_demand_audit_manifest.v1",
        "dataset_hash": dataset_hash,
        "policy_hash": policy_hash,
        "model_version": MODEL_VERSION,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "files": files,
        "production_action": "none_read_only",
    }
    _write_json(output_dir / "analysis-manifest.json", analysis_manifest)
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--store-path", type=Path, default=DEFAULT_REPLAY_STORE_PATH)
    parser.add_argument("--dataset-hash", required=True)
    parser.add_argument("--date-from", type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument("--date-to", type=date.fromisoformat, default=date(2026, 7, 31))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--no-store-trajectory", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    summary = run_audit(
        preflight_dir=args.preflight_dir,
        store_path=args.store_path,
        dataset_hash=args.dataset_hash,
        date_from=args.date_from,
        date_to=args.date_to,
        output_dir=args.output_dir,
        store_trajectory=not args.no_store_trajectory,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
