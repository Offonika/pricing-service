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
    onec_counterparty_identity_hash,
    parse_crm_cluster_row,
    resolve_crm_counterparty_hashes,
)

CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32


class _HashResult:
    def __init__(self, refs: list[str]):
        self.refs = refs

    def mappings(self):
        return ({"counterparty_ref": value} for value in self.refs)


class _HashConnection:
    def __init__(self, refs: list[str]):
        self.refs = refs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, _statement):
        return _HashResult(self.refs)


class _HashEngine:
    def __init__(self, refs: list[str]):
        self.refs = refs

    def connect(self):
        return _HashConnection(self.refs)


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


def _raw_contact(row_id: int) -> dict[str, object]:
    return {
        "ID": str(row_id),
        CRM_CLUSTER_FIELD: f"cluster-{row_id}",
        CRM_SITE_USERS_FIELD: [str(1000 + row_id)],
        CRM_COUNTERPARTIES_FIELD: [CP_1],
    }


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


def test_invalid_crm_identifiers_make_cluster_ambiguous_without_aborting_sync() -> None:
    row = parse_crm_cluster_row(
        {
            "ID": "11",
            CRM_CLUSTER_FIELD: "cluster-invalid",
            CRM_SITE_USERS_FIELD: ["101", "not-a-user-id"],
            CRM_COUNTERPARTIES_FIELD: [CP_1, "РБ000001"],
        }
    )

    assert row.site_user_ids == ("101",)
    assert row.counterparty_refs == (CP_1,)
    assert row.has_invalid_site_user_id is True
    assert row.has_invalid_counterparty_ref is True
    entries = build_mapping_entries([row])
    assert len(entries) == 1
    assert entries[0].site_user_id == "101"
    assert entries[0].status == "ambiguous"
    assert entries[0].counterparty_ref is None


def test_hashed_onec_counterparty_id_resolves_to_exact_raw_ref() -> None:
    value_hash = onec_counterparty_identity_hash(CP_1)
    assert value_hash == "6804f2dd1a94b348d7387477"
    row = parse_crm_cluster_row(
        {
            "ID": "12",
            CRM_CLUSTER_FIELD: "cluster-hashed",
            CRM_SITE_USERS_FIELD: ["102"],
            CRM_COUNTERPARTIES_FIELD: [value_hash],
        }
    )

    assert row.counterparty_refs == ()
    assert row.counterparty_hashes == (value_hash,)
    assert row.has_invalid_counterparty_ref is False
    resolved = resolve_crm_counterparty_hashes(
        [row],
        onec_engine=_HashEngine([CP_1]),
    )
    assert resolved[0].counterparty_refs == (CP_1,)
    entries = build_mapping_entries(resolved)
    assert len(entries) == 1
    assert entries[0].status == "linked"
    assert entries[0].counterparty_ref == CP_1


def test_unresolved_onec_counterparty_hash_is_ambiguous() -> None:
    value_hash = onec_counterparty_identity_hash(CP_2)
    row = parse_crm_cluster_row(
        {
            "ID": "13",
            CRM_CLUSTER_FIELD: "cluster-unresolved",
            CRM_SITE_USERS_FIELD: ["103"],
            CRM_COUNTERPARTIES_FIELD: [value_hash],
        }
    )
    resolved = resolve_crm_counterparty_hashes(
        [row],
        onec_engine=_HashEngine([]),
    )
    assert resolved[0].has_invalid_counterparty_ref is True
    assert build_mapping_entries(resolved)[0].status == "ambiguous"


def test_fetch_crm_cluster_rows_checks_complete_pagination(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    responses = [
        {
            "result": [_raw_contact(row_id) for row_id in range(1, 51)],
            "total": 51,
            "next": 50,
        },
        {
            "result": {
                "result": {
                    "page_1": [
                        {
                            "ID": "51",
                            CRM_CLUSTER_FIELD: "cluster-b",
                            CRM_SITE_USERS_FIELD: ["102"],
                            CRM_COUNTERPARTIES_FIELD: [CP_2],
                        }
                    ]
                },
                "result_error": {},
            }
        },
        {
            "result": [_raw_contact(row_id) for row_id in range(1, 51)],
            "total": 51,
            "next": 50,
        },
    ]

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        assert timeout_seconds == 2
        calls.append((url, payload))
        return responses[len(calls) - 1]

    monkeypatch.setattr(mapping, "_post_json", fake_post_json)
    rows = fetch_crm_cluster_rows(
        webhook_url="https://example.test/rest/1/token/",
        timeout_seconds=2,
    )

    assert len(rows) == 51
    assert rows[0].row_id == "1"
    assert rows[-1].row_id == "51"
    assert calls[0][0] == "https://example.test/rest/1/token/crm.contact.list.json"
    assert calls[0][1]["start"] == 0
    assert calls[0][1]["filter"] == {f"!{CRM_SITE_USERS_FIELD}": False}
    assert calls[1][0] == "https://example.test/rest/1/token/batch.json"
    assert set(calls[1][1]["cmd"]) == {"page_1"}
    assert "crm.contact.list?" in calls[1][1]["cmd"]["page_1"]
    assert "start=-1" in calls[1][1]["cmd"]["page_1"]
    assert calls[2][0] == "https://example.test/rest/1/token/crm.contact.list.json"


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
    responses = [
        {
            "result": [_raw_contact(row_id) for row_id in range(1, 51)],
            "total": 51,
            "next": 50,
        },
        {
            "result": {
                "result": {"page_1": [_raw_contact(51)]},
                "result_error": {},
            }
        },
        {
            "result": [_raw_contact(row_id) for row_id in range(1, 51)],
            "total": 52,
            "next": 50,
        },
    ]
    call_count = 0

    def changing_total(*args, **kwargs):
        nonlocal call_count
        value = responses[call_count]
        call_count += 1
        return value

    monkeypatch.setattr(mapping, "_post_json", changing_total)
    with pytest.raises(
        CustomerSettlementMappingSourceError,
        match="crm_mapping_total_changed_during_read",
    ):
        fetch_crm_cluster_rows(webhook_url="https://example.test/rest/1/token", timeout_seconds=2)


def test_fetch_crm_cluster_rows_rejects_duplicate_rows(monkeypatch) -> None:
    responses = [
        {
            "result": [_raw_contact(row_id) for row_id in range(1, 51)],
            "total": 51,
            "next": 50,
        },
        {
            "result": {
                "result": {"page_1": [{"ID": "1"}]},
                "result_error": {},
            }
        },
    ]
    call_count = 0

    def duplicate_page(*args, **kwargs):
        nonlocal call_count
        value = responses[call_count]
        call_count += 1
        return value

    monkeypatch.setattr(mapping, "_post_json", duplicate_page)
    with pytest.raises(
        CustomerSettlementMappingSourceError,
        match="crm_mapping_duplicate_or_missing_id",
    ):
        fetch_crm_cluster_rows(webhook_url="https://example.test/rest/1/token", timeout_seconds=2)
