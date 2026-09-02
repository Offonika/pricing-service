from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

import pytest

from app.services import procurement_supplier_crm as sync
from scripts.import_onec_supplier_order_to_procurement import (
    bitrix_values_match,
    existing_procurement_item_id,
    import_order,
    source_number_lookup_candidates,
)


def _multi(*values: str) -> list[dict[str, str]]:
    return [{"VALUE": value, "VALUE_TYPE": "WORK"} for value in values]


class FakeBitrixApi:
    def __init__(
        self,
        *,
        companies: dict[str, dict[str, Any]] | None = None,
        contacts: dict[str, dict[str, Any]] | None = None,
        items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.companies = companies or {}
        self.contacts = contacts or {}
        self.items = items or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_company_id = 1000
        self.next_contact_id = 2000
        self.next_item_id = 3000
        self.next_task_id = 4000

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.calls.append((method, deepcopy(params)))
        if method == "crm.company.list":
            return {"result": self._list(self.companies, params.get("filter") or {})}
        if method == "crm.contact.list":
            return {"result": self._list(self.contacts, params.get("filter") or {})}
        if method == "crm.company.get":
            return {"result": deepcopy(self.companies.get(str(params["id"]), {}))}
        if method == "crm.contact.get":
            return {"result": deepcopy(self.contacts.get(str(params["id"]), {}))}
        if method == "crm.company.add":
            company_id = str(self.next_company_id)
            self.next_company_id += 1
            self.companies[company_id] = {"ID": company_id, **deepcopy(params["fields"])}
            return {"result": company_id}
        if method == "crm.company.update":
            self.companies[str(params["id"])].update(deepcopy(params["fields"]))
            return {"result": True}
        if method == "crm.contact.add":
            contact_id = str(self.next_contact_id)
            self.next_contact_id += 1
            self.contacts[contact_id] = {"ID": contact_id, **deepcopy(params["fields"])}
            return {"result": contact_id}
        if method == "crm.contact.update":
            self.contacts[str(params["id"])].update(deepcopy(params["fields"]))
            return {"result": True}
        if method == "crm.item.list":
            return {"result": {"items": self._list(self.items, params.get("filter") or {})}}
        if method == "crm.item.get":
            return {"result": {"item": deepcopy(self.items.get(str(params["id"]), {}))}}
        if method == "crm.item.add":
            item_id = str(self.next_item_id)
            self.next_item_id += 1
            self.items[item_id] = {"id": item_id, **deepcopy(params["fields"])}
            return {"result": {"item": deepcopy(self.items[item_id])}}
        if method == "crm.item.update":
            self.items[str(params["id"])].update(deepcopy(params["fields"]))
            return {"result": {"item": deepcopy(self.items[str(params["id"])])}}
        if method == "tasks.task.add":
            task_id = str(self.next_task_id)
            self.next_task_id += 1
            return {"result": {"task": {"id": task_id}}}
        if method == "crm.duplicate.findbycomm":
            return {"result": self._duplicates(params)}
        raise AssertionError(method)

    def _list(
        self, rows: dict[str, dict[str, Any]], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows.values():
            if self._matches(row, filters):
                result.append(deepcopy(row))
        return result

    def _matches(self, row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for raw_key, value in filters.items():
            key = str(raw_key).lstrip("=")
            if key == "TITLE":
                if sync.normalize_name(row.get("TITLE")) != sync.normalize_name(value):
                    return False
                continue
            if str(row.get(key) or "") != str(value):
                return False
        return True

    def _duplicates(self, params: dict[str, Any]) -> dict[str, list[str]]:
        comm_type = params.get("type")
        values = {str(item).casefold() for item in params.get("values") or []}
        result = {"COMPANY": [], "CONTACT": []}
        for company_id, row in self.companies.items():
            if self._has_comm(row, comm_type, values):
                result["COMPANY"].append(company_id)
        for contact_id, row in self.contacts.items():
            if self._has_comm(row, comm_type, values):
                result["CONTACT"].append(contact_id)
        return result

    @staticmethod
    def _has_comm(row: dict[str, Any], comm_type: str, values: set[str]) -> bool:
        for item in row.get(comm_type) or []:
            value = str(item.get("VALUE") or "").casefold()
            if value in values:
                return True
        return False


def test_sync_supplier_updates_existing_company_empty_fields_only() -> None:
    ref_field = sync.DEFAULT_CRM_FIELD_MAP["company"]["mm_onec_supplier_ref"]
    city_field = sync.DEFAULT_CRM_FIELD_MAP["company"]["mm_supplier_city"]
    api = FakeBitrixApi(
        companies={
            "10": {
                "ID": "10",
                "TITLE": "2312 Huagior battery",
                "ORIGINATOR_ID": sync.CRM_SUPPLIER_ORIGINATOR_ID,
                "ORIGIN_ID": "onec-2312",
                ref_field: "onec-2312",
                city_field: "",
                "PHONE": _multi("+86 111"),
            }
        }
    )

    result = sync.sync_supplier_to_crm(
        api,
        {
            "onec_ref": "onec-2312",
            "title": "2312 Huagior battery",
            "city": "Dongguan",
            "phone": "+86 222",
        },
        apply=False,
    )

    assert result["resolution_status_key"] == "resolved_existing"
    assert result["company_id"] == "10"
    assert city_field in result["updated_field_names"]
    assert "PHONE" in result["conflict_fields"]
    assert not any(method == "crm.company.update" for method, _params in api.calls)


def test_sync_supplier_creates_company_when_no_safe_match_apply() -> None:
    api = FakeBitrixApi()

    result = sync.sync_supplier_to_crm(
        api,
        {
            "onec_ref": "onec-new",
            "onec_code": "00042",
            "title": "New Foreign Supplier",
            "email": "supplier@example.test",
            "contacts": [{"name": "Julia Manager", "email": "julia@example.test"}],
        },
        apply=True,
        assigned_by_id=130750,
    )

    assert result["status"] == "created_company"
    assert result["company_id"] == "1000"
    assert api.companies["1000"]["ASSIGNED_BY_ID"] == "130750"
    assert api.contacts["2000"]["COMPANY_ID"] == "1000"
    assert any(method == "crm.company.add" for method, _params in api.calls)
    assert any(method == "crm.contact.add" for method, _params in api.calls)


def test_sync_supplier_blocks_multiple_title_matches() -> None:
    api = FakeBitrixApi(
        companies={
            "10": {"ID": "10", "TITLE": "Same Supplier"},
            "11": {"ID": "11", "TITLE": "same supplier"},
        }
    )

    result = sync.sync_supplier_to_crm(api, {"title": "Same Supplier"}, apply=True)

    assert result["resolution_status_key"] == "blocked_duplicate"
    assert result["company_id"] == ""
    assert "multiple_company_matches" in result["blocker_comment"]
    assert not any(method == "crm.company.add" for method, _params in api.calls)


def test_sync_supplier_reuses_contact_by_email_without_duplicate() -> None:
    api = FakeBitrixApi(
        companies={"10": {"ID": "10", "TITLE": "Supplier"}},
        contacts={"20": {"ID": "20", "NAME": "Julia", "EMAIL": _multi("julia@example.test")}},
    )

    result = sync.sync_supplier_to_crm(
        api,
        {
            "title": "Supplier",
            "contacts": [{"name": "Julia Manager", "email": "julia@example.test"}],
        },
        apply=True,
    )

    assert result["company_id"] == "10"
    assert result["contacts"][0]["contact_id"] == "20"
    assert api.contacts["20"]["COMPANY_ID"] == "10"
    assert not any(method == "crm.contact.add" for method, _params in api.calls)


def _mapping() -> dict[str, Any]:
    return {
        "category_map": {
            "ordinary": {"id": 52, "name": "Обычный"},
            "cargo": {"id": 53, "name": "Карго"},
            "ved_import": {"id": 12, "name": "ВЭД импорт"},
        },
        "stage_map": {
            "ordinary": {"supplier_order": "DT1056_52:NEW"},
            "cargo": {
                "supplier_order": "DT1056_53:NEW",
                "ready_for_cargo": "DT1056_53:READY_CARGO",
                "cargo_dropoff": "DT1056_53:CARGO",
                "payment_work": "DT1056_53:PAYMENT_WORK",
            },
            "ved_import": {
                "supplier_order": "DT1056_12:NEW",
                "docs_collection": "DT1056_12:DOCS_COLLECTION",
                "receiving": "DT1056_12:RECEIVING",
            },
        },
        "field_map": {
            "procurement_contour": "UF_CONTOUR",
            "pilot_batch_id": "UF_BATCH",
            "supplier_company": "UF_SUPPLIER_COMPANY",
            "supplier_onec_ref": "UF_SUPPLIER_REF",
            "supplier_resolution_status": "UF_SUPPLIER_STATUS",
            "supplier_resolution_basis": "UF_SUPPLIER_BASIS",
            "supplier_conflicts": "UF_SUPPLIER_CONFLICTS",
            "blocker_comment": "UF_BLOCKER",
            "supplier_dispatch_date": "UF_SUPPLIER_DISPATCH",
            "cargo_dropoff_date": "UF_CARGO_DROPOFF",
            "expected_receipt_date": "UF_RECEIPT",
            "payment_task_id": "UF_PAYMENT_TASK_ID",
            "payment_task_status": "UF_PAYMENT_TASK_STATUS",
            "payment_request_created_at": "UF_PAYMENT_CREATED_AT",
        },
        "enum_map": {
            "procurement_contour": {"ordinary": "367", "cargo": "368", "ved_import": "369"},
            "payment_task_status": {
                "created": "701",
                "done": "702",
                "skipped": "703",
                "error": "704",
            },
        },
        "crm_supplier_sync_contract": {
            "procurement_supplier_status_enum": {
                "resolved_existing": "501",
                "created_from_onec": "502",
                "manual_review": "503",
                "blocked_duplicate": "504",
            }
        },
    }


def test_build_procurement_order_fields_links_contour_supplier_and_dates() -> None:
    payload = sync.build_procurement_order_bitrix_fields(
        {
            "КонтурЗакупки": "ВЭДИмпорт",
            "supplier": {"onec_ref": "onec-2312"},
            "Отправка постав.": datetime(2026, 6, 10, 10, 0),
            "Сдача в карго": "2026-06-12",
            "Поступление": "2026-06-20",
        },
        {
            "resolution_status_key": "resolved_existing",
            "resolution_basis": "onec_ref",
            "company_id": "22835",
        },
        mapping=_mapping(),
    )

    assert payload["logical_key"] == "ved_import"
    assert payload["fields"]["categoryId"] == 12
    assert payload["fields"]["stageId"] == "DT1056_12:RECEIVING"
    assert payload["fields"]["UF_CONTOUR"] == "369"
    assert payload["fields"]["UF_SUPPLIER_COMPANY"] == "22835"
    assert payload["fields"]["UF_SUPPLIER_STATUS"] == "501"
    assert payload["fields"]["UF_SUPPLIER_REF"] == "onec-2312"
    assert payload["fields"]["UF_SUPPLIER_DISPATCH"] == "2026-06-10T10:00:00"
    assert payload["fields"]["UF_CARGO_DROPOFF"] == "2026-06-12"
    assert payload["fields"]["UF_RECEIPT"] == "2026-06-20"


def test_build_procurement_order_fields_routes_blank_contour_by_currency() -> None:
    payload = sync.build_procurement_order_bitrix_fields(
        {"КонтурЗакупки": "", "currency": "RMB"},
        {"resolution_status_key": "resolved_existing", "company_id": "22835"},
        mapping=_mapping(),
    )

    assert payload["logical_key"] == "cargo"
    assert payload["fields"]["categoryId"] == 53
    assert payload["fields"]["UF_CONTOUR"] == "368"

    rub_payload = sync.build_procurement_order_bitrix_fields(
        {"КонтурЗакупки": "", "currency": "руб."},
        {"resolution_status_key": "resolved_existing", "company_id": "22835"},
        mapping={
            **_mapping(),
            "category_map": {**_mapping()["category_map"], "ordinary": {"id": 52}},
            "stage_map": {
                **_mapping()["stage_map"],
                "ordinary": {"supplier_order": "DT1056_52:NEW"},
            },
            "enum_map": {
                "procurement_contour": {"ordinary": "367", "cargo": "368", "ved_import": "369"}
            },
        },
    )

    assert rub_payload["logical_key"] == "ordinary"

    cargo_dropoff_payload = sync.build_procurement_order_bitrix_fields(
        {"КонтурЗакупки": "", "currency": "руб.", "Сдача в карго": "2026-06-12"},
        {"resolution_status_key": "resolved_existing", "company_id": "22835"},
        mapping={
            **_mapping(),
            "category_map": {**_mapping()["category_map"], "ordinary": {"id": 52}},
            "stage_map": {
                **_mapping()["stage_map"],
                "ordinary": {"supplier_order": "DT1056_52:NEW"},
            },
            "enum_map": {
                "procurement_contour": {"ordinary": "367", "cargo": "368", "ved_import": "369"}
            },
        },
    )

    assert cargo_dropoff_payload["logical_key"] == "cargo"
    assert cargo_dropoff_payload["fields"]["categoryId"] == 53

    explicit_ordinary_payload = sync.build_procurement_order_bitrix_fields(
        {"КонтурЗакупки": "Обычный", "currency": "RMB", "Сдача в карго": "2026-06-12"},
        {"resolution_status_key": "resolved_existing", "company_id": "22835"},
        mapping={
            **_mapping(),
            "category_map": {**_mapping()["category_map"], "ordinary": {"id": 52}},
            "stage_map": {
                **_mapping()["stage_map"],
                "ordinary": {"supplier_order": "DT1056_52:NEW"},
            },
            "enum_map": {
                "procurement_contour": {"ordinary": "367", "cargo": "368", "ved_import": "369"}
            },
        },
    )

    assert explicit_ordinary_payload["logical_key"] == "ordinary"


def test_build_procurement_order_fields_selects_cargo_stage_from_order_state() -> None:
    mapping = _mapping()
    mapping["stage_map"]["cargo"] = {
        "supplier_order": "DT1056_53:NEW",
        "payment_work": "DT1056_53:PAYMENT_WORK",
        "ready_for_cargo": "DT1056_53:READY_CARGO",
        "cargo_dropoff": "DT1056_53:CARGO",
    }
    supplier = {"resolution_status_key": "resolved_existing", "company_id": "22835"}

    assert (
        sync.build_procurement_order_bitrix_fields(
            {"КонтурЗакупки": "", "currency": "RMB", "posted": True},
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:NEW"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {
                "КонтурЗакупки": "",
                "currency": "RMB",
                "posted": "false",
                "is_posted": "true",
            },
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:NEW"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {"КонтурЗакупки": "", "currency": "RMB", "Оплата": "2026-06-09"},
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:PAYMENT_WORK"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {"КонтурЗакупки": "", "currency": "RMB", "supplier_dispatch_date": "2026-06-10"},
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:NEW"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {"КонтурЗакупки": "", "currency": "RMB", "Отправка постав.": "2026-06-10"},
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:NEW"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {
                "КонтурЗакупки": "",
                "currency": "RMB",
                "posted": True,
                "supplier_dispatch_date": "2026-06-10",
            },
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:NEW"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {"КонтурЗакупки": "", "currency": "RMB", "Сдача в карго": "2026-06-11"},
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:CARGO"
    )
    assert (
        sync.build_procurement_order_bitrix_fields(
            {
                "КонтурЗакупки": "",
                "currency": "RMB",
                "Сдача в карго": "2026-06-11",
                "payment_task_status": "Created",
            },
            supplier,
            mapping=mapping,
        )["fields"]["stageId"]
        == "DT1056_53:PAYMENT_WORK"
    )


def test_build_procurement_order_fields_can_block_supplier_conflict() -> None:
    with pytest.raises(sync.SupplierSyncError):
        sync.build_procurement_order_bitrix_fields(
            {"КонтурЗакупки": "Cargo"},
            {
                "resolution_status_key": "blocked_duplicate",
                "blocker_comment": "Найдено несколько CRM-компаний.",
            },
            mapping=_mapping(),
            on_supplier_conflict="block_import",
        )

    payload = sync.build_procurement_order_bitrix_fields(
        {"КонтурЗакупки": "Cargo"},
        {
            "resolution_status_key": "blocked_duplicate",
            "resolution_basis": "normalized_title",
            "blocker_comment": "Найдено несколько CRM-компаний.",
        },
        mapping=_mapping(),
        on_supplier_conflict="create_card_with_blocker",
    )

    assert payload["blocked_supplier"] is True
    assert payload["fields"]["categoryId"] == 53
    assert payload["fields"]["UF_SUPPLIER_STATUS"] == "504"
    assert payload["fields"]["UF_BLOCKER"] == "Найдено несколько CRM-компаний."


def test_import_order_dry_run_resolves_supplier_before_procurement_fields() -> None:
    ref_field = sync.DEFAULT_CRM_FIELD_MAP["company"]["mm_onec_supplier_ref"]
    api = FakeBitrixApi(
        companies={
            "10": {
                "ID": "10",
                "TITLE": "Supplier",
                "ORIGINATOR_ID": sync.CRM_SUPPLIER_ORIGINATOR_ID,
                "ORIGIN_ID": "supplier-ref",
                ref_field: "supplier-ref",
            }
        }
    )

    result = import_order(
        api,
        {
            "number": "РБГУ000001",
            "КонтурЗакупки": "Cargo",
            "supplier": {"onec_ref": "supplier-ref", "title": "Supplier"},
            "currency": "RMB",
            "amount": 1000,
        },
        mapping=_mapping(),
        apply=False,
        assigned_by_id="130750",
    )

    assert result["action"] == "dry_run_update_or_create"
    assert result["source_number"] == "РБГУ000001"
    assert result["contour"] == "cargo"
    assert result["supplier_company_id"] == "10"
    assert "UF_SUPPLIER_COMPANY" in result["field_names"]
    assert not any(method == "crm.item.add" for method, _params in api.calls)


def test_import_order_dry_run_generates_cargo_batch_and_payment_task_action() -> None:
    api = FakeBitrixApi()

    result = import_order(
        api,
        {
            "number": "РБГУ000001",
            "КонтурЗакупки": "Cargo",
            "supplier": {"title": "Supplier"},
            "currency": "RMB",
            "amount": 1000,
            "Сдача в карго": "2026-06-12",
        },
        mapping=_mapping(),
        apply=False,
        finance_user_id="130746",
    )

    assert result["batch_id"] == "CARGO-20260612-РБГУ000001"
    assert result["initial_stage_key"] == "cargo_dropoff"
    assert result["stage_key"] == "payment_work"
    assert result["payment_task_action"] == "dry_run_create"
    assert "UF_BATCH" in result["field_names"]
    assert "UF_PAYMENT_TASK_STATUS" in result["field_names"]
    assert not any(method == "tasks.task.add" for method, _params in api.calls)


def test_import_order_apply_creates_payment_task_once_for_cargo_dropoff() -> None:
    api = FakeBitrixApi()
    mapping = {**_mapping(), "process": {"entity_type_id": 1056}}

    result = import_order(
        api,
        {
            "number": "РБГУ000001",
            "КонтурЗакупки": "Cargo",
            "supplier": {"title": "Supplier"},
            "currency": "RMB",
            "amount": 1000,
            "Сдача в карго": "2026-06-12",
        },
        mapping=mapping,
        apply=True,
        finance_user_id="130746",
    )

    assert result["action"] == "created"
    assert result["initial_stage_key"] == "cargo_dropoff"
    assert result["stage_key"] == "payment_work"
    assert result["payment_task_action"] == "created"
    assert result["payment_task_id"] == "4000"
    assert result["batch_id"] == "CARGO-20260612-РБГУ000001"
    task_calls = [params for method, params in api.calls if method == "tasks.task.add"]
    assert len(task_calls) == 1
    assert task_calls[0]["fields"]["RESPONSIBLE_ID"] == "130746"
    assert (
        "Оплатить карго-заявку: РБГУ000001 / CARGO-20260612-РБГУ000001"
        in task_calls[0]["fields"]["TITLE"]
    )
    item = api.items[result["item_id"]]
    assert item["UF_PAYMENT_TASK_ID"] == "4000"
    assert item["UF_PAYMENT_TASK_STATUS"] == "701"
    assert item["stageId"] == "DT1056_53:PAYMENT_WORK"


def test_import_order_apply_reuses_existing_payment_task_without_duplicate() -> None:
    api = FakeBitrixApi(items={"42": {"id": "42", "UF_PAYMENT_TASK_ID": "555"}})
    mapping = {**_mapping(), "process": {"entity_type_id": 1056}}

    result = import_order(
        api,
        {
            "number": "РБГУ000001",
            "КонтурЗакупки": "Cargo",
            "supplier": {"title": "Supplier"},
            "currency": "RMB",
            "amount": 1000,
            "Сдача в карго": "2026-06-12",
        },
        mapping=mapping,
        apply=True,
        finance_user_id="130746",
        existing_item_id="42",
    )

    assert result["action"] == "updated"
    assert result["stage_key"] == "payment_work"
    assert result["payment_task_action"] == "exists"
    assert result["payment_task_id"] == "555"
    assert not any(method == "tasks.task.add" for method, _params in api.calls)
    update_calls = [params for method, params in api.calls if method == "crm.item.update"]
    assert len(update_calls) == 1
    assert update_calls[0]["fields"]["stageId"] == "DT1056_53:PAYMENT_WORK"


def test_import_order_apply_noops_when_existing_card_already_matches() -> None:
    mapping = {**_mapping(), "process": {"entity_type_id": 1056}}
    api = FakeBitrixApi(
        items={
            "42": {
                "id": "42",
                "categoryId": 53,
                "stageId": "DT1056_53:PAYMENT_WORK",
                "UF_CONTOUR": "368",
                "UF_BATCH": "CARGO-20260612-РБГУ000001",
                "UF_SUPPLIER_COMPANY": "22835",
                "UF_SUPPLIER_STATUS": "501",
                "UF_PAYMENT_TASK_STATUS": "702",
                "UF_CARGO_DROPOFF": "2026-06-12",
            }
        }
    )

    result = import_order(
        api,
        {
            "number": "РБГУ000001",
            "КонтурЗакупки": "",
            "supplier": {"title": "Supplier"},
            "currency": "RMB",
            "amount": 1000,
            "Сдача в карго": "2026-06-12",
            "Оплата": "2026-06-12",
        },
        mapping=mapping,
        apply=True,
        finance_user_id="130746",
        supplier_result={
            "status": "resolved_existing_company",
            "resolution_status_key": "resolved_existing",
            "company_id": "22835",
        },
        existing_item_id="42",
    )

    assert result["action"] == "noop"
    assert result["payment_task_action"] == "skipped_payment_done"
    assert not any(method == "crm.item.update" for method, _params in api.calls)


def test_bitrix_values_match_normalizes_bitrix_dates_and_numeric_values() -> None:
    assert bitrix_values_match("2026-06-03T00:00:00+03:00", "2026-06-03T00:00:00")
    assert bitrix_values_match("2026-07-04T12:46:12+03:00", "2026-07-04T09:46:12+00:00")
    assert bitrix_values_match("3740", 3740.0)
    assert not bitrix_values_match("0003740", 3740)


def test_import_order_generates_batch_suffix_for_duplicate_rows() -> None:
    api = FakeBitrixApi()
    used_batch_ids: set[str] = set()
    order = {
        "number": "РБГУ000001",
        "КонтурЗакупки": "Cargo",
        "supplier": {"title": "Supplier"},
        "currency": "RMB",
        "amount": 1000,
        "Сдача в карго": "2026-06-12",
    }

    first = import_order(
        api,
        order,
        mapping=_mapping(),
        apply=False,
        finance_user_id="130746",
        used_batch_ids=used_batch_ids,
    )
    second = import_order(
        api,
        order,
        mapping=_mapping(),
        apply=False,
        finance_user_id="130746",
        used_batch_ids=used_batch_ids,
    )

    assert first["batch_id"] == "CARGO-20260612-РБГУ000001"
    assert second["batch_id"] == "CARGO-20260612-РБГУ000001-02"


def test_existing_procurement_lookup_uses_crm_item_rest_field_names() -> None:
    api = FakeBitrixApi(
        items={
            "1": {
                "id": "1",
                "title": "ВЭД импорт АКБ РБГУ000001 от 03.01.2026",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
            }
        }
    )
    mapping = {
        "process": {"entity_type_id": 1056},
        "field_map": {"onec_source_number": "UF_CRM_8_ONECSOURCENUMBER"},
    }

    item_id = existing_procurement_item_id(api, {"number": "РБГУ000001"}, mapping)

    assert item_id == "1"
    method, params = api.calls[-1]
    assert method == "crm.item.list"
    assert params["filter"] == {"=ufCrm8Onecsourcenumber": "РБГУ000001"}
    assert params["select"] == ["id", "title", "ufCrm8Onecsourcenumber"]


def test_existing_procurement_lookup_falls_back_to_adjacent_number_width() -> None:
    api = FakeBitrixApi(
        items={
            "1": {
                "id": "1",
                "title": "ВЭД импорт АКБ РБГУ000001 от 03.01.2026",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
            }
        }
    )
    mapping = {
        "process": {"entity_type_id": 1056},
        "field_map": {"onec_source_number": "UF_CRM_8_ONECSOURCENUMBER"},
    }

    item_id = existing_procurement_item_id(api, {"number": "РБГУ0000001"}, mapping)

    assert item_id == "1"
    filters = [params["filter"] for method, params in api.calls if method == "crm.item.list"]
    assert filters == [
        {"=ufCrm8Onecsourcenumber": "РБГУ0000001"},
        {"=ufCrm8Onecsourcenumber": "РБГУ000001"},
        {"=ufCrm8Onecsourcenumber": "РБГУ00000001"},
    ]


def test_existing_procurement_lookup_uses_source_year_for_repeating_numbers() -> None:
    api = FakeBitrixApi(
        items={
            "1": {
                "id": "1",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
                "ufCrm8Onecsourcetype": "ЗаказПоставщику",
                "ufCrm8Onecsourcedate": "2025-01-03T11:37:49+03:00",
            },
            "2": {
                "id": "2",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
                "ufCrm8Onecsourcetype": "ЗаказПоставщику",
                "ufCrm8Onecsourcedate": "2026-06-09T00:09:25+03:00",
            },
        }
    )
    mapping = {
        "process": {"entity_type_id": 1056},
        "field_map": {
            "onec_source_number": "UF_CRM_8_ONECSOURCENUMBER",
            "onec_source_type": "UF_CRM_8_ONECSOURCETYPE",
            "onec_source_date": "UF_CRM_8_ONECSOURCEDATE",
        },
    }

    item_id = existing_procurement_item_id(
        api,
        {"number": "РБГУ0000001", "source_type": "ЗаказПоставщику", "date": "2026-06-09"},
        mapping,
    )

    assert item_id == "2"


def test_existing_procurement_lookup_stops_on_ambiguous_same_year_match() -> None:
    api = FakeBitrixApi(
        items={
            "1": {
                "id": "1",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
                "ufCrm8Onecsourcedate": "2026-01-03T11:37:49+03:00",
            },
            "2": {
                "id": "2",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
                "ufCrm8Onecsourcedate": "2026-06-09T00:09:25+03:00",
            },
        }
    )
    mapping = {
        "process": {"entity_type_id": 1056},
        "field_map": {
            "onec_source_number": "UF_CRM_8_ONECSOURCENUMBER",
            "onec_source_date": "UF_CRM_8_ONECSOURCEDATE",
        },
    }

    with pytest.raises(RuntimeError, match="Найдено несколько Bitrix-карточек"):
        existing_procurement_item_id(api, {"number": "РБГУ0000001", "date": "2026-06-09"}, mapping)


def test_source_number_lookup_candidates_changes_only_numeric_width() -> None:
    assert source_number_lookup_candidates("РБГУ0000001") == [
        "РБГУ0000001",
        "РБГУ000001",
        "РБГУ00000001",
    ]
    assert source_number_lookup_candidates("MANUAL") == ["MANUAL"]


def test_import_order_apply_updates_existing_crm_item_without_duplicate() -> None:
    ref_field = sync.DEFAULT_CRM_FIELD_MAP["company"]["mm_onec_supplier_ref"]
    api = FakeBitrixApi(
        companies={
            "10": {
                "ID": "10",
                "TITLE": "Supplier",
                "ORIGINATOR_ID": sync.CRM_SUPPLIER_ORIGINATOR_ID,
                "ORIGIN_ID": "supplier-ref",
                ref_field: "supplier-ref",
            }
        },
        items={
            "1": {
                "id": "1",
                "title": "ВЭД импорт АКБ РБГУ000001 от 03.01.2026",
                "ufCrm8Onecsourcenumber": "РБГУ000001",
            }
        },
    )
    mapping = _mapping()
    mapping["process"] = {"entity_type_id": 1056}
    mapping["field_map"]["title"] = "TITLE"
    mapping["field_map"]["procurement_contour"] = "UF_CRM_8_PROCUREMENTCONTOUR"
    mapping["field_map"]["onec_source_number"] = "UF_CRM_8_ONECSOURCENUMBER"

    result = import_order(
        api,
        {
            "number": "РБГУ000001",
            "КонтурЗакупки": "ВЭД импорт",
            "supplier": {"onec_ref": "supplier-ref", "title": "Supplier"},
            "amount": "1000",
            "currency": "RMB",
            "open_amount_rub": "11500",
        },
        mapping=mapping,
        apply=True,
        assigned_by_id="130750",
    )

    assert result["action"] == "updated"
    assert result["item_id"] == "1"
    assert not any(method == "crm.item.add" for method, _params in api.calls)
    update = next(params for method, params in api.calls if method == "crm.item.update")
    assert update["id"] == "1"
    assert update["fields"]["title"] == "ВЭД импорт · РБГУ000001 · 1 000 RMB · Supplier"
    assert update["fields"]["opportunity"] == "11500"
    assert update["fields"]["currencyId"] == "RUB"
    assert "ufCrm8Onecsourcenumber" not in update["fields"]
    assert update["fields"]["ufCrm8Procurementcontour"] == "369"
    assert "assignedById" not in update["fields"]
