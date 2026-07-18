"""Поведенческие тесты контура «Типы цен»: правила, а не структура.

Проверяют границы каждого уровня лестницы, глобальную заморозку повышений,
обязательность ключевых полей blueprint и обработку нулевого знаменателя в
витрине возвратов — ровно те риски, которые пропускали структурные тесты
(вывод независимой ревизии 2026-07-18).
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.build_bronze_monthly_inventory import classify_client  # noqa: E402
from scripts.build_customer_returns_portrait import (  # noqa: E402
    _behavior_group,
    _period_mismatch,
)

RULESET = yaml.safe_load(
    (REPO_ROOT / "config/price_types/ruleset.yaml").read_text(encoding="utf-8")
)
LEVELS = RULESET["levels"]
BLUEPRINT = json.loads(
    (REPO_ROOT / "build/bitrix/customer_price_type_blueprint.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("level_key", list(LEVELS.keys()))
def test_level_boundaries_from_ruleset(level_key: str) -> None:
    """Границы уровня: норматив включительно, рубль ниже - уже не норма."""
    norm = Decimal(str(LEVELS[level_key]["retention_norm_3m"]))
    hold = Decimal(str(LEVELS[level_key]["hold_last_month"]))

    assert classify_client(norm, hold, retention_norm=norm, hold_threshold=hold) == "норма"
    assert (
        classify_client(norm - 1, hold, retention_norm=norm, hold_threshold=hold)
        == "удержание_дожим"
    )
    assert (
        classify_client(norm - 1, hold - 1, retention_norm=norm, hold_threshold=hold)
        == "изолятор_1м"
    )
    assert (
        classify_client(Decimal("0"), Decimal("0"), retention_norm=norm, hold_threshold=hold)
        == "изолятор_1м"
    )


def test_silver_client_not_measured_by_bronze_threshold() -> None:
    """Регресс на находку ревизии: серебро с последним месяцем 100к - это
    изолятор (порог 110к), а не удержание по бронзовым 3 300."""
    silver = LEVELS["silver"]
    bucket = classify_client(
        Decimal("200000"),
        Decimal("100000"),
        retention_norm=Decimal(str(silver["retention_norm_3m"])),
        hold_threshold=Decimal(str(silver["hold_last_month"])),
    )
    assert bucket == "изолятор_1м"


def test_blueprint_rulebook_matches_ruleset() -> None:
    rulebook = BLUEPRINT["rulebook"]
    assert rulebook["ruleset_version"] == RULESET["ruleset_version"]
    for key, level in LEVELS.items():
        assert rulebook["levels"][key]["retention_norm_3m"] == level["retention_norm_3m"]
        assert rulebook["levels"][key]["hold_last_month"] == level["hold_last_month"]


def test_blueprint_upgrade_freeze_present() -> None:
    freeze = [s for s in BLUEPRINT["stop_factors"] if s["key"] == "upgrade_freeze"]
    assert freeze and freeze[0]["blocks"] == "upgrade"
    assert RULESET["upgrades"]["frozen"] is True


def test_blueprint_required_fields() -> None:
    required = {f["logical_key"] for f in BLUEPRINT["fields"] if f.get("required")}
    for key in (
        "stable_key",
        "counterparty_ref",
        "counterparty_code",
        "current_price_type",
        "snapshot_date",
        "approval_status",
        "onec_export_status",
    ):
        assert key in required, f"поле {key} должно быть обязательным"
    stable = next(f for f in BLUEPRINT["fields"] if f["logical_key"] == "stable_key")
    assert stable.get("formula"), "stable_key должен иметь формулу"


def test_blueprint_transitions_have_no_bare_bronze_threshold() -> None:
    for rule in BLUEPRINT["transition_rules"]:
        if "3300" in rule["when"]:
            assert "ruleset" in rule["when"], (
                "порог бронзы в переходе без ссылки на таблицу уровней: "
                f"{rule['from']} -> {rule['to']}"
            )


def test_returns_zero_denominator_is_review_not_healthy() -> None:
    """Регресс: возвраты без продаж окна - сверка периодов, не здоровье."""
    mismatch = _period_mismatch(Decimal("0"), Decimal("5000"))
    assert mismatch
    group = _behavior_group(Decimal("0"), Decimal("0"), True, mismatch)
    assert group == "needs_review_period_mismatch"


def test_returns_over_window_sales_flagged() -> None:
    mismatch = _period_mismatch(Decimal("1000"), Decimal("2650"))
    assert "больше продаж" in mismatch
    assert _period_mismatch(Decimal("10000"), Decimal("1500")) == ""


def test_linter_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/validate_price_type_ruleset.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
