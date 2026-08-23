from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import scripts.ensure_site_service_requests_bitrix_process as bitrix_setup
from app.core.config import Settings


def _settings(*, writes_enabled: bool = False) -> Settings:
    return Settings(
        site_service_requests_bitrix_writes_enabled=writes_enabled,
        site_service_requests_bitrix_entity_type_id=1134,
        site_service_requests_bitrix_working_category_id=55,
    )


def _field(spec: dict[str, Any], index: int) -> dict[str, Any]:
    enum = [
        {
            "id": str(index * 100 + enum_index),
            "value": title,
            "xmlId": f"MM_SITE_{spec['key'].upper()}_{key.upper()}",
        }
        for enum_index, (key, title) in enumerate(spec.get("enum") or (), start=1)
    ]
    return {
        "id": str(index),
        "entityId": "CRM_36",
        "fieldName": bitrix_setup._field_name("CRM_36", spec["key"]),
        "userTypeId": spec["type"],
        "xmlId": bitrix_setup._xml_id(spec["key"]),
        "enum": enum,
    }


def _all_fields() -> list[dict[str, Any]]:
    fields = [_field(spec, index) for index, spec in enumerate(bitrix_setup.FIELD_SPECS, start=1)]
    fields.append(_request_type_field())
    return fields


def _request_type_field() -> dict[str, Any]:
    rows = [
        ("clarify", "Разобраться"),
        ("refund_money", "Вернуть деньги"),
        ("replacement", "Замена товара"),
        ("expertise", "Нужна экспертиза"),
        ("logistics_return", "Доставка / возврат"),
        ("other", "Другое"),
    ]
    return {
        "id": "99",
        "fieldName": bitrix_setup.REQUEST_TYPE_FIELD_NAME,
        "userTypeId": "enumeration",
        "enum": [
            {"id": str(9900 + index), "xmlId": key, "value": title}
            for index, (key, title) in enumerate(rows, start=1)
        ],
    }


def _stages() -> list[dict[str, Any]]:
    return [
        {"STATUS_ID": "DT1134_55:NEW", "SEMANTICS": None},
        {"STATUS_ID": "DT1134_55:PREPARATION", "SEMANTICS": None},
        {"STATUS_ID": "DT1134_55:SUCCESS", "SEMANTICS": "S"},
        {"STATUS_ID": "DT1134_55:FAIL", "SEMANTICS": "F"},
    ]


class FakeBitrixApi:
    def __init__(
        self,
        *,
        fields: list[dict[str, Any]],
        persist_added_fields: bool = True,
        return_saved_form: bool = True,
        stages: list[dict[str, Any]] | None = None,
        existing_form: list[dict[str, Any]] | None = None,
    ) -> None:
        self.fields = deepcopy(fields)
        self.persist_added_fields = persist_added_fields
        self.return_saved_form = return_saved_form
        self.stages = deepcopy(stages if stages is not None else _stages())
        default_form = [
            {
                "name": "main",
                "title": "Основное",
                "type": "section",
                "elements": [{"name": "TITLE", "optionFlags": 1}],
            }
        ]
        self.saved_form: list[dict[str, Any]] = deepcopy(
            default_form if existing_form is None else existing_form
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_json(
        self,
        method: str,
        payload: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((method, deepcopy(payload)))
        if method == "crm.type.get":
            return {"result": {"type": {"id": 36, "entityTypeId": 1134}}}
        if method == "userfieldconfig.list":
            return {"result": {"fields": deepcopy(self.fields)}}
        if method == "crm.status.list":
            return {"result": deepcopy(self.stages)}
        if method == "userfieldconfig.add":
            field = deepcopy(payload["field"])
            field["id"] = str(len(self.fields) + 1)
            for enum_index, item in enumerate(field.get("enum") or (), start=1):
                item["id"] = str(9000 + enum_index)
            if self.persist_added_fields:
                self.fields.append(field)
            return {"result": {"field": deepcopy(field)}}
        if method == "crm.item.details.configuration.set":
            self.saved_form = deepcopy(payload["data"])
            return {"result": True}
        if method == "crm.item.details.configuration.get":
            return {"result": deepcopy(self.saved_form) if self.return_saved_form else []}
        raise AssertionError(f"unexpected Bitrix method: {method}")


def _write_methods(api: FakeBitrixApi) -> list[str]:
    return [
        method
        for method, _payload in api.calls
        if method in {"userfieldconfig.add", "crm.item.details.configuration.set"}
    ]


def test_dry_run_reports_missing_fields_without_writes() -> None:
    api = FakeBitrixApi(fields=[])

    plan = bitrix_setup.ensure(api, settings=_settings(), apply=False)

    assert plan.missing_fields == tuple(spec["key"] for spec in bitrix_setup.FIELD_SPECS)
    assert plan.missing_stages == ()
    assert plan.stage_map == {
        "new": "DT1134_55:NEW",
        "success": "DT1134_55:SUCCESS",
        "failure": "DT1134_55:FAIL",
    }
    assert _write_methods(api) == []


def test_apply_creates_only_missing_field_and_reads_back_mapping() -> None:
    fields = _all_fields()
    missing_spec = bitrix_setup.FIELD_SPECS[5]
    fields = [
        field for field in fields if field.get("xmlId") != bitrix_setup._xml_id(missing_spec["key"])
    ]
    api = FakeBitrixApi(fields=fields)

    plan = bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    add_calls = [payload for method, payload in api.calls if method == "userfieldconfig.add"]
    assert len(add_calls) == 1
    assert add_calls[0]["field"]["xmlId"] == bitrix_setup._xml_id(missing_spec["key"])
    assert plan.missing_fields == ()
    assert plan.type_mismatches == ()
    assert plan.enum_map["reply_action_send"] == "9002"
    assert plan.enum_map["request_type_warranty"] == "9904"
    assert plan.enum_map["request_type_delivery_return"] == "9905"
    assert _write_methods(api) == [
        "userfieldconfig.add",
        "crm.item.details.configuration.set",
    ]


def test_apply_blocks_existing_field_type_mismatch_before_writes() -> None:
    fields = _all_fields()
    fields[0]["userTypeId"] = "datetime"
    api = FakeBitrixApi(fields=fields)

    with pytest.raises(RuntimeError, match="field_type_mismatch"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_requires_created_field_readback() -> None:
    api = FakeBitrixApi(
        fields=_all_fields()[1:],
        persist_added_fields=False,
    )

    with pytest.raises(RuntimeError, match="fields_readback_failed"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == ["userfieldconfig.add"]


def test_apply_fails_closed_when_existing_form_readback_is_empty() -> None:
    api = FakeBitrixApi(fields=_all_fields(), return_saved_form=False)

    with pytest.raises(RuntimeError, match="form_readback_unrecognized"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_fails_closed_when_existing_form_contains_unknown_rows() -> None:
    class MalformedFormApi(FakeBitrixApi):
        def call_json(self, method: str, payload: dict[str, Any], **kwargs: Any):
            if method == "crm.item.details.configuration.get":
                return {
                    "result": [
                        {
                            "name": "main",
                            "title": "Основное",
                            "type": "section",
                            "elements": [{"name": "TITLE", "optionFlags": 1}],
                        },
                        "unexpected-row",
                    ]
                }
            return super().call_json(method, payload, **kwargs)

    api = MalformedFormApi(fields=_all_fields())

    with pytest.raises(RuntimeError, match="form_readback_unrecognized"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_blocks_missing_required_stage_before_writes() -> None:
    api = FakeBitrixApi(fields=_all_fields(), stages=_stages()[:-1])

    with pytest.raises(RuntimeError, match="required_stage_missing"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_blocks_incomplete_existing_request_type_enum_before_writes() -> None:
    fields = _all_fields()
    fields[-1]["enum"] = fields[-1]["enum"][:-1]
    api = FakeBitrixApi(fields=fields)

    with pytest.raises(RuntimeError, match="request_type_enum_mapping_incomplete"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_blocks_incomplete_site_enum_before_writes() -> None:
    fields = _all_fields()
    sync_status = next(
        field
        for field in fields
        if field.get("xmlId") == bitrix_setup._xml_id("site_sync_status")
    )
    sync_status["enum"] = sync_status["enum"][:-1]
    api = FakeBitrixApi(fields=fields)

    with pytest.raises(RuntimeError, match="enum_mapping_incomplete"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": []},
        {"result": {"type": []}},
        {"result": {"type": {"id": 0, "entityTypeId": 1134}}},
        {"result": {"type": {"id": 36.5, "entityTypeId": 1134}}},
        {"result": {"type": {"id": 36, "entityTypeId": 1134.5}}},
        {"result": {"type": {"id": 36, "entityTypeId": 999}}},
    ],
)
def test_apply_rejects_malformed_process_type_before_writes(response: dict[str, Any]) -> None:
    class MalformedTypeApi(FakeBitrixApi):
        def call_json(self, method: str, payload: dict[str, Any], **kwargs: Any):
            if method == "crm.type.get":
                return response
            return super().call_json(method, payload, **kwargs)

    api = MalformedTypeApi(fields=_all_fields())

    with pytest.raises(RuntimeError, match="type_readback_unrecognized"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": None},
        {"result": {}},
        {"result": {"statuses": "not-a-list"}},
        {"result": ["unknown-row"]},
    ],
)
def test_apply_rejects_malformed_stages_before_writes(response: dict[str, Any]) -> None:
    class MalformedStagesApi(FakeBitrixApi):
        def call_json(self, method: str, payload: dict[str, Any], **kwargs: Any):
            if method == "crm.status.list":
                return response
            return super().call_json(method, payload, **kwargs)

    api = MalformedStagesApi(fields=_all_fields())

    with pytest.raises(RuntimeError, match="stages_readback_unrecognized"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_rejects_malformed_enum_rows_before_writes() -> None:
    fields = _all_fields()
    fields[3]["enum"] = ["unknown-row"]
    api = FakeBitrixApi(fields=fields)

    with pytest.raises(RuntimeError, match="enum_readback_unrecognized"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_rejects_duplicate_enum_ids_before_writes() -> None:
    fields = _all_fields()
    fields[3]["enum"][1]["id"] = fields[3]["enum"][0]["id"]
    api = FakeBitrixApi(fields=fields)

    with pytest.raises(RuntimeError, match="enum_readback_ambiguous"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_rejects_duplicate_stage_rows_before_writes() -> None:
    stages = _stages()
    stages.append(deepcopy(stages[0]))
    api = FakeBitrixApi(fields=_all_fields(), stages=stages)

    with pytest.raises(RuntimeError, match="stage_mapping_ambiguous"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_rejects_duplicate_target_field_before_writes() -> None:
    fields = _all_fields()
    fields.append(deepcopy(fields[0]))
    api = FakeBitrixApi(fields=fields)

    with pytest.raises(RuntimeError, match="field_readback_ambiguous"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_rejects_ambiguous_existing_form_before_writes() -> None:
    existing_form = [
        {
            "name": "main",
            "title": "Основное",
            "type": "section",
            "elements": [
                {"name": "TITLE", "optionFlags": 1},
                {"name": "TITLE", "optionFlags": 1},
            ],
        }
    ]
    api = FakeBitrixApi(fields=_all_fields(), existing_form=existing_form)

    with pytest.raises(RuntimeError, match="form_readback_ambiguous"):
        bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert _write_methods(api) == []


def test_apply_merges_site_sections_without_replacing_existing_form() -> None:
    existing_form = [
        {
            "name": "main",
            "title": "Основное",
            "type": "section",
            "elements": [
                {"name": "TITLE", "optionFlags": 1},
                {"name": "UF_CRM_36_EXISTING", "optionFlags": 1},
            ],
        }
    ]
    api = FakeBitrixApi(fields=_all_fields(), existing_form=existing_form)

    bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    assert api.saved_form[0] == existing_form[0]
    all_names = {
        element["name"] for section in api.saved_form for element in section.get("elements") or []
    }
    assert "UF_CRM_36_EXISTING" in all_names
    assert "TITLE" in all_names
    assert (
        sum(
            element["name"] == "TITLE"
            for section in api.saved_form
            for element in section.get("elements") or []
        )
        == 1
    )


def test_apply_refetches_and_merges_concurrent_form_change_before_set() -> None:
    class ConcurrentFormApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__(fields=_all_fields())
            self.form_reads = 0

        def call_json(self, method: str, payload: dict[str, Any], **kwargs: Any):
            if method == "crm.item.details.configuration.get":
                self.form_reads += 1
                if self.form_reads == 2:
                    self.saved_form[0]["elements"].append(
                        {"name": "UF_CRM_36_CONCURRENT", "optionFlags": 1}
                    )
            return super().call_json(method, payload, **kwargs)

    api = ConcurrentFormApi()
    bitrix_setup.ensure(api, settings=_settings(writes_enabled=True), apply=True)

    names = {
        element["name"] for section in api.saved_form for element in section.get("elements") or []
    }
    assert "UF_CRM_36_CONCURRENT" in names


def test_userfield_list_reads_all_pages() -> None:
    calls: list[dict[str, Any]] = []

    class PaginatedApi:
        def call_json(
            self,
            method: str,
            payload: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            assert method == "userfieldconfig.list"
            calls.append(deepcopy(payload))
            if payload.get("start") == 50:
                return {
                    "result": {
                        "fields": [
                            {
                                "id": "2",
                                "fieldName": "UF_CRM_36_SECOND",
                                "userTypeId": "string",
                            }
                        ]
                    }
                }
            return {
                "result": {
                    "fields": [
                        {
                            "id": "1",
                            "fieldName": "UF_CRM_36_FIRST",
                            "userTypeId": "string",
                        }
                    ]
                },
                "next": 50,
            }

    fields = bitrix_setup._list_fields(PaginatedApi(), entity_id="CRM_36")

    assert [field["id"] for field in fields] == ["1", "2"]
    assert calls == [
        {"moduleId": "crm", "filter": {"entityId": "CRM_36"}},
        {"moduleId": "crm", "filter": {"entityId": "CRM_36"}, "start": 50},
    ]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": []},
        {"result": {"fields": "not-a-list"}},
        {"result": {"fields": ["unknown-row"]}},
        {
            "result": {
                "fields": [{"id": "1", "fieldName": "", "userTypeId": "string"}]
            }
        },
        {
            "result": {
                "fields": [
                    {
                        "id": "invalid",
                        "fieldName": "UF_CRM_1_VALUE",
                        "userTypeId": "string",
                    }
                ]
            }
        },
        {
            "result": {
                "fields": [
                    {
                        "id": 1.5,
                        "fieldName": "UF_CRM_1_VALUE",
                        "userTypeId": "string",
                    }
                ]
            }
        },
        {
            "result": {
                "fields": [
                    {
                        "id": "1",
                        "fieldName": ["UF_CRM_1_VALUE"],
                        "userTypeId": "string",
                    }
                ]
            }
        },
        {
            "result": {
                "fields": [
                    {
                        "id": "1",
                        "fieldName": "UF_CRM_1_VALUE",
                        "userTypeId": {"type": "string"},
                    }
                ]
            }
        },
    ],
)
def test_userfield_list_rejects_malformed_page(response: dict[str, Any]) -> None:
    class MalformedApi:
        def call_json(self, method: str, payload: dict[str, Any], **_kwargs: Any):
            assert method == "userfieldconfig.list"
            return response

    with pytest.raises(RuntimeError, match="fields_readback_unrecognized"):
        bitrix_setup._list_fields(MalformedApi(), entity_id="CRM_36")


@pytest.mark.parametrize("next_value", [False, -1, 1.5, "1.5", " 1", "invalid"])
def test_userfield_list_rejects_invalid_next_offset(next_value: object) -> None:
    class InvalidPaginationApi:
        def call_json(self, method: str, payload: dict[str, Any], **_kwargs: Any):
            assert method == "userfieldconfig.list"
            return {"result": {"fields": []}, "next": next_value}

    with pytest.raises(RuntimeError, match="fields_pagination_invalid"):
        bitrix_setup._list_fields(InvalidPaginationApi(), entity_id="CRM_36")


@pytest.mark.parametrize("next_value", [False, -1, 1.5, "1.5", " 1", "invalid"])
def test_stage_list_rejects_invalid_next_offset(next_value: object) -> None:
    class InvalidPaginationApi:
        def call_json(self, method: str, payload: dict[str, Any], **_kwargs: Any):
            assert method == "crm.status.list"
            return {"result": [], "next": next_value}

    with pytest.raises(RuntimeError, match="stages_pagination_invalid"):
        bitrix_setup._list_stages(
            InvalidPaginationApi(),
            entity_type_id=1134,
            category_id=55,
        )


def test_userfield_list_rejects_repeated_offset() -> None:
    class RepeatedPaginationApi:
        def call_json(self, method: str, payload: dict[str, Any], **_kwargs: Any):
            assert method == "userfieldconfig.list"
            return {"result": {"fields": [], "next": 50}}

    with pytest.raises(RuntimeError, match="fields_pagination_loop"):
        bitrix_setup._list_fields(RepeatedPaginationApi(), entity_id="CRM_36")


def test_userfield_list_stops_after_100_pages() -> None:
    calls = 0

    class EndlessPaginationApi:
        def call_json(self, method: str, payload: dict[str, Any], **_kwargs: Any):
            nonlocal calls
            assert method == "userfieldconfig.list"
            calls += 1
            return {"result": {"fields": [], "next": calls}}

    with pytest.raises(RuntimeError, match="fields_pagination_invalid"):
        bitrix_setup._list_fields(EndlessPaginationApi(), entity_id="CRM_36")
    assert calls == 100
