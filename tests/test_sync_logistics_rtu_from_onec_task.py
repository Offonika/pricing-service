from datetime import date

from tasks import sync_logistics_rtu_from_onec as task


def test_parse_args_supports_targeted_filters_and_page_size() -> None:
    args = task.parse_args(
        [
            "--date-from",
            "2026-08-14",
            "--limit",
            "250",
            "--site-order-number",
            "241666",
            "--rtu-external-id",
            "0xb4fc002590803daf11f19eca3ecfe591",
            "--apply",
        ]
    )

    assert args.date_from == date(2026, 8, 14)
    assert args.limit == 250
    assert args.site_order_number == "241666"
    assert args.rtu_external_id == "0xb4fc002590803daf11f19eca3ecfe591"
    assert args.apply is True


def test_sync_all_pages_processes_rows_after_first_page(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_sync(_session, _onec_engine, **kwargs):
        calls.append(kwargs)
        page_rows = {0: 2, 2: 2, 4: 1}[kwargs["offset"]]
        return {
            "dry_run": kwargs["dry_run"],
            "fetched": page_rows,
            "ready": page_rows,
            "synced_planned": page_rows,
            "synced_created": 0,
            "synced_updated": 0,
            "manual_review_resolved": 0,
            "skipped": 0,
            "manual_review_created": 0,
            "manual_review_planned": 0,
            "pending_readiness": 0,
            "ignored_non_site": 0,
            "warehouses_created": 0,
            "warehouses_planned": 0,
            "external_carrier_planned": 0,
            "external_carrier_handoff_created": 0,
            "external_carrier_handoff_existing": 0,
            "external_carrier_state_conflicts": 0,
            "by_reason": {"ready": page_rows},
        }

    monkeypatch.setattr(task, "sync_ready_rtu_units", fake_sync)

    result = task.sync_all_pages(
        object(),
        object(),
        date_from=date(2026, 8, 14),
        page_size=2,
        dry_run=True,
        external_carrier_flow=False,
        site_order_number="241666",
        rtu_external_id="0xb4fc002590803daf11f19eca3ecfe591",
    )

    assert [call["offset"] for call in calls] == [0, 2, 4]
    assert all(call["limit"] == 2 for call in calls)
    assert all(call["site_order_number"] == "241666" for call in calls)
    assert result["pages"] == 3
    assert result["fetched"] == 5
    assert result["synced_planned"] == 5
    assert result["by_reason"] == {"ready": 5}
    assert result["page_size"] == 2
