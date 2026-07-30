from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import customer_settlement_mapping as mapping
from app.services.customer_settlement_mapping import (
    CRM_CLUSTER_FIELD,
    CRM_COUNTERPARTIES_FIELD,
    CRM_SITE_USERS_FIELD,
    CrmClusterSourceRow,
    CustomerSettlementMappingSourceError,
    build_mapping_entries,
    fetch_crm_cluster_rows,
    parse_crm_cluster_row,
)

CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32


def _row(
    row_id: str,
    cluster: str | None,
    users: tuple[str, ...],
    counterparties: tuple[str, ...],
) -> CrmClusterSourceRow:
    return CrmClusterSourceRow(
        row_id=row_id,
        cluster_id=cluster,
        site_user_ids=users,
        counterparty_refs=counterparties,
        source_updated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )


def test_build_mapping_entries_marks_every_ambiguous_cluster_shape() -> None:
    entries = build_mapping_entries(
        [
            _row("1", "cluster-a", ("101", "102"), (CP_1,)),
            _row("2", "cluster-a", (), (CP_2,)),
            _row("3", "cluster-b", ("103",), (CP_1,)),
            _row("4", "cluster-c", ("103",), (CP_1,)),
            _row("5", None, ("104",), ()),
            _row("6", "cluster-d", ("105",), (CP_1,)),
        ]
    )
    by_user = {item.site_user_id: item for item in entries}

    assert by_user["101"].status == "ambiguous"
    assert by_user["102"].status == "ambiguous"
    assert by_user["103"].status == "ambiguous"
    assert by_user["104"].status == "not_linked"
    assert by_user["105"].status == "linked"
    assert by_user["105"].counterparty_ref == CP_1


def test_parse_crm_cluster_row_normalizes_multi_fields_and_timestamp() -> None:
    row = parse_crm_cluster_row(
        {
            "ID": "10",
            CRM_CLUSTER_FIELD: " cluster-a ",
            CRM_SITE_USERS_FIELD: [{"VALUE": "101"}, "101", "102"],
            CRM_COUNTERPARTIES_FIELD: [CP_1.upper().replace("0X", "0x")],
            mapping.CRM_UPDATED_AT_FIELD: "2026-07-29T12:00:00+03:00",
        }
    )

    assert row.cluster_id == "cluster-a"
    assert row.site_user_ids == ("101", "102")
    assert row.counterparty_refs == (CP_1,)
    assert row.source_updated_at == datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def test_fetch_crm_cluster_rows_checks_complete_pagination(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    pages = [
        {
            "result": [
                {
                    "ID": "1",
                    CRM_CLUSTER_FIELD: "cluster-a",
                    CRM_SITE_USERS_FIELD: ["101"],
                    CRM_COUNTERPARTIES_FIELD: [CP_1],
                }
            ],
            "total": 2,
            "next": 50,
        },
        {
            "result": [
                {
                    "ID": "2",
                    CRM_CLUSTER_FIELD: "cluster-b",
                    CRM_SITE_USERS_FIELD: ["102"],
                    CRM_COUNTERPARTIES_FIELD: [CP_2],
                }
            ],
        },
    ]

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        assert url == "https://example.test/rest/1/token/crm.contact.list.json"
        assert timeout_seconds == 2
        calls.append(payload)
        return pages[len(calls) - 1]

    monkeypatch.setattr(mapping, "_post_json", fake_post_json)
    rows = fetch_crm_cluster_rows(
        webhook_url="https://example.test/rest/1/token/",
        timeout_seconds=2,
    )

    assert [row.row_id for row in rows] == ["1", "2"]
    assert [call["start"] for call in calls] == [0, 50]
    assert calls[0]["filter"] == {f"!{CRM_SITE_USERS_FIELD}": False}


def test_fetch_crm_cluster_rows_rejects_incomplete_pages(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mapping,
        "_post_json",
        lambda *args, **kwargs: {"result": [], "total": 1},
    )
    with pytest.raises(
        CustomerSettlementMappingSourceError,
        match="crm_mapping_incomplete_pagination",
    ):
        fetch_crm_cluster_rows(webhook_url="https://example.test/rest/1/token", timeout_seconds=2)


def test_fetch_crm_cluster_rows_rejects_total_change_during_pagination(
    monkeypatch,
) -> None:
    pages = [
        {"result": [{"ID": "1"}], "total": 2, "next": 50},
        {"result": [{"ID": "2"}], "total": 3},
    ]
    call_count = 0

    def changing_total(*args, **kwargs):
        nonlocal call_count
        value = pages[call_count]
        call_count += 1
        return value

    monkeypatch.setattr(mapping, "_post_json", changing_total)
    with pytest.raises(
        CustomerSettlementMappingSourceError,
        match="crm_mapping_total_changed_during_read",
    ):
        fetch_crm_cluster_rows(webhook_url="https://example.test/rest/1/token", timeout_seconds=2)


def test_fetch_crm_cluster_rows_rejects_duplicate_rows(monkeypatch) -> None:
    pages = [
        {
            "result": [{"ID": "1"}],
            "total": 2,
            "next": 50,
        },
        {"result": [{"ID": "1"}]},
    ]
    call_count = 0

    def duplicate_page(*args, **kwargs):
        nonlocal call_count
        value = pages[call_count]
        call_count += 1
        return value

    monkeypatch.setattr(mapping, "_post_json", duplicate_page)
    with pytest.raises(
        CustomerSettlementMappingSourceError,
        match="crm_mapping_duplicate_or_missing_id",
    ):
        fetch_crm_cluster_rows(webhook_url="https://example.test/rest/1/token", timeout_seconds=2)
