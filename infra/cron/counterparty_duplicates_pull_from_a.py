#!/usr/bin/env python3
"""Consume pending counterparty duplicate cases from server A and upsert them on server B."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable


def _load_env(path: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if not path:
        return env
    env_path = Path(path)
    if not env_path.exists():
        return env
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def _b24_call(base_url: str, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{method}.json"
    data = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(
            f"Bitrix24 {method}: {payload['error']} {payload.get('error_description', '')}"
        )
    return payload


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"cases": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _fingerprint_case(payload: dict[str, Any]) -> str:
    source = {
        "dedupe_key": payload.get("dedupe_key"),
        "risk_level": payload.get("risk_level"),
        "reason_codes": payload.get("reason_codes"),
        "records": payload.get("records"),
        "status": payload.get("status"),
        "sla_deadline_at": payload.get("sla_deadline_at"),
        "summary_text": payload.get("summary_text"),
        "source_hash": payload.get("source_hash"),
    }
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_title(payload: dict[str, Any]) -> str:
    refs = [
        item.get("counterparty_ref")
        for item in payload.get("records", [])
        if item.get("counterparty_ref")
    ]
    return f"[{payload.get('risk_level', 'P1')}] Дубли контрагентов: {', '.join(refs[:3])}"


def _render_description(payload: dict[str, Any]) -> str:
    lines = [
        f"Case ID: {payload.get('case_id')}",
        f"Dedupe key: {payload.get('dedupe_key')}",
        f"Risk: {payload.get('risk_level')}",
        f"Reasons: {', '.join(payload.get('reason_codes', []))}",
        f"SLA: {payload.get('sla_deadline_at')}",
        "",
        str(payload.get("summary_text") or ""),
        "",
        "Кандидаты:",
    ]
    for item in payload.get("records", []):
        lines.append(
            f"- {item.get('counterparty_ref')}: {item.get('counterparty_name') or ''} | "
            f"phone={item.get('phone') or '-'} | email={item.get('email') or '-'} | "
            f"tax_id={item.get('tax_id') or '-'}"
        )
    return "\n".join(lines)


def _create_smart_process_item(
    *,
    webhook_url: str,
    entity_type_id: int,
    payload: dict[str, Any],
    field_map: dict[str, str],
    responsible_id: int | None = None,
) -> tuple[str, str | None]:
    fields = {
        field_map["title"]: _render_title(payload),
        field_map["dedupe_key"]: str(payload["dedupe_key"]),
        field_map["case_id"]: str(payload["case_id"]),
        field_map["risk_level"]: str(payload["risk_level"]),
        field_map["reason_codes"]: json.dumps(payload.get("reason_codes", []), ensure_ascii=False),
        field_map["detected_at"]: str(payload["detected_at"]),
        field_map["sla_deadline_at"]: str(payload["sla_deadline_at"]),
        field_map["records_json"]: json.dumps(payload.get("records", []), ensure_ascii=False),
        field_map["source_hash"]: str(payload["source_hash"]),
        field_map["source_system"]: "1C",
        field_map["status"]: str(payload["status"]),
        field_map["summary_text"]: _render_description(payload),
    }
    params = [("entityTypeId", str(entity_type_id))]
    for key, value in fields.items():
        params.append((f"fields[{key}]", value))
    if responsible_id is not None and field_map.get("assigned_by"):
        params.append((f"fields[{field_map['assigned_by']}]", str(responsible_id)))
    response = _b24_call(webhook_url, "crm.item.add", params)
    item = response.get("result", {}).get("item", {}) or {}
    return str(item.get("id") or response.get("result")), item.get("detailUrl")


def _update_smart_process_item(
    *,
    webhook_url: str,
    entity_type_id: int,
    item_id: str,
    payload: dict[str, Any],
    field_map: dict[str, str],
    responsible_id: int | None = None,
) -> None:
    fields = {
        field_map["title"]: _render_title(payload),
        field_map["risk_level"]: str(payload["risk_level"]),
        field_map["reason_codes"]: json.dumps(payload.get("reason_codes", []), ensure_ascii=False),
        field_map["sla_deadline_at"]: str(payload["sla_deadline_at"]),
        field_map["records_json"]: json.dumps(payload.get("records", []), ensure_ascii=False),
        field_map["source_hash"]: str(payload["source_hash"]),
        field_map["status"]: str(payload["status"]),
        field_map["summary_text"]: _render_description(payload),
    }
    params = [("entityTypeId", str(entity_type_id)), ("id", str(item_id))]
    for key, value in fields.items():
        params.append((f"fields[{key}]", value))
    if responsible_id is not None and field_map.get("assigned_by"):
        params.append((f"fields[{field_map['assigned_by']}]", str(responsible_id)))
    _b24_call(webhook_url, "crm.item.update", params)


def sync_counterparty_duplicate_cases(
    *,
    fetch_json: Callable[[str, dict[str, str]], dict[str, Any]],
    ack_case: Callable[..., dict[str, Any]],
    state_path: Path,
    create_case: Callable[..., tuple[str, str | None]] | None = None,
    update_case: Callable[..., None] | None = None,
    webhook_url: str | None = None,
    entity_type_id: int | None = None,
    field_map: dict[str, str] | None = None,
    default_responsible_id: int | None = None,
) -> dict[str, Any]:
    state = _load_state(state_path)
    stored_cases = state.setdefault("cases", {})
    payload = fetch_json("/api/internal/counterparty-duplicates/pending", {})
    items = payload.get("items", [])
    summary = {"created": 0, "updated": 0, "noop": 0}

    for item in items:
        dedupe_key = str(item["dedupe_key"])
        fingerprint = _fingerprint_case(item)
        stored = stored_cases.get(dedupe_key)
        external_case_id = None if stored is None else stored.get("external_case_id")
        external_url = None if stored is None else stored.get("external_url")

        if stored and stored.get("fingerprint") == fingerprint:
            summary["noop"] += 1
            continue

        if external_case_id is None:
            if create_case is not None:
                external_case_id, external_url = create_case(
                    webhook_url=webhook_url,
                    entity_type_id=entity_type_id,
                    payload=item,
                    field_map=field_map or {},
                    responsible_id=default_responsible_id,
                )
            summary["created"] += 1
        else:
            if update_case is not None:
                update_case(
                    webhook_url=webhook_url,
                    entity_type_id=entity_type_id,
                    item_id=external_case_id,
                    payload=item,
                    field_map=field_map or {},
                    responsible_id=default_responsible_id,
                )
            summary["updated"] += 1

        ack_case(
            case_id=int(item["case_id"]),
            external_case_id=external_case_id,
            external_status="new",
            external_url=external_url,
            status=item.get("status"),
        )
        stored_cases[dedupe_key] = {
            "external_case_id": external_case_id,
            "external_url": external_url,
            "fingerprint": fingerprint,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    _save_state(state_path, state)
    return summary


def main() -> None:
    env = _load_env(
        os.getenv("COUNTERPARTY_DUPLICATES_B_ENV_FILE") or os.getenv("OPENCLAW_ENV_FILE")
    )
    required = ["COUNTERPARTY_DUPLICATES_SOURCE_URL", "COUNTERPARTY_DUPLICATES_SOURCE_TOKEN"]
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise SystemExit(f"Missing required env: {', '.join(missing)}")

    source_base = env["COUNTERPARTY_DUPLICATES_SOURCE_URL"].rstrip("/")
    auth_headers = {"Authorization": f"Bearer {env['COUNTERPARTY_DUPLICATES_SOURCE_TOKEN']}"}
    state_path = Path(
        env.get(
            "COUNTERPARTY_DUPLICATES_STATE_PATH",
            "/home/deploy/.openclaw/workspace/.data/counterparty-duplicates/state.json",
        )
    )
    entity_type_id = int(env.get("COUNTERPARTY_DUPLICATES_B24_ENTITY_TYPE_ID", "0") or 0)
    webhook_url = env.get("COUNTERPARTY_DUPLICATES_B24_WEBHOOK_URL") or env.get(
        "BITRIX24_WEBHOOK_URL"
    )
    field_map = {
        "title": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_TITLE", "TITLE"),
        "assigned_by": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_ASSIGNED_BY", ""),
        "case_id": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_CASE_ID", "ufCrmCaseId"),
        "dedupe_key": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_DEDUPE_KEY", "ufCrmDedupeKey"),
        "risk_level": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_RISK_LEVEL", "ufCrmRiskLevel"),
        "reason_codes": env.get(
            "COUNTERPARTY_DUPLICATES_B24_FIELD_REASON_CODES", "ufCrmReasonCodes"
        ),
        "detected_at": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_DETECTED_AT", "ufCrmDetectedAt"),
        "sla_deadline_at": env.get(
            "COUNTERPARTY_DUPLICATES_B24_FIELD_SLA_DEADLINE", "ufCrmSlaDeadline"
        ),
        "records_json": env.get(
            "COUNTERPARTY_DUPLICATES_B24_FIELD_RECORDS_JSON", "ufCrmRecordsJson"
        ),
        "source_hash": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_SOURCE_HASH", "ufCrmSourceHash"),
        "source_system": env.get(
            "COUNTERPARTY_DUPLICATES_B24_FIELD_SOURCE_SYSTEM", "ufCrmSourceSystem"
        ),
        "status": env.get("COUNTERPARTY_DUPLICATES_B24_FIELD_STATUS", "ufCrmCaseStatus"),
        "summary_text": env.get(
            "COUNTERPARTY_DUPLICATES_B24_FIELD_SUMMARY_TEXT", "ufCrmSummaryText"
        ),
    }
    default_responsible_id = (
        int(env["COUNTERPARTY_DUPLICATES_B24_DEFAULT_RESPONSIBLE_ID"])
        if env.get("COUNTERPARTY_DUPLICATES_B24_DEFAULT_RESPONSIBLE_ID")
        else None
    )

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"{source_base}{path}"
        if query:
            url += f"?{query}"
        return _http_json(url, headers=auth_headers)

    def ack_case(**payload: Any) -> dict[str, Any]:
        case_id = payload.pop("case_id")
        return _http_json(
            f"{source_base}/api/internal/counterparty-duplicates/{case_id}/ack",
            method="POST",
            headers=auth_headers,
            payload=payload,
        )

    summary = sync_counterparty_duplicate_cases(
        fetch_json=fetch_json,
        ack_case=ack_case,
        state_path=state_path,
        create_case=_create_smart_process_item if webhook_url and entity_type_id > 0 else None,
        update_case=_update_smart_process_item if webhook_url and entity_type_id > 0 else None,
        webhook_url=webhook_url,
        entity_type_id=entity_type_id,
        field_map=field_map,
        default_responsible_id=default_responsible_id,
    )
    print(json.dumps({"date": date.today().isoformat(), **summary}, ensure_ascii=False))
