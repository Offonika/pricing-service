from __future__ import annotations

from scripts.ensure_site_order_deal_stages import build_plan


def test_build_plan_adds_missing_order_fulfillment_stages() -> None:
    plan = build_plan(
        [
            {"STATUS_ID": "IN_DELIVERY", "NAME": "Передан в доставку", "SORT": "60"},
            {"STATUS_ID": "WON", "NAME": "Сделка успешна", "SORT": "90"},
        ]
    )

    assert [item["action"] for item in plan] == ["add", "add", "add"]
    assert [item["stage"]["STATUS_ID"] for item in plan] == [
        "PICKUP_WAITING",
        "PICKUP_STORAGE",
        "DISMANTLING",
    ]


def test_build_plan_requires_manual_review_for_existing_stage_mismatch() -> None:
    plan = build_plan(
        [
            {
                "STATUS_ID": "PICKUP_WAITING",
                "NAME": "Ожидает клиента",
                "SORT": "65",
            },
            {
                "STATUS_ID": "PICKUP_STORAGE",
                "NAME": "Хранение в ПВЗ / отделении",
                "SORT": "70",
            },
            {
                "STATUS_ID": "DISMANTLING",
                "NAME": "Расформирование / отмена",
                "SORT": "80",
            },
        ]
    )

    assert len(plan) == 1
    assert plan[0]["action"] == "manual_review"
    assert plan[0]["stage"]["STATUS_ID"] == "PICKUP_WAITING"
    assert plan[0]["mismatches"] == {
        "NAME": {
            "current": "Ожидает клиента",
            "required": "Ожидает самовывоза",
        }
    }
