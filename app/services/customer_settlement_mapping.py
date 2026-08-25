from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.customer_settlements import (
    MAPPING_AMBIGUOUS,
    MAPPING_LINKED,
    MAPPING_NOT_LINKED,
    SettlementMappingInput,
    normalize_counterparty_ref,
    normalize_site_user_id,
)

CRM_CLUSTER_FIELD = "UF_CRM_MM_CUSTOMER_CLUSTER_ID"
CRM_SITE_USERS_FIELD = "UF_CRM_MM_BOX_SHOP_USER_IDS"
CRM_COUNTERPARTIES_FIELD = "UF_CRM_MM_ONEC_COUNTERPARTY_IDS"
CRM_UPDATED_AT_FIELD = "UF_CRM_MM_LAST_SYNC_AT"
CRM_SOURCE_SYSTEMS_FIELD = "UF_CRM_MM_SYNC_SOURCE_SYSTEMS"
CRM_PAGE_SIZE = 50
CRM_BATCH_PAGE_COUNT = 50
CRM_ONEC_HASH_RE = re.compile(r"^[0-9a-fA-F]{24}$")


class CustomerSettlementMappingSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrmClusterSourceRow:
    row_id: str
    cluster_id: str | None
    site_user_ids: tuple[str, ...]
    counterparty_refs: tuple[str, ...]
    source_updated_at: datetime | None
    counterparty_hashes: tuple[str, ...] = ()
    has_invalid_site_user_id: bool = False
    has_invalid_counterparty_ref: bool = False


def _list_values(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    values = value if isinstance(value, list | tuple | set) else (value,)
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("VALUE") or item.get("value")
        normalized = str(item or "").strip()
        if normalized:
            result.append(normalized)
    return tuple(result)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CustomerSettlementMappingSourceError("invalid_crm_mapping_timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_crm_cluster_row(payload: dict[str, Any]) -> CrmClusterSourceRow:
    row_id = str(payload.get("ID") or payload.get("id") or "").strip()
    if not row_id:
        raise CustomerSettlementMappingSourceError("crm_mapping_row_without_id")
    cluster_id = str(payload.get(CRM_CLUSTER_FIELD) or "").strip() or None
    site_user_ids: set[str] = set()
    has_invalid_site_user_id = False
    for value in _list_values(payload.get(CRM_SITE_USERS_FIELD)):
        try:
            site_user_ids.add(normalize_site_user_id(value))
        except ValueError:
            has_invalid_site_user_id = True
    counterparty_refs: set[str] = set()
    counterparty_hashes: set[str] = set()
    has_invalid_counterparty_ref = False
    for value in _list_values(payload.get(CRM_COUNTERPARTIES_FIELD)):
        if CRM_ONEC_HASH_RE.fullmatch(value):
            counterparty_hashes.add(value.lower())
            continue
        try:
            counterparty_refs.add(normalize_counterparty_ref(value))
        except ValueError:
            has_invalid_counterparty_ref = True
    return CrmClusterSourceRow(
        row_id=row_id,
        cluster_id=cluster_id,
        site_user_ids=tuple(sorted(site_user_ids)),
        counterparty_refs=tuple(sorted(counterparty_refs)),
        counterparty_hashes=tuple(sorted(counterparty_hashes)),
        source_updated_at=_parse_datetime(payload.get(CRM_UPDATED_AT_FIELD)),
        has_invalid_site_user_id=has_invalid_site_user_id,
        has_invalid_counterparty_ref=has_invalid_counterparty_ref,
    )


def onec_counterparty_identity_hash(counterparty_ref: str) -> str:
    normalized_ref = normalize_counterparty_ref(counterparty_ref)
    source = f"bitrix-crm-customer-audit-v1|onec-ref|{normalized_ref.lower()}"
    digest = hashlib.sha256(source.encode()).hexdigest()
    return digest[:24]


def resolve_crm_counterparty_hashes(
    rows: Iterable[CrmClusterSourceRow],
    *,
    onec_engine: Engine,
) -> tuple[CrmClusterSourceRow, ...]:
    source_rows = tuple(rows)
    required_hashes = {value for row in source_rows for value in row.counterparty_hashes}
    if not required_hashes:
        return source_rows

    refs_by_hash: dict[str, set[str]] = defaultdict(set)
    with onec_engine.connect() as connection:
        result = connection.execute(text("""
                SELECT CONVERT(varchar(34), _IDRRef, 1) AS counterparty_ref
                FROM dbo._Reference54
                WHERE _IDRRef <> 0x00000000000000000000000000000000
                """)).mappings()
        for item in result:
            counterparty_ref = normalize_counterparty_ref(str(item["counterparty_ref"]))
            value_hash = onec_counterparty_identity_hash(counterparty_ref)
            if value_hash in required_hashes:
                refs_by_hash[value_hash].add(counterparty_ref)

    resolved_rows: list[CrmClusterSourceRow] = []
    for row in source_rows:
        counterparty_refs = set(row.counterparty_refs)
        has_invalid_counterparty_ref = row.has_invalid_counterparty_ref
        for value_hash in row.counterparty_hashes:
            matches = refs_by_hash.get(value_hash, set())
            if len(matches) == 1:
                counterparty_refs.update(matches)
            else:
                has_invalid_counterparty_ref = True
        resolved_rows.append(
            replace(
                row,
                counterparty_refs=tuple(sorted(counterparty_refs)),
                has_invalid_counterparty_ref=has_invalid_counterparty_ref,
            )
        )
    return tuple(resolved_rows)


def build_mapping_entries(
    rows: Iterable[CrmClusterSourceRow],
) -> tuple[SettlementMappingInput, ...]:
    cluster_users: dict[str, set[str]] = defaultdict(set)
    cluster_counterparties: dict[str, set[str]] = defaultdict(set)
    cluster_updated_at: dict[str, datetime] = {}
    user_clusters: dict[str, set[str]] = defaultdict(set)
    users_without_cluster: set[str] = set()
    invalid_clusters: set[str] = set()
    users_with_invalid_unclustered_rows: set[str] = set()

    for row in rows:
        if row.cluster_id is None:
            users_without_cluster.update(row.site_user_ids)
            if row.has_invalid_site_user_id or row.has_invalid_counterparty_ref:
                users_with_invalid_unclustered_rows.update(row.site_user_ids)
            continue
        if row.has_invalid_site_user_id or row.has_invalid_counterparty_ref:
            invalid_clusters.add(row.cluster_id)
        cluster_users[row.cluster_id].update(row.site_user_ids)
        cluster_counterparties[row.cluster_id].update(row.counterparty_refs)
        if row.source_updated_at is not None:
            previous = cluster_updated_at.get(row.cluster_id)
            if previous is None or row.source_updated_at > previous:
                cluster_updated_at[row.cluster_id] = row.source_updated_at
        for site_user_id in row.site_user_ids:
            user_clusters[site_user_id].add(row.cluster_id)

    all_users = set(user_clusters) | users_without_cluster
    entries: list[SettlementMappingInput] = []
    for site_user_id in sorted(all_users):
        clusters = user_clusters.get(site_user_id, set())
        if site_user_id in users_with_invalid_unclustered_rows:
            status = MAPPING_AMBIGUOUS
            cluster_id = None
            counterparty_ref = None
            source_updated_at = None
        elif site_user_id in users_without_cluster and clusters:
            status = MAPPING_AMBIGUOUS
            cluster_id = None
            counterparty_ref = None
            source_updated_at = None
        elif not clusters:
            status = MAPPING_NOT_LINKED
            cluster_id = None
            counterparty_ref = None
            source_updated_at = None
        elif len(clusters) != 1:
            status = MAPPING_AMBIGUOUS
            cluster_id = None
            counterparty_ref = None
            source_updated_at = max(
                (cluster_updated_at[value] for value in clusters if value in cluster_updated_at),
                default=None,
            )
        else:
            cluster_id = next(iter(clusters))
            counterparties = cluster_counterparties.get(cluster_id, set())
            source_updated_at = cluster_updated_at.get(cluster_id)
            if cluster_id in invalid_clusters:
                status = MAPPING_AMBIGUOUS
                counterparty_ref = None
            elif len(counterparties) == 1:
                status = MAPPING_LINKED
                counterparty_ref = next(iter(counterparties))
            elif not counterparties:
                status = MAPPING_NOT_LINKED
                counterparty_ref = None
            else:
                status = MAPPING_AMBIGUOUS
                counterparty_ref = None
        entries.append(
            SettlementMappingInput(
                site_user_id=site_user_id,
                cluster_id=cluster_id,
                counterparty_ref=counterparty_ref,
                status=status,
                source_updated_at=source_updated_at,
            )
        )
    return tuple(entries)


def _post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise CustomerSettlementMappingSourceError("crm_mapping_source_unavailable") from exc
    if not isinstance(body, dict) or body.get("error"):
        raise CustomerSettlementMappingSourceError("crm_mapping_source_invalid_response")
    return body


def _contact_list_payload(start: int) -> dict[str, Any]:
    return {
        "order": {"ID": "ASC"},
        "filter": {f"!{CRM_SITE_USERS_FIELD}": False},
        "select": [
            "ID",
            CRM_CLUSTER_FIELD,
            CRM_SITE_USERS_FIELD,
            CRM_COUNTERPARTIES_FIELD,
            CRM_UPDATED_AT_FIELD,
            CRM_SOURCE_SYSTEMS_FIELD,
        ],
        "start": start,
    }


def _contact_list_batch_command(after_id: str) -> str:
    payload = _contact_list_payload(-1)
    query: list[tuple[str, str | int]] = [
        ("order[ID]", "ASC"),
        (f"filter[!{CRM_SITE_USERS_FIELD}]", "false"),
        ("filter[>ID]", after_id),
        ("start", -1),
    ]
    query.extend(("select[]", field) for field in payload["select"])
    return f"crm.contact.list?{urllib.parse.urlencode(query)}"


def _crm_total(value: Any) -> int:
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise CustomerSettlementMappingSourceError("crm_mapping_total_is_invalid") from exc
    if total < 0:
        raise CustomerSettlementMappingSourceError("crm_mapping_total_is_invalid")
    return total


def fetch_crm_cluster_rows(
    *,
    webhook_url: str,
    timeout_seconds: float,
) -> tuple[CrmClusterSourceRow, ...]:
    base_url = webhook_url.rstrip("/")
    url = f"{base_url}/crm.contact.list.json"
    raw_rows: list[dict[str, Any]] = []
    seen_row_ids: set[str] = set()
    last_row_id = 0

    def append_page(result: Any) -> None:
        nonlocal last_row_id
        if not isinstance(result, list):
            raise CustomerSettlementMappingSourceError("crm_mapping_result_is_not_list")
        for item in result:
            if not isinstance(item, dict):
                raise CustomerSettlementMappingSourceError("crm_mapping_row_is_invalid")
            row_id = str(item.get("ID") or "").strip()
            if not row_id or row_id in seen_row_ids:
                raise CustomerSettlementMappingSourceError("crm_mapping_duplicate_or_missing_id")
            try:
                numeric_row_id = int(row_id)
            except ValueError as exc:
                raise CustomerSettlementMappingSourceError(
                    "crm_mapping_row_without_numeric_id"
                ) from exc
            if numeric_row_id <= last_row_id:
                raise CustomerSettlementMappingSourceError(
                    "crm_mapping_pagination_order_is_invalid"
                )
            seen_row_ids.add(row_id)
            raw_rows.append(item)
            last_row_id = numeric_row_id

    first_body = _post_json(
        url,
        _contact_list_payload(0),
        timeout_seconds=timeout_seconds,
    )
    if first_body.get("total") is None:
        raise CustomerSettlementMappingSourceError("crm_mapping_total_is_missing")
    expected_total = _crm_total(first_body["total"])
    append_page(first_body.get("result"))
    first_page_ids = tuple(str(row["ID"]) for row in raw_rows)

    if len(raw_rows) < expected_total:
        next_start = first_body.get("next")
        if next_start in (None, ""):
            raise CustomerSettlementMappingSourceError("crm_mapping_incomplete_pagination")
        if str(next_start) == "0":
            raise CustomerSettlementMappingSourceError("crm_mapping_pagination_did_not_advance")

        remaining_pages = (expected_total - len(raw_rows) + CRM_PAGE_SIZE - 1) // CRM_PAGE_SIZE
        batch_url = f"{base_url}/batch.json"
        page_number = 1
        while page_number <= remaining_pages:
            batch_page_numbers = list(
                range(
                    page_number,
                    min(page_number + CRM_BATCH_PAGE_COUNT, remaining_pages + 1),
                )
            )
            commands: dict[str, str] = {}
            after_id = str(last_row_id)
            for current_page in batch_page_numbers:
                key = f"page_{current_page}"
                commands[key] = _contact_list_batch_command(after_id)
                after_id = f"$result[{key}][{CRM_PAGE_SIZE - 1}][ID]"
            batch_body = _post_json(
                batch_url,
                {"halt": 0, "cmd": commands},
                timeout_seconds=timeout_seconds,
            )
            batch_result = batch_body.get("result")
            if not isinstance(batch_result, dict):
                raise CustomerSettlementMappingSourceError("crm_mapping_batch_result_is_invalid")
            errors = batch_result.get("result_error")
            if errors:
                raise CustomerSettlementMappingSourceError("crm_mapping_batch_page_failed")
            pages = batch_result.get("result")
            if not isinstance(pages, dict) or set(pages) != set(commands):
                raise CustomerSettlementMappingSourceError("crm_mapping_incomplete_pagination")
            for current_page in batch_page_numbers:
                page = pages[f"page_{current_page}"]
                is_last_page = current_page == remaining_pages
                if not isinstance(page, list):
                    raise CustomerSettlementMappingSourceError("crm_mapping_result_is_not_list")
                if not is_last_page and len(page) != CRM_PAGE_SIZE:
                    raise CustomerSettlementMappingSourceError("crm_mapping_incomplete_pagination")
                append_page(page)
            page_number = batch_page_numbers[-1] + 1

    if len(raw_rows) != expected_total:
        raise CustomerSettlementMappingSourceError("crm_mapping_incomplete_pagination")
    verification_body = _post_json(
        url,
        _contact_list_payload(0),
        timeout_seconds=timeout_seconds,
    )
    if _crm_total(verification_body.get("total")) != expected_total:
        raise CustomerSettlementMappingSourceError("crm_mapping_total_changed_during_read")
    verification_rows = verification_body.get("result")
    if not isinstance(verification_rows, list):
        raise CustomerSettlementMappingSourceError("crm_mapping_result_is_not_list")
    if tuple(str(row.get("ID") or "") for row in verification_rows) != first_page_ids:
        raise CustomerSettlementMappingSourceError("crm_mapping_source_changed_during_read")
    return tuple(parse_crm_cluster_row(item) for item in raw_rows)
