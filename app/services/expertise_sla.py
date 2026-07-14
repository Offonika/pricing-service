from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_GEO_GROUP = "other"
DEFAULT_DELIVERY_DAYS_MAP: dict[str, int] = {
    "moscow": 2,
    "spb": 8,
    "other": 8,
}
DEFAULT_REVIEW_DAYS_MAP: dict[str, int] = {
    "moscow": 3,
    "spb": 14,
    "other": 14,
}


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def resolve_geo_group(
    *,
    store_external_id: str | None,
    store_group_map: dict[str, str] | None,
) -> tuple[str, bool]:
    normalized_map = dict(store_group_map or {})
    if store_external_id:
        candidate = normalized_map.get(store_external_id)
        if candidate:
            normalized = str(candidate).strip().lower()
            if normalized in DEFAULT_DELIVERY_DAYS_MAP:
                return normalized, False
    return DEFAULT_GEO_GROUP, True


def _resolve_days(days_map: dict[str, int] | None, geo_group: str, defaults: dict[str, int]) -> int:
    normalized_map = dict(defaults)
    normalized_map.update({str(key): int(value) for key, value in (days_map or {}).items()})
    return int(normalized_map.get(geo_group, normalized_map[DEFAULT_GEO_GROUP]))


def delivery_deadline_at(
    *,
    anchor_at: datetime,
    store_external_id: str | None,
    store_group_map: dict[str, str] | None,
    delivery_days_map: dict[str, int] | None,
) -> tuple[datetime, str, bool]:
    geo_group, is_fallback = resolve_geo_group(
        store_external_id=store_external_id,
        store_group_map=store_group_map,
    )
    days = _resolve_days(delivery_days_map, geo_group, DEFAULT_DELIVERY_DAYS_MAP)
    return normalize_datetime(anchor_at) + timedelta(days=days), geo_group, is_fallback


def review_deadline_at(
    *,
    anchor_at: datetime,
    store_external_id: str | None,
    store_group_map: dict[str, str] | None,
    review_days_map: dict[str, int] | None,
) -> tuple[datetime, str, bool]:
    geo_group, is_fallback = resolve_geo_group(
        store_external_id=store_external_id,
        store_group_map=store_group_map,
    )
    days = _resolve_days(review_days_map, geo_group, DEFAULT_REVIEW_DAYS_MAP)
    return normalize_datetime(anchor_at) + timedelta(days=days), geo_group, is_fallback
