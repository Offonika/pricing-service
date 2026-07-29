from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

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


class CustomerSettlementMappingSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrmClusterSourceRow:
    row_id: str
    cluster_id: str | None
    site_user_ids: tuple[str, ...]
    counterparty_refs: tuple[str, ...]
    source_updated_at: datetime | None


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
    site_user_ids = tuple(
        sorted({normalize_site_user_id(value) for value in _list_values(payload.get(CRM_SITE_USERS_FIELD))})
    )
    counterparty_refs = tuple(
        sorted(
            {
                normalize_counterparty_ref(value)
                for value in _list_values(payload.get(CRM_COUNTERPARTIES_FIELD))
            }
        )
    )
    return CrmClusterSourceRow(
        row_id=row_id,
        cluster_id=cluster_id,
        site_user_ids=site_user_ids,
        counterparty_refs=counterparty_refs,
        source_updated_at=_parse_datetime(payload.get(CRM_UPDATED_AT_FIELD)),
    )


def build_mapping_entries(
    rows: Iterable[CrmClusterSourceRow],
) -> tuple[SettlementMappingInput, ...]:
    cluster_users: dict[str, set[str]] = defaultdict(set)
    cluster_counterparties: dict[str, set[str]] = defaultdict(set)
    cluster_updated_at: dict[str, datetime] = {}
    user_clusters: dict[str, set[str]] = defaultdict(set)
    users_without_cluster: set[str] = set()

    for row in rows:
        if row.cluster_id is None:
            users_without_cluster.update(row.site_user_ids)
            continue
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
        if site_user_id in users_without_cluster and clusters:
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
            if len(counterparties) == 1:
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
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CustomerSettlementMappingSourceError("crm_mapping_source_unavailable") from exc
    if not isinstance(body, dict) or body.get("error"):
        raise CustomerSettlementMappingSourceError("crm_mapping_source_invalid_response")
    return body


def fetch_crm_cluster_rows(
    *,
    webhook_url: str,
    timeout_seconds: float,
) -> tuple[CrmClusterSourceRow, ...]:
    base_url = webhook_url.rstrip("/")
    url = f"{base_url}/crm.contact.list.json"
    start: int | str = 0
    raw_rows: list[dict[str, Any]] = []
    seen_row_ids: set[str] = set()
    expected_total: int | None = None

    while True:
        body = _post_json(
            url,
            {
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
            },
            timeout_seconds=timeout_seconds,
        )
        result = body.get("result")
        if not isinstance(result, list):
            raise CustomerSettlementMappingSourceError("crm_mapping_result_is_not_list")
        if expected_total is None and body.get("total") is not None:
            try:
                expected_total = int(body["total"])
            except (TypeError, ValueError) as exc:
                raise CustomerSettlementMappingSourceError("crm_mapping_total_is_invalid") from exc
        for item in result:
            if not isinstance(item, dict):
                raise CustomerSettlementMappingSourceError("crm_mapping_row_is_invalid")
            row_id = str(item.get("ID") or "").strip()
            if not row_id or row_id in seen_row_ids:
                raise CustomerSettlementMappingSourceError("crm_mapping_duplicate_or_missing_id")
            seen_row_ids.add(row_id)
            raw_rows.append(item)
        next_start = body.get("next")
        if next_start in (None, ""):
            break
        if str(next_start) == str(start):
            raise CustomerSettlementMappingSourceError("crm_mapping_pagination_did_not_advance")
        start = next_start

    if expected_total is not None and len(raw_rows) != expected_total:
        raise CustomerSettlementMappingSourceError("crm_mapping_incomplete_pagination")
    return tuple(parse_crm_cluster_row(item) for item in raw_rows)
