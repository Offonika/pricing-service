"""Build the immutable family-registry successor after applying display scope policy.

The task is local and deterministic. It reads one already accepted inventory,
writes a separate bundle, and never connects to a database or external system.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.services.display_scope_policy import (
    DISPLAY_SCOPE_POLICY_VERSION,
    EXCLUDED_DISPLAY_NAME_BITOK,
    filter_display_scope_records,
)

SOURCE_BUNDLE = Path("reports/assortment_lifecycle/display-family-registry-preflight-v2-2026-08-16")
OUTPUT_BUNDLE = Path(
    "reports/assortment_lifecycle/display-family-registry-scope-policy-v1-2026-08-16"
)
EXPECTED_SOURCE_INVENTORY_SHA256 = (
    "d13a7883d68c4c49e948b4f7aecaf4fe989cb8cdacf2c13eedce906efee9ef5f"
)
EXPECTED_SOURCE_INVENTORY_CHECKSUM = (
    "0331da1b96d9582f32ee1f8c72e9d6566ac66b5fb74f8e3685fe7a70ade7319e"
)
EXPECTED_SOURCE_MEMBER_COUNT = 2689
EXPECTED_EXCLUDED_COUNT = 11
EXPECTED_TARGET_MEMBER_COUNT = 2678


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_checksum(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _clean_counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted((key, value) for key, value in counter.items() if value > 0))


def _summary_for_items(
    source_summary: Mapping[str, Any],
    included: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    family_counts = Counter(str(row.get("proposed_family_id") or "") for row in included)
    status_counts = Counter(str(row.get("proposal_status") or "") for row in included)
    warning_counts = Counter(
        str(warning) for row in included for warning in (row.get("proposal_warnings") or ())
    )
    note_counts = Counter(
        str(note) for row in included for note in (row.get("proposal_notes") or ())
    )
    matching_relation_counts: Counter[str] = Counter()
    matching_property_counts: Counter[str] = Counter()
    for row in included:
        audit = row.get("matching_audit") or {}
        matching_relation_counts.update(audit.get("relation_counts") or {})
        matching_property_counts.update(audit.get("property_disagreement_counts") or {})

    classification_counts = Counter(source_summary.get("display_scope_reason_counts") or {})
    classification_warning_counts = Counter(
        source_summary.get("display_scope_warning_counts") or {}
    )
    for row in excluded:
        reason = str(row.get("scope_classification_reason") or "")
        if reason:
            classification_counts[reason] -= 1
        classification_warning_counts.subtract(row.get("scope_classification_warnings") or ())

    result = dict(source_summary)
    result.update(
        {
            "included_display_sku_count": len(included),
            "excluded_scope_policy_count": len(excluded),
            "excluded_scope_policy_reason_counts": {EXCLUDED_DISPLAY_NAME_BITOK: len(excluded)},
            "proposed_family_count": len(family_counts),
            "multi_sku_family_count": sum(value > 1 for value in family_counts.values()),
            "manual_review_sku_count": sum(
                bool(row.get("requires_manual_review")) for row in included
            ),
            "status_counts": _clean_counter(status_counts),
            "warning_counts": _clean_counter(warning_counts),
            "note_counts": _clean_counter(note_counts),
            "display_scope_reason_counts": _clean_counter(classification_counts),
            "display_scope_warning_counts": _clean_counter(classification_warning_counts),
            "accepted_matching_link_count": sum(
                int((row.get("matching_audit") or {}).get("accepted_count") or 0)
                for row in included
            ),
            "manual_accepted_matching_link_count": sum(
                int((row.get("matching_audit") or {}).get("manual_accepted_count") or 0)
                for row in included
            ),
            "matching_review_sku_count": sum(
                bool((row.get("matching_audit") or {}).get("requires_review")) for row in included
            ),
            "matching_relation_counts": _clean_counter(matching_relation_counts),
            "matching_property_disagreement_counts": _clean_counter(matching_property_counts),
        }
    )
    return result


def build_successor_inventory(
    source_inventory: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw_items = source_inventory.get("items")
    if not isinstance(raw_items, list) or not all(isinstance(item, Mapping) for item in raw_items):
        raise ValueError("source inventory items must be a list of objects")
    if len(raw_items) != EXPECTED_SOURCE_MEMBER_COUNT:
        raise ValueError(
            f"source member count mismatch: {len(raw_items)} != {EXPECTED_SOURCE_MEMBER_COUNT}"
        )
    scope_result = filter_display_scope_records(raw_items)
    included = [dict(item) for item in scope_result.included]
    excluded_codes = {str(row.get("nomenclature_code") or "") for row in scope_result.exclusions}
    excluded = [
        dict(item)
        for item in raw_items
        if str(item.get("nomenclature_code") or "") in excluded_codes
    ]
    if scope_result.audit["excluded_item_count"] != EXPECTED_EXCLUDED_COUNT:
        raise ValueError(
            "scope exclusion count mismatch: "
            f"{scope_result.audit['excluded_item_count']} != {EXPECTED_EXCLUDED_COUNT}"
        )
    if len(included) != EXPECTED_TARGET_MEMBER_COUNT:
        raise ValueError(
            f"target member count mismatch: {len(included)} != {EXPECTED_TARGET_MEMBER_COUNT}"
        )

    source_scope_audit = source_inventory.get("scope_audit") or {}
    excluded_product_ids = {int(item["product_id"]) for item in excluded}
    conflicts = [
        dict(conflict)
        for conflict in (source_scope_audit.get("conflicts") or ())
        if int(conflict.get("product_id") or 0) not in excluded_product_ids
    ]
    source_inventory_checksum = str(source_inventory.get("inventory_checksum") or "")
    inventory_core = {
        key: value
        for key, value in source_inventory.items()
        if key not in {"inventory_checksum", "items", "summary", "scope_audit"}
    }
    source_quality = inventory_core.pop("source_quality", {})
    source_warnings = inventory_core.pop("source_warnings", [])
    payload: dict[str, Any] = {
        **inventory_core,
        "summary": _summary_for_items(source_inventory.get("summary") or {}, included, excluded),
        "scope_audit": {
            **scope_result.audit,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        },
        "scope_transition": {
            "schema": "display_family_registry_scope_transition.v1",
            "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
            "source_inventory_checksum": source_inventory_checksum,
            "source_inventory_sha256": str(
                (source_manifest.get("artifact_sha256") or {}).get("inventory.json") or ""
            ),
            "source_member_count": len(raw_items),
            "excluded_member_count": len(excluded),
            "target_member_count": len(included),
            "excluded_reason_counts": scope_result.audit["excluded_reason_counts"],
        },
        "items": included,
    }
    payload["inventory_checksum"] = _canonical_checksum(payload)
    payload["source_quality"] = source_quality
    payload["source_warnings"] = source_warnings
    return payload, [dict(row) for row in scope_result.exclusions]


_CSV_FIELDS = (
    "product_id",
    "nomenclature_code",
    "article",
    "name",
    "scope_reasons",
    "scope_classification_reason",
    "scope_classification_warnings",
    "last_sale_at",
    "current_stock_qty",
    "phone_model_ids",
    "phone_models",
    "quality",
    "display_type",
    "construction",
    "has_frame",
    "has_ic_pad",
    "segment_id",
    "proposed_family_id",
    "proposal_status",
    "proposal_warnings",
    "proposal_notes",
    "accepted_matching_count",
    "accepted_matching_relations",
    "accepted_matching_warnings",
    "requires_manual_review",
    "identity_schema_version",
    "identity_rules_version",
    "available_at_status",
)


def _flatten_item(row: Mapping[str, Any]) -> dict[str, Any]:
    matching_audit = row.get("matching_audit") or {}
    return {
        "product_id": row.get("product_id"),
        "nomenclature_code": row.get("nomenclature_code"),
        "article": row.get("article"),
        "name": row.get("name"),
        "scope_reasons": ";".join(row.get("scope_reasons") or []),
        "scope_classification_reason": row.get("scope_classification_reason"),
        "scope_classification_warnings": ";".join(row.get("scope_classification_warnings") or []),
        "last_sale_at": row.get("last_sale_at"),
        "current_stock_qty": row.get("current_stock_qty"),
        "phone_model_ids": ";".join(str(value) for value in row.get("phone_model_ids") or []),
        "phone_models": "; ".join(
            " ".join(
                str(value or "").strip()
                for value in (
                    model.get("brand"),
                    model.get("model_name"),
                    model.get("variant"),
                )
                if str(value or "").strip()
            )
            for model in row.get("phone_models") or []
        ),
        "quality": row.get("quality"),
        "display_type": row.get("display_type"),
        "construction": row.get("construction"),
        "has_frame": row.get("has_frame"),
        "has_ic_pad": row.get("has_ic_pad"),
        "segment_id": row.get("segment_id"),
        "proposed_family_id": row.get("proposed_family_id"),
        "proposal_status": row.get("proposal_status"),
        "proposal_warnings": ";".join(row.get("proposal_warnings") or []),
        "proposal_notes": ";".join(row.get("proposal_notes") or []),
        "accepted_matching_count": matching_audit.get("accepted_count", 0),
        "accepted_matching_relations": ";".join(
            f"{key}={value}"
            for key, value in sorted((matching_audit.get("relation_counts") or {}).items())
        ),
        "accepted_matching_warnings": ";".join(matching_audit.get("warnings") or []),
        "requires_manual_review": row.get("requires_manual_review"),
        "identity_schema_version": row.get("identity_schema_version"),
        "identity_rules_version": row.get("identity_rules_version"),
        "available_at_status": row.get("available_at_status"),
    }


def _write_csv(path: Path, items: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_flatten_item(item) for item in items)
    os.replace(temporary, path)


def _html_report(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    rows = []
    for item in payload["items"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['nomenclature_code']))}</td>"
            f"<td>{html.escape(str(item['name']))}</td>"
            f"<td>{html.escape(str(item['proposed_family_id']))}</td>"
            f"<td>{html.escape(str(item['segment_id']))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Реестр семейств после scope-фильтра</title>
<style>body{{font:14px system-ui;margin:24px;color:#17202a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd1d1;padding:6px;vertical-align:top}}th{{background:#f4f6f7;position:sticky;top:0}}code{{background:#f4f6f7;padding:2px 4px}}</style></head>
<body><h1>Реестр семейств после scope-фильтра</h1>
<p>Политика: <code>{DISPLAY_SCOPE_POLICY_VERSION}</code>; исключено: <strong>{payload['scope_audit']['excluded_item_count']}</strong>; осталось SKU: <strong>{summary['included_display_sku_count']}</strong>; семей: <strong>{summary['proposed_family_count']}</strong>.</p>
<p>Построение артефакта не создаёт заказы, не пишет в 1С и не изменяет production.</p>
<table><thead><tr><th>Код</th><th>Товар</th><th>Семья</th><th>Сегмент</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""


def write_successor_bundle(
    output_dir: Path,
    *,
    payload: dict[str, Any],
    exclusions: Sequence[Mapping[str, Any]],
    source_bundle: Path,
    source_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = output_dir / "inventory.json"
    csv_path = output_dir / "inventory.csv"
    report_path = output_dir / "report.html"
    exclusions_path = output_dir / "exclusions.json"
    _atomic_write(
        inventory_path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
    )
    _write_csv(csv_path, payload["items"])
    _atomic_write(report_path, _html_report(payload))
    _atomic_write(
        exclusions_path,
        json.dumps(
            {
                "schema": "display_scope_exclusions.v1",
                "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
                "items": list(exclusions),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )
    source_quality = payload.get("source_quality") or {}
    artifact_hashes = {
        name: _sha256(output_dir / name)
        for name in ("inventory.json", "inventory.csv", "report.html", "exclusions.json")
    }
    manifest = {
        "schema": "display_family_registry_preflight_manifest.v2",
        "as_of": payload["as_of"],
        "inventory_checksum": payload["inventory_checksum"],
        "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
        "scope_excluded_count": len(exclusions),
        "scope_excluded_reason_counts": {EXCLUDED_DISPLAY_NAME_BITOK: len(exclusions)},
        "source_quality_checksum": _canonical_checksum(source_quality),
        "source_quality_status": source_quality.get("status"),
        "source_gates": source_quality.get("gates") or {},
        "status": "complete_read_only",
        "production_authorized": False,
        "external_writes": False,
        "artifact_sha256": artifact_hashes,
        "derived_from": {
            "source_bundle_path": str(source_bundle),
            "source_inventory_checksum": source_manifest.get("inventory_checksum"),
            "source_artifact_sha256": dict(source_manifest.get("artifact_sha256") or {}),
            "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
        },
    }
    _atomic_write(
        output_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return manifest


def build_bundle(source_bundle: Path, output_bundle: Path) -> dict[str, Any]:
    source_bundle = source_bundle.resolve()
    output_bundle = output_bundle.resolve()
    if source_bundle == output_bundle:
        raise ValueError("output bundle must differ from source bundle")
    source_inventory_path = source_bundle / "inventory.json"
    source_manifest_path = source_bundle / "manifest.json"
    if _sha256(source_inventory_path) != EXPECTED_SOURCE_INVENTORY_SHA256:
        raise ValueError("source inventory SHA-256 mismatch")
    source_inventory = json.loads(source_inventory_path.read_text(encoding="utf-8-sig"))
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8-sig"))
    if source_inventory.get("inventory_checksum") != EXPECTED_SOURCE_INVENTORY_CHECKSUM:
        raise ValueError("source inventory checksum mismatch")
    if (source_manifest.get("artifact_sha256") or {}).get(
        "inventory.json"
    ) != EXPECTED_SOURCE_INVENTORY_SHA256:
        raise ValueError("source manifest inventory SHA-256 mismatch")
    for filename, expected_hash in (source_manifest.get("artifact_sha256") or {}).items():
        if _sha256(source_bundle / filename) != expected_hash:
            raise ValueError(f"source artifact SHA-256 mismatch: {filename}")
    if source_manifest.get("status") != "complete_read_only":
        raise ValueError("source bundle is not accepted read-only evidence")
    if any(
        gate.get("status") != "pass"
        for gate in (source_manifest.get("source_gates") or {}).values()
    ):
        raise ValueError("source bundle contains a failed source gate")

    payload, exclusions = build_successor_inventory(
        source_inventory, source_manifest=source_manifest
    )
    return write_successor_bundle(
        output_bundle,
        payload=payload,
        exclusions=exclusions,
        source_bundle=source_bundle,
        source_manifest=source_manifest,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, default=SOURCE_BUNDLE)
    parser.add_argument("--output-bundle", type=Path, default=OUTPUT_BUNDLE)
    args = parser.parse_args()
    manifest = build_bundle(args.source_bundle, args.output_bundle)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
