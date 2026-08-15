from __future__ import annotations

import io
import json
import os
import tempfile
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Base, ExpertiseCase, ExpertiseCaseEvent
from app.services import expertise_bitrix


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="expertise_bitrix_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://bitrix.example/rest/1/token/crm.item.update.json",
        code,
        "temporary failure",
        {},
        io.BytesIO(b'{"error":"temporary"}'),
    )


def test_bitrix_update_retries_transient_http_errors(monkeypatch) -> None:
    calls = 0
    sleeps: list[int] = []

    def fake_urlopen(request, timeout=60):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _http_error(500)
        return _FakeHTTPResponse({"result": {"item": {"id": 42}}})

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(expertise_bitrix.time_module, "sleep", sleeps.append)

    client = expertise_bitrix.BitrixRestClient("https://bitrix.example/rest/1/token")
    client.update_smart_process_item(
        entity_type_id=187,
        item_id="42",
        fields={"title": "Debt card"},
    )

    assert calls == 3
    assert sleeps == [1, 2]


def test_bitrix_add_does_not_retry_http_500(monkeypatch) -> None:
    calls = 0

    def fake_urlopen(request, timeout=60):
        nonlocal calls
        calls += 1
        raise _http_error(500)

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    client = expertise_bitrix.BitrixRestClient("https://bitrix.example/rest/1/token")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.add_smart_process_item(
            entity_type_id=187,
            fields={"title": "Debt card"},
        )

    assert calls == 1


def _configure_bitrix_env(
    monkeypatch: pytest.MonkeyPatch, *, include_store_map: bool = True
) -> None:
    monkeypatch.setenv("EXPERTISE_BITRIX_WEBHOOK_URL", "https://bitrix.example/rest/1/token")
    monkeypatch.setenv("EXPERTISE_BITRIX_ENTITY_TYPE_ID", "187")
    monkeypatch.setenv(
        "EXPERTISE_BITRIX_STAGE_MAP",
        json.dumps(
            {
                "created": "DT187_1:CREATED",
                "received_by_okk": "DT187_1:PREPARATION",
                "under_review": "DT187_1:PREPARATION",
                "decision_ready": "DT187_1:DECISION",
                "client_notified": "DT187_1:NOTIFIED",
                "returned_to_central_defect": "DT187_1:SUCCESS",
                "returned_to_store": "DT187_1:FAIL",
                "manual_review": "DT187_1:MANUAL",
            }
        ),
    )
    monkeypatch.setenv(
        "EXPERTISE_BITRIX_FIELD_MAP",
        json.dumps(
            {
                "title": "TITLE",
                "expertise_ref": "UF_CRM_EXPERTISE_REF",
                "expertise_number": "UF_CRM_EXPERTISE_NUMBER",
                "case_id": "UF_CRM_EXPERTISE_CASE_ID",
                "sale_ref": "UF_CRM_EXPERTISE_SALE_REF",
                "sale_number": "UF_CRM_EXPERTISE_SALE_NUMBER",
                "organization_ref": "UF_CRM_EXPERTISE_ORGANIZATION_REF",
                "contract_ref": "UF_CRM_EXPERTISE_CONTRACT_REF",
                "store": "UF_CRM_EXPERTISE_STORE",
                "customer": "UF_CRM_EXPERTISE_CUSTOMER",
                "phone": "UF_CRM_EXPERTISE_PHONE",
                "problem": "UF_CRM_EXPERTISE_PROBLEM",
                "decision_code": "UF_CRM_EXPERTISE_DECISION_CODE",
                "decision_label": "UF_CRM_EXPERTISE_DECISION_LABEL",
                "decision_comment": "UF_CRM_EXPERTISE_DECISION_COMMENT",
                "status": "UF_CRM_EXPERTISE_STATUS",
                "owner_ext": "UF_CRM_EXPERTISE_OWNER_EXT",
                "owner_name": "UF_CRM_EXPERTISE_OWNER_NAME",
                "due_at": "UF_CRM_EXPERTISE_DUE_AT",
                "overdue": "UF_CRM_EXPERTISE_OVERDUE",
                "client_notified": "UF_CRM_EXPERTISE_CLIENT_NOTIFIED",
                "sync_at": "UF_CRM_EXPERTISE_SYNC_AT",
                "source": "UF_CRM_EXPERTISE_SOURCE",
                "folder_url": "UF_CRM_EXPERTISE_FOLDER_URL",
                "assigned_by": "ASSIGNED_BY_ID",
            }
        ),
    )
    monkeypatch.setenv("EXPERTISE_BITRIX_ROOT_FOLDER_ID", "77")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID", "900")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS", "[900, 901, 902]")
    if include_store_map:
        monkeypatch.setenv(
            "EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP",
            json.dumps({"store-1": 501}),
        )
    else:
        monkeypatch.delenv("EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP", raising=False)
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_WARNING_HOURS", "24")
    monkeypatch.setenv("EXPERTISE_ALARM_NOTIFY_WARNING_HOURS", "24")
    monkeypatch.setenv("EXPERTISE_ALARM_NOTIFY_ESCALATION_HOURS", "48")
    monkeypatch.setenv(
        "EXPERTISE_ALARM_REVIEW_PRIMARY_DAYS_MAP",
        json.dumps({"moscow": 2, "spb": 13, "other": 13}),
    )
    monkeypatch.setenv(
        "EXPERTISE_ALARM_REVIEW_ESCALATION_DAYS_MAP",
        json.dumps({"moscow": 4, "spb": 15, "other": 15}),
    )
    monkeypatch.setenv(
        "EXPERTISE_ALARM_REVIEW_TOP_ESCALATION_DAYS_MAP",
        json.dumps({"moscow": 12, "spb": 23, "other": 23}),
    )
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_PRIMARY_USER_IDS", "[900]")
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_ESCALATION_USER_IDS", "[901,902]")
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_TOP_ESCALATION_USER_IDS", "[903]")
    monkeypatch.setenv(
        "EXPERTISE_SLA_STORE_GROUP_MAP",
        json.dumps({"store-1": "moscow", "store-2": "spb"}),
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_DELIVERY_DAYS_MAP",
        json.dumps({"moscow": 2, "spb": 8, "other": 8}),
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_REVIEW_DAYS_MAP",
        json.dumps({"moscow": 3, "spb": 14, "other": 14}),
    )
    get_settings.cache_clear()


def _make_case(
    session: Session,
    *,
    external_id: str = "exp-001",
    onec_expertise_ref: str = "1c-exp-001",
    onec_expertise_number: str = "ЭКС-001",
    current_status: str = "decision_ready",
    decision_label: str | None = "Принято",
    client_notified: bool = False,
    updated_at: datetime | None = None,
    bitrix_entity_id: str | None = None,
    bitrix_disk_folder_id: str | None = None,
    bitrix_disk_folder_url: str | None = None,
    bitrix_notify_task_id: str | None = None,
    store_external_id: str = "store-1",
    owner_user_external_id: str = "okk-1",
    payload_extra: dict | None = None,
    payload_posted: bool = False,
    created_at_source: datetime | None = None,
    due_at: datetime | None = None,
) -> ExpertiseCase:
    payload = {"source": "1c", "posted": payload_posted, "items": [{"line_no": 1}]}
    if payload_extra:
        payload.update(payload_extra)
    row = ExpertiseCase(
        external_id=external_id,
        onec_expertise_ref=onec_expertise_ref,
        onec_expertise_number=onec_expertise_number,
        created_at_source=created_at_source or datetime(2026, 4, 1, 10, 0, 0),
        organization_ref="org-001",
        contract_ref="contract-001",
        linked_sale_ref="sale-001",
        linked_sale_number="РБГУ010001",
        store_external_id=store_external_id,
        store_name="Щёлковская",
        customer_name="Иван Иванов",
        customer_phone="+79990000001",
        problem_summary="Не работает экран",
        current_status=current_status,
        decision_code=(
            "approved"
            if decision_label == "Принято"
            else "rejected" if decision_label == "Отказано" else None
        ),
        decision_label=decision_label,
        decision_comment="Подтвержден дефект",
        client_notified=client_notified,
        due_at=due_at or datetime(2026, 4, 15, 10, 0, 0),
        owner_user_external_id=owner_user_external_id,
        bitrix_entity_id=bitrix_entity_id,
        bitrix_disk_folder_id=bitrix_disk_folder_id,
        bitrix_disk_folder_url=bitrix_disk_folder_url,
        bitrix_notify_task_id=bitrix_notify_task_id,
        payload=payload,
        updated_at=updated_at or datetime(2026, 4, 2, 12, 0, 0),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_sync_case_to_bitrix_creates_folder_item_and_notify_task(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse(
                {"result": {"item": {"id": 555, "detailUrl": "https://sp/555/"}}}
            )
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "1001"}]})
        if method == "user.get":
            return _FakeHTTPResponse({"result": [{"ID": "1001"}, {"ID": "1002"}]})
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(session)
            result = expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)

            assert result["status"] == "synced"
            assert row.bitrix_entity_id == "555"
            assert row.bitrix_disk_folder_id == "701"
            assert row.bitrix_disk_folder_url == "https://disk/701/"
            assert row.bitrix_notify_task_id == "8801"
            assert row.bitrix_last_error is None
            assert row.bitrix_last_sync_at is not None

        assert [item[0] for item in calls] == [
            "disk.folder.getchildren",
            "disk.folder.addsubfolder",
            "crm.item.list",
            "crm.item.add",
            "department.get",
            "user.get",
            "tasks.task.add",
        ]
        task_params = next(params for method, params in calls if method == "tasks.task.add")
        assert task_params["fields[RESPONSIBLE_ID]"] == ["1001"]
        assert task_params["fields[CREATED_BY]"] == ["900"]
        assert task_params["fields[ACCOMPLICES][]"] == ["1002"]
        assert sorted(task_params["fields[AUDITORS][]"]) == ["901", "902"]

        list_params = next(params for method, params in calls if method == "crm.item.list")
        assert list_params["filter[ufCrmExpertiseRef]"] == ["1c-exp-001"]

        item_params = next(params for method, params in calls if method == "crm.item.add")
        assert item_params["fields[stageId]"] == ["DT187_1:DECISION"]
        assert item_params["fields[ufCrmExpertiseRef]"] == ["1c-exp-001"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_updates_existing_item_and_task(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []
    task_update_payloads: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.get":
            return _FakeHTTPResponse(
                {
                    "result": {
                        "ID": "701",
                        "DETAIL_URL": "https://disk/shared/path/Отдел контроля качества/Экспертиза",
                    }
                }
            )
        if method == "tasks.task.get":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801", "status": "2"}}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "1001"}]})
        if method == "user.get":
            return _FakeHTTPResponse({"result": [{"ID": "1001"}]})
        if method == "tasks.task.update":
            task_update_payloads.append(json.loads((request.data or b"{}").decode("utf-8")))
            return _FakeHTTPResponse({"result": True})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_disk_folder_url="https://disk/old/701/",
                bitrix_notify_task_id="8801",
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)
            assert (
                row.bitrix_disk_folder_url
                == "https://disk/shared/path/Отдел контроля качества/Экспертиза"
            )
        assert calls == [
            "disk.folder.get",
            "tasks.task.get",
            "crm.item.update",
            "department.get",
            "user.get",
            "tasks.task.update",
        ]
        assert len(task_update_payloads) == 1
        assert task_update_payloads[0]["taskId"] == "8801"
        task_fields = task_update_payloads[0]["fields"]
        assert task_fields["TITLE"] == "Уведомить клиента по экспертизе ЭКС-001"
        assert task_fields["RESPONSIBLE_ID"] == 1001
        assert task_fields["ACCOMPLICES"] == []
        assert task_fields["AUDITORS"] == [901, 902]
        assert "Экспертиза: ЭКС-001" in task_fields["DESCRIPTION"]
        assert "Smart-process item ID: 555" in task_fields["DESCRIPTION"]
        assert (
            "Папка Bitrix Disk: [URL=https://disk/shared/path/%D0%9E%D1%82%D0%B4%D0%B5%D0%BB%20"
            in task_fields["DESCRIPTION"]
        )
        assert "открыть папку[/URL]" in task_fields["DESCRIPTION"]
        assert "https://disk/shared/path/Отдел контроля" not in task_fields["DESCRIPTION"]
        assert "DEADLINE" in task_fields
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_recreates_missing_smart_process_item(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.update":
            payload = {
                "error": "NOT_FOUND",
                "error_description": "Элемент не найден",
            }
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            )
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 777}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "1001"}]})
        if method == "user.get":
            return _FakeHTTPResponse({"result": [{"ID": "1001"}]})
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_notify_task_id=None,
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)

            assert row.bitrix_entity_id == "777"
            assert row.bitrix_notify_task_id == "8801"
            assert row.bitrix_last_error is None
        assert calls == [
            "disk.folder.get",
            "crm.item.update",
            "crm.item.list",
            "crm.item.add",
            "department.get",
            "user.get",
            "tasks.task.add",
        ]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_completes_notify_task_when_client_notified(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "tasks.task.get":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801", "status": "2"}}})
        if method == "tasks.task.complete":
            return _FakeHTTPResponse({"result": True})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                current_status="client_notified",
                client_notified=True,
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_notify_task_id="8801",
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
        assert calls == [
            "disk.folder.get",
            "crm.item.update",
            "tasks.task.get",
            "tasks.task.complete",
        ]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_skips_complete_for_already_closed_terminal_task(
    monkeypatch,
) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "tasks.task.get":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801", "status": "5"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                current_status="returned_to_central_defect",
                client_notified=True,
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_notify_task_id="8801",
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
        assert calls == ["disk.folder.get", "crm.item.update", "tasks.task.get"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_treats_terminal_task_readback_as_best_effort(
    monkeypatch,
) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "tasks.task.get":
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                {},
                io.BytesIO(b"temporary error"),
            )
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                current_status="returned_to_store",
                decision_label="Отказано",
                client_notified=True,
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_notify_task_id="8801",
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)

            assert row.bitrix_last_error is None
            event = session.scalar(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "automation_error",
                )
            )
            assert event is not None
            assert "readback failed" in (event.comment or "")
        assert calls == ["disk.folder.get", "crm.item.update", "tasks.task.get"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_finalizes_approved_case_when_notify_task_closed(
    monkeypatch,
) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "tasks.task.get":
            return _FakeHTTPResponse(
                {
                    "result": {
                        "task": {
                            "id": "8801",
                            "status": "5",
                            "closedDate": "2026-04-18T12:30:00+03:00",
                        }
                    }
                }
            )
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_notify_task_id="8801",
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)

            assert row.client_notified is True
            assert row.current_status == "returned_to_central_defect"
            assert row.due_at is None
            events = session.scalars(
                select(ExpertiseCaseEvent)
                .where(ExpertiseCaseEvent.expertise_case_id == row.id)
                .order_by(ExpertiseCaseEvent.event_type.asc())
            ).all()
            assert [event.event_type for event in events] == [
                "client_notified",
                "returned_to_central_defect",
            ]
            assert {event.source for event in events} == {"bitrix"}

        methods = [method for method, _ in calls]
        assert methods == ["disk.folder.get", "tasks.task.get", "crm.item.update"]
        update_params = next(params for method, params in calls if method == "crm.item.update")
        assert update_params["fields[stageId]"] == ["DT187_1:SUCCESS"]
        assert update_params["fields[ufCrmExpertiseClientNotified]"] == ["Y"]
        assert "tasks.task.complete" not in methods
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_finalizes_rejected_case_when_notify_task_closed(
    monkeypatch,
) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "tasks.task.get":
            return _FakeHTTPResponse({"result": {"task": {"id": "8802", "status": "5"}}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                decision_label="Отказано",
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_notify_task_id="8802",
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)

            assert row.client_notified is True
            assert row.current_status == "returned_to_store"
            assert row.due_at is None
            event_types = session.scalars(
                select(ExpertiseCaseEvent.event_type)
                .where(ExpertiseCaseEvent.expertise_case_id == row.id)
                .order_by(ExpertiseCaseEvent.event_type.asc())
            ).all()
            assert event_types == ["client_notified", "returned_to_store"]

        update_params = next(params for method, params in calls if method == "crm.item.update")
        assert update_params["fields[stageId]"] == ["DT187_1:FAIL"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_maps_under_review_to_shared_okk_stage(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                current_status="under_review",
                bitrix_notify_task_id=None,
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()

        item_params = next(params for method, params in calls if method == "crm.item.add")
        assert item_params["fields[stageId]"] == ["DT187_1:PREPARATION"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_skips_inactive_posted_created_case(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                current_status="created",
                decision_label="Принято",
                bitrix_entity_id=None,
                bitrix_disk_folder_id=None,
                payload_posted=True,
            )
            result = expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)
            assert result["status"] == "skipped_inactive"
            assert row.bitrix_entity_id is None
            assert row.bitrix_last_sync_at is None
            assert calls == []
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_reuses_existing_disk_folder(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": [{"ID": "701", "DETAIL_URL": "https://disk/701/"}]})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                current_status="created",
                decision_label=None,
                payload_posted=False,
            )
            result = expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()
            session.refresh(row)
            assert result["status"] == "synced"
            assert row.bitrix_disk_folder_id == "701"
            assert row.bitrix_disk_folder_url == "https://disk/701/"
            assert row.bitrix_entity_id == "555"
            assert "disk.folder.addsubfolder" not in calls
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_falls_back_when_department_mapping_is_missing(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch, include_store_map=False)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(session)
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()

            events = session.scalars(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "automation_error",
                )
            ).all()
            assert len(events) == 1
            assert "department is not configured" in (events[0].comment or "")

        task_params = next(params for method, params in calls if method == "tasks.task.add")
        assert task_params["fields[RESPONSIBLE_ID]"] == ["900"]
        assert task_params["fields[CREATED_BY]"] == ["900"]
        assert "fields[ACCOMPLICES][]" not in task_params
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_falls_back_when_department_head_is_missing(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": ""}]})
        if method == "user.get":
            return _FakeHTTPResponse({"result": [{"ID": "1001"}, {"ID": "1002"}]})
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(session)
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()

            events = session.scalars(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "automation_error",
                )
            ).all()
            assert len(events) == 1
            assert "fallback to department manager" in (events[0].comment or "")

        task_params = next(params for method, params in calls if method == "tasks.task.add")
        assert task_params["fields[RESPONSIBLE_ID]"] == ["1001"]
        assert task_params["fields[CREATED_BY]"] == ["900"]
        assert task_params["fields[ACCOMPLICES][]"] == ["1002"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_prefers_active_non_courier_owner(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "1001"}]})
        if method == "user.get":
            return _FakeHTTPResponse(
                {
                    "result": [
                        {
                            "ID": "1001",
                            "NAME": "Мария",
                            "LAST_NAME": "Давыденкова",
                            "WORK_POSITION": "Курьер",
                        },
                        {
                            "ID": "1002",
                            "NAME": "Дмитрий",
                            "LAST_NAME": "Куценко",
                            "SECOND_NAME": "Алексеевич",
                            "WORK_POSITION": "Менеджер по продажам",
                        },
                        {
                            "ID": "1003",
                            "NAME": "Камиль",
                            "LAST_NAME": "Гаджимурадов",
                            "WORK_POSITION": "Менеджер по продажам",
                        },
                        {
                            "ID": "1004",
                            "NAME": "Иван",
                            "LAST_NAME": "Морозов",
                            "WORK_POSITION": "Курьер",
                        },
                    ]
                }
            )
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                owner_user_external_id="0xtechnical",
                payload_extra={"responsible_name": "Куценко Дмитрий Алексеевич"},
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()

            events = session.scalars(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "automation_error",
                )
            ).all()
            assert events == []

        task_params = next(params for method, params in calls if method == "tasks.task.add")
        assert task_params["fields[RESPONSIBLE_ID]"] == ["1002"]
        assert task_params["fields[ACCOMPLICES][]"] == ["1003"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_falls_back_to_manager_when_owner_is_technical(
    monkeypatch,
) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "130912"}]})
        if method == "user.get":
            return _FakeHTTPResponse(
                {
                    "result": [
                        {
                            "ID": "130912",
                            "NAME": "Мария",
                            "LAST_NAME": "Давыденкова",
                            "WORK_POSITION": "Курьер",
                        },
                        {
                            "ID": "130907",
                            "NAME": "Ксения",
                            "LAST_NAME": "Сосновская",
                            "WORK_POSITION": "Мерчандайзер",
                        },
                        {
                            "ID": "131017",
                            "NAME": "Дмитрий",
                            "LAST_NAME": "Куценко",
                            "WORK_POSITION": "Менеджер по продажам",
                        },
                        {
                            "ID": "131748",
                            "NAME": "Камиль",
                            "LAST_NAME": "Гаджимурадов",
                            "WORK_POSITION": "Менеджер по продажам",
                        },
                        {
                            "ID": "132818",
                            "NAME": "Иван",
                            "LAST_NAME": "Морозов",
                            "WORK_POSITION": "Курьер",
                        },
                    ]
                }
            )
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                owner_user_external_id="0x9e79002590803daf11efe977ca909c8c",
                payload_extra={"responsible_name": "Стажер_Экама12"},
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()

            events = session.scalars(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "automation_error",
                )
            ).all()
            assert len(events) == 1
            assert "fallback to department manager" in (events[0].comment or "")

        task_params = next(params for method, params in calls if method == "tasks.task.add")
        assert task_params["fields[RESPONSIBLE_ID]"] == ["131017"]
        assert task_params["fields[ACCOMPLICES][]"] == ["131748"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_sync_case_to_bitrix_allows_store_manager_head_as_responsible(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.getchildren":
            return _FakeHTTPResponse({"result": []})
        if method == "disk.folder.addsubfolder":
            return _FakeHTTPResponse({"result": {"ID": "701"}})
        if method == "crm.item.list":
            return _FakeHTTPResponse({"result": {"items": []}})
        if method == "crm.item.add":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "130743"}]})
        if method == "user.get":
            return _FakeHTTPResponse(
                {
                    "result": [
                        {
                            "ID": "130743",
                            "NAME": "Зафаржон",
                            "LAST_NAME": "Юлдошев",
                            "WORK_POSITION": "Управляющий магазином",
                        },
                        {
                            "ID": "130744",
                            "NAME": "Елена",
                            "LAST_NAME": "Петрова",
                            "WORK_POSITION": "Менеджер по продажам",
                        },
                        {
                            "ID": "130745",
                            "NAME": "Игорь",
                            "LAST_NAME": "Сидоров",
                            "WORK_POSITION": "Курьер",
                        },
                    ]
                }
            )
        if method == "tasks.task.add":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801"}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                owner_user_external_id="0xtechnical",
                payload_extra={"responsible_name": "Стажер_Экама12"},
            )
            expertise_bitrix.sync_case_to_bitrix(session, case_id=row.id)
            session.commit()

        task_params = next(params for method, params in calls if method == "tasks.task.add")
        assert task_params["fields[RESPONSIBLE_ID]"] == ["130743"]
        assert task_params["fields[ACCOMPLICES][]"] == ["130744"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_scan_alarm_cases_adds_events_once(monkeypatch) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("EXPERTISE_BITRIX_WEBHOOK_URL", "")
    monkeypatch.setenv("EXPERTISE_BITRIX_ENTITY_TYPE_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_CATEGORY_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_ROOT_FOLDER_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_STAGE_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_FIELD_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS", "[]")
    monkeypatch.setenv("EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_WARNING_HOURS", "24")
    monkeypatch.setenv("EXPERTISE_ALARM_NOTIFY_WARNING_HOURS", "24")
    monkeypatch.setenv("EXPERTISE_ALARM_NOTIFY_ESCALATION_HOURS", "48")
    monkeypatch.setenv(
        "EXPERTISE_ALARM_REVIEW_PRIMARY_DAYS_MAP",
        '{"moscow":1,"spb":13,"other":13}',
    )
    monkeypatch.setenv(
        "EXPERTISE_ALARM_REVIEW_ESCALATION_DAYS_MAP",
        '{"moscow":4,"spb":15,"other":15}',
    )
    monkeypatch.setenv(
        "EXPERTISE_ALARM_REVIEW_TOP_ESCALATION_DAYS_MAP",
        '{"moscow":12,"spb":23,"other":23}',
    )
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_PRIMARY_USER_IDS", "[0]")
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_ESCALATION_USER_IDS", "[]")
    monkeypatch.setenv("EXPERTISE_ALARM_REVIEW_TOP_ESCALATION_USER_IDS", "[]")
    monkeypatch.setenv(
        "EXPERTISE_SLA_STORE_GROUP_MAP",
        '{"store-1":"moscow","store-2":"spb"}',
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_DELIVERY_DAYS_MAP",
        '{"moscow":2,"spb":8,"other":8}',
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_REVIEW_DAYS_MAP",
        '{"moscow":3,"spb":14,"other":14}',
    )
    get_settings.cache_clear()

    now = datetime(2026, 4, 10, 12, 0, 0)

    try:
        with Session(engine) as session:
            review_row = _make_case(
                session,
                external_id="exp-review",
                onec_expertise_ref="1c-exp-review",
                onec_expertise_number="ЭКС-REVIEW",
                current_status="under_review",
                updated_at=now - timedelta(hours=30),
                bitrix_entity_id=None,
                bitrix_disk_folder_id=None,
                bitrix_notify_task_id=None,
            )
            decision_row = _make_case(
                session,
                external_id="exp-decision",
                onec_expertise_ref="1c-exp-decision",
                onec_expertise_number="ЭКС-DECISION",
                current_status="decision_ready",
                updated_at=now - timedelta(hours=60),
                bitrix_entity_id=None,
                bitrix_disk_folder_id=None,
                bitrix_notify_task_id=None,
                store_external_id="store-2",
            )
            session.add(
                ExpertiseCaseEvent(
                    expertise_case_id=review_row.id,
                    event_type="moved_to_review",
                    event_at=now - timedelta(hours=30),
                    source="api",
                )
            )
            session.add(
                ExpertiseCaseEvent(
                    expertise_case_id=decision_row.id,
                    event_type="decision_recorded",
                    event_at=now - timedelta(hours=60),
                    source="api",
                )
            )
            session.commit()

            summary_first = expertise_bitrix.scan_alarm_cases(session, now=now)
            summary_second = expertise_bitrix.scan_alarm_cases(session, now=now)

            assert summary_first["review_warning"] == 1
            assert summary_first["client_notify_reminder"] == 1
            assert summary_first["client_notify_escalation"] == 1
            assert summary_second["review_warning"] == 0
            assert summary_second["client_notify_reminder"] == 0
            assert summary_second["client_notify_escalation"] == 0

            event_types = session.scalars(
                select(ExpertiseCaseEvent.event_type).order_by(ExpertiseCaseEvent.id.asc())
            ).all()
            assert event_types.count("review_warning") == 1
            assert event_types.count("client_notify_reminder") == 1
            assert event_types.count("client_notify_escalation") == 1
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_scan_alarm_cases_auto_moves_created_case_to_received_by_okk(monkeypatch) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("EXPERTISE_BITRIX_WEBHOOK_URL", "")
    monkeypatch.setenv("EXPERTISE_BITRIX_ENTITY_TYPE_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_CATEGORY_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_ROOT_FOLDER_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_STAGE_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_FIELD_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS", "[]")
    monkeypatch.setenv("EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_SLA_STORE_GROUP_MAP", '{"store-1":"moscow"}')
    monkeypatch.setenv(
        "EXPERTISE_SLA_DELIVERY_DAYS_MAP",
        '{"moscow":2,"spb":8,"other":8}',
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_REVIEW_DAYS_MAP",
        '{"moscow":3,"spb":14,"other":14}',
    )
    get_settings.cache_clear()

    now = datetime(2026, 4, 3, 12, 0, 0)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                external_id="exp-created",
                onec_expertise_ref="1c-exp-created",
                onec_expertise_number="ЭКС-CREATED",
                current_status="created",
                decision_label=None,
                bitrix_entity_id=None,
                bitrix_disk_folder_id=None,
                bitrix_notify_task_id=None,
                store_external_id="store-1",
                due_at=datetime(2026, 4, 15, 10, 0, 0),
            )

            summary = expertise_bitrix.scan_alarm_cases(session, now=now)
            session.refresh(row)

            assert summary["auto_received_by_okk"] == 1
            assert summary["review_warning"] == 0
            assert row.current_status == "received_by_okk"
            assert row.due_at == datetime(2026, 4, 6, 10, 0, 0)

            event = session.scalar(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "received_by_okk",
                )
            )
            assert event is not None
            assert event.source == "automation"
            assert event.event_at == datetime(2026, 4, 3, 10, 0, 0)
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_scan_alarm_cases_uses_other_group_when_store_mapping_missing(monkeypatch) -> None:
    engine, path = _setup_db()
    monkeypatch.setenv("EXPERTISE_BITRIX_WEBHOOK_URL", "")
    monkeypatch.setenv("EXPERTISE_BITRIX_ENTITY_TYPE_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_CATEGORY_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_ROOT_FOLDER_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID", "0")
    monkeypatch.setenv("EXPERTISE_BITRIX_STAGE_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_FIELD_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS", "[]")
    monkeypatch.setenv("EXPERTISE_BITRIX_STORE_DEPARTMENT_MAP", "{}")
    monkeypatch.setenv("EXPERTISE_SLA_STORE_GROUP_MAP", '{"store-1":"moscow"}')
    monkeypatch.setenv(
        "EXPERTISE_SLA_DELIVERY_DAYS_MAP",
        '{"moscow":2,"spb":8,"other":8}',
    )
    monkeypatch.setenv(
        "EXPERTISE_SLA_REVIEW_DAYS_MAP",
        '{"moscow":3,"spb":14,"other":14}',
    )
    get_settings.cache_clear()

    now = datetime(2026, 4, 2, 12, 0, 0)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                external_id="exp-fallback",
                onec_expertise_ref="1c-exp-fallback",
                onec_expertise_number="ЭКС-FALLBACK",
                current_status="created",
                decision_label=None,
                bitrix_entity_id=None,
                bitrix_disk_folder_id=None,
                bitrix_notify_task_id=None,
                store_external_id="store-999",
                due_at=datetime(2026, 4, 15, 10, 0, 0),
            )

            summary = expertise_bitrix.scan_alarm_cases(session, now=now)
            session.refresh(row)

            assert summary["auto_received_by_okk"] == 0
            assert row.current_status == "created"
            assert row.due_at == datetime(2026, 4, 9, 10, 0, 0)

            event = session.scalar(
                select(ExpertiseCaseEvent).where(
                    ExpertiseCaseEvent.expertise_case_id == row.id,
                    ExpertiseCaseEvent.event_type == "automation_error",
                )
            )
            assert event is not None
            assert "applied group=other" in (event.comment or "")
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_scan_alarm_cases_delivers_bitrix_notifications(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []
    now = datetime(2026, 4, 10, 12, 0, 0)

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.get":
            return _FakeHTTPResponse(
                {"result": {"ID": "701", "DETAIL_URL": "https://disk/new/701/"}}
            )
        if method == "tasks.task.get":
            return _FakeHTTPResponse({"result": {"task": {"id": "8801", "status": "2"}}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "department.get":
            return _FakeHTTPResponse({"result": [{"ID": "501", "UF_HEAD": "1001"}]})
        if method == "user.get":
            return _FakeHTTPResponse({"result": [{"ID": "1001"}]})
        if method == "tasks.task.update":
            return _FakeHTTPResponse({"result": True})
        if method == "im.notify.personal.add":
            return _FakeHTTPResponse({"result": 9001})
        if method == "task.commentitem.add":
            return _FakeHTTPResponse({"result": {"ID": "7001"}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                external_id="exp-decision",
                onec_expertise_ref="1c-exp-decision",
                onec_expertise_number="ЭКС-DECISION",
                current_status="decision_ready",
                updated_at=now - timedelta(hours=60),
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_disk_folder_url="https://disk/old/701/",
                bitrix_notify_task_id="8801",
                store_external_id="store-1",
            )
            session.add(
                ExpertiseCaseEvent(
                    expertise_case_id=row.id,
                    event_type="decision_recorded",
                    event_at=now - timedelta(hours=60),
                    source="api",
                )
            )
            session.commit()

            summary = expertise_bitrix.scan_alarm_cases(session, now=now)
            session.refresh(row)

            assert summary["client_notify_reminder"] == 1
            assert summary["client_notify_escalation"] == 1
            assert summary["synced"] == 1
            assert summary["errors"] == 0
            assert row.bitrix_disk_folder_url == "https://disk/new/701/"

        methods = [method for method, _ in calls]
        assert methods.count("im.notify.personal.add") == 6
        assert methods.count("task.commentitem.add") == 2

        notify_user_ids = [
            params["USER_ID"][0] for method, params in calls if method == "im.notify.personal.add"
        ]
        assert notify_user_ids == ["900", "901", "902", "900", "901", "902"]

        comment_messages = [
            params["arFields[POST_MESSAGE]"][0]
            for method, params in calls
            if method == "task.commentitem.add"
        ]
        assert any("Напоминание: клиент еще не оповещен" in message for message in comment_messages)
        assert any("Эскалация: клиент не оповещен" in message for message in comment_messages)
        assert all("ЭКС-DECISION" in message for message in comment_messages)
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_scan_alarm_cases_syncs_only_current_month_overdue_cases(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[str] = []
    now = datetime(2026, 5, 18, 12, 0, 0)

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append(method)
        if method == "disk.folder.get":
            return _FakeHTTPResponse({"result": {"ID": "701", "DETAIL_URL": "https://disk/701/"}})
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            _make_case(
                session,
                external_id="exp-old-overdue",
                onec_expertise_ref="1c-exp-old-overdue",
                onec_expertise_number="ЭКС-OLD",
                current_status="received_by_okk",
                decision_label=None,
                updated_at=now - timedelta(hours=1),
                bitrix_entity_id="554",
                bitrix_disk_folder_id="700",
                bitrix_disk_folder_url="https://disk/700/",
                due_at=datetime(2026, 4, 17, 10, 0, 0),
            )
            _make_case(
                session,
                external_id="exp-fresh-overdue",
                onec_expertise_ref="1c-exp-fresh-overdue",
                onec_expertise_number="ЭКС-FRESH",
                current_status="received_by_okk",
                decision_label=None,
                updated_at=now - timedelta(hours=1),
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_disk_folder_url="https://disk/701/",
                due_at=datetime(2026, 5, 17, 10, 0, 0),
            )

            summary = expertise_bitrix.scan_alarm_cases(session, now=now)

            assert summary["synced"] == 1
            assert summary["errors"] == 0
        assert calls == ["disk.folder.get", "crm.item.update"]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_scan_alarm_cases_delivers_review_escalation_ladder(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_bitrix_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []
    now = datetime(2026, 4, 20, 12, 0, 0)

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append((method, params))
        if method == "disk.folder.get":
            return _FakeHTTPResponse(
                {"result": {"ID": "701", "DETAIL_URL": "https://disk/new/701/"}}
            )
        if method == "crm.item.update":
            return _FakeHTTPResponse({"result": {"item": {"id": 555}}})
        if method == "im.notify.personal.add":
            return _FakeHTTPResponse({"result": 9001})
        raise AssertionError(f"unexpected Bitrix24 method: {method}")

    monkeypatch.setattr(expertise_bitrix.urllib.request, "urlopen", fake_urlopen)

    try:
        with Session(engine) as session:
            row = _make_case(
                session,
                external_id="exp-review-ladder",
                onec_expertise_ref="1c-exp-review-ladder",
                onec_expertise_number="ЭКС-REVIEW-LADDER",
                current_status="under_review",
                decision_label=None,
                updated_at=now - timedelta(days=13),
                bitrix_entity_id="555",
                bitrix_disk_folder_id="701",
                bitrix_disk_folder_url="https://disk/old/701/",
                bitrix_notify_task_id=None,
                store_external_id="store-1",
            )
            session.add(
                ExpertiseCaseEvent(
                    expertise_case_id=row.id,
                    event_type="moved_to_review",
                    event_at=now - timedelta(days=13),
                    source="api",
                )
            )
            session.commit()

            summary_first = expertise_bitrix.scan_alarm_cases(session, now=now)
            summary_second = expertise_bitrix.scan_alarm_cases(session, now=now)

            assert summary_first["review_warning"] == 1
            assert summary_first["review_escalation"] == 1
            assert summary_first["review_top_escalation"] == 1
            assert summary_second["review_warning"] == 0
            assert summary_second["review_escalation"] == 0
            assert summary_second["review_top_escalation"] == 0

            event_types = session.scalars(
                select(ExpertiseCaseEvent.event_type).order_by(ExpertiseCaseEvent.id.asc())
            ).all()
            assert event_types.count("review_warning") == 1
            assert event_types.count("review_escalation") == 1
            assert event_types.count("review_top_escalation") == 1

        notify_user_ids = [
            params["USER_ID"][0] for method, params in calls if method == "im.notify.personal.add"
        ]
        assert notify_user_ids == ["900", "901", "902", "903"]
        assert "task.commentitem.add" not in [method for method, _ in calls]
    finally:
        get_settings.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)
