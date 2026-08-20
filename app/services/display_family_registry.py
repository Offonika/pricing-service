"""Versioned display-family registry bootstrap, readback, rollback and read models."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.display_family_registry import (
    DisplayFamily,
    DisplayFamilyDecisionEvent,
    DisplayFamilyMember,
    DisplayFamilyRegistryVersion,
)
from app.models.product import Product
from app.services.display_scope_policy import (
    DISPLAY_SCOPE_POLICY_VERSION,
    EXCLUDED_DISPLAY_NAME_BITOK,
    display_scope_exclusion_reason,
)
from app.services.query_batching import normalized_text_batches

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APPROVED_BUNDLE_PATH = (
    REPOSITORY_ROOT
    / "reports/assortment_lifecycle/display-family-registry-scope-policy-v1-2026-08-16"
)
APPROVED_INVENTORY_CHECKSUM = "8d62c30659f79682a745d64080cc86a20025b3c39e0e16aef2e0fadba718027b"
APPROVED_ARTIFACT_SHA256 = {
    "inventory.json": "5328749ef6f73435a952c9896d6d56cd57562740252b00a548b4029fe982be38",
    "inventory.csv": "309b777553072753752fc9622b9ec98d5fa111cb5ee79360503df49376a24ffe",
    "report.html": "5ae337d90c95c29c54c4be42ade5f871832052803efec1283ea1e78869ea1f33",
    "exclusions.json": "0d54b98c55377df081311140e33a4cfeb9b0b6712c32002d602d7524995bf4f3",
}
REQUIRED_SOURCE_GATES = {
    "accepted_matching_readable",
    "application_catalog_nonempty",
    "current_stock_snapshot_fresh_for_as_of",
    "current_stock_snapshot_nonempty",
    "lifecycle_history_readable",
    "procurement_orders_readable",
}
INITIAL_BOOTSTRAP_REASON = "Первичная активация принятого preflight v2"


class DisplayFamilyRegistryError(ValueError):
    """Fail-closed validation or state transition error."""


@dataclass(frozen=True)
class ActiveDisplayFamilyMemberContext:
    """Read-only active-registry projection used by downstream calculations."""

    registry_version_id: int
    registry_version_number: int
    registry_inventory_checksum: str
    family_record_id: int
    family_key: str
    family_label: str
    family_member_count: int
    family_review_member_count: int
    family_matching_review_member_count: int
    family_warning_codes: tuple[str, ...]
    product_id: int
    segment_id: str
    quality_segment: str
    construction_segment: str
    requires_manual_review: bool
    member_warning_codes: tuple[str, ...]
    matching_evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ApprovedBundleContract:
    bundle_path: Path
    as_of: date
    manifest_schema: str
    inventory_schema: str
    inventory_checksum: str
    artifact_sha256: Mapping[str, str]
    expected_member_count: int
    expected_family_count: int
    required_scope_policy_version: str | None = None
    expected_excluded_count: int | None = None


APPROVED_BUNDLE_CONTRACT = ApprovedBundleContract(
    bundle_path=APPROVED_BUNDLE_PATH,
    as_of=date(2026, 8, 16),
    manifest_schema="display_family_registry_preflight_manifest.v2",
    inventory_schema="display_family_inventory.v2",
    inventory_checksum=APPROVED_INVENTORY_CHECKSUM,
    artifact_sha256=APPROVED_ARTIFACT_SHA256,
    expected_member_count=2678,
    expected_family_count=1380,
    required_scope_policy_version=DISPLAY_SCOPE_POLICY_VERSION,
    expected_excluded_count=11,
)


@dataclass(frozen=True)
class PreparedDisplayFamilyBundle:
    path: Path
    manifest: dict[str, Any]
    inventory: dict[str, Any]
    items: tuple[dict[str, Any], ...]
    family_items: Mapping[str, tuple[dict[str, Any], ...]]
    effective_from: date
    membership_checksum: str


@dataclass(frozen=True)
class DisplayFamilyBootstrapPlan:
    ready: bool
    registry_schema_ready: bool
    action: str
    idempotent: bool
    version_number: int | None
    active_version_number: int | None
    replaces_version_number: int | None
    existing_checksum_version_number: int | None
    expected_family_count: int
    expected_member_count: int
    existing_product_count: int
    missing_product_ids: tuple[int, ...]
    blockers: tuple[str, ...]
    inventory_checksum: str
    membership_checksum: str
    bundle_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "display_family_registry_bootstrap_plan.v1",
            "ready": self.ready,
            "registry_schema_ready": self.registry_schema_ready,
            "action": self.action,
            "idempotent": self.idempotent,
            "version_number": self.version_number,
            "active_version_number": self.active_version_number,
            "replaces_version_number": self.replaces_version_number,
            "existing_checksum_version_number": self.existing_checksum_version_number,
            "expected_family_count": self.expected_family_count,
            "expected_member_count": self.expected_member_count,
            "existing_product_count": self.existing_product_count,
            "missing_product_ids": list(self.missing_product_ids),
            "blockers": list(self.blockers),
            "inventory_checksum": self.inventory_checksum,
            "membership_checksum": self.membership_checksum,
            "bundle_path": self.bundle_path,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DisplayFamilyRegistryError(f"cannot read valid JSON from {path}") from exc
    if not isinstance(payload, dict):
        raise DisplayFamilyRegistryError(f"expected JSON object in {path}")
    return payload


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _membership_checksum(items: Iterable[Mapping[str, Any]]) -> str:
    rows = sorted(
        (
            str(item.get("proposed_family_id") or ""),
            int(item.get("product_id") or 0),
            str(item.get("segment_id") or ""),
        )
        for item in items
    )
    digest = hashlib.sha256()
    for family_key, product_id, segment_id in rows:
        digest.update(f"{family_key}\t{product_id}\t{segment_id}\n".encode())
    return digest.hexdigest()


def load_approved_display_family_bundle(
    bundle_path: Path = APPROVED_BUNDLE_PATH,
    *,
    contract: ApprovedBundleContract = APPROVED_BUNDLE_CONTRACT,
) -> PreparedDisplayFamilyBundle:
    """Load only the exact accepted bundle and validate every immutable gate."""

    resolved_path = bundle_path.resolve()
    if resolved_path != contract.bundle_path.resolve():
        raise DisplayFamilyRegistryError(
            f"bundle path is not approved: {resolved_path}; expected {contract.bundle_path.resolve()}"
        )
    if not resolved_path.is_dir():
        raise DisplayFamilyRegistryError(
            f"approved bundle directory does not exist: {resolved_path}"
        )

    required_files = {"manifest.json", *contract.artifact_sha256.keys()}
    missing_files = sorted(name for name in required_files if not (resolved_path / name).is_file())
    if missing_files:
        raise DisplayFamilyRegistryError(
            f"approved bundle is incomplete: missing {', '.join(missing_files)}"
        )

    manifest = _load_json(resolved_path / "manifest.json")
    inventory = _load_json(resolved_path / "inventory.json")
    checks: tuple[tuple[bool, str], ...] = (
        (manifest.get("schema") == contract.manifest_schema, "manifest schema mismatch"),
        (manifest.get("status") == "complete_read_only", "manifest status is not complete"),
        (manifest.get("external_writes") is False, "manifest external_writes must be false"),
        (
            manifest.get("production_authorized") is False,
            "manifest production_authorized must be false",
        ),
        (manifest.get("source_quality_status") == "ready", "source quality is not ready"),
        (
            manifest.get("inventory_checksum") == contract.inventory_checksum,
            "manifest inventory checksum mismatch",
        ),
        (manifest.get("as_of") == contract.as_of.isoformat(), "manifest as_of mismatch"),
        (inventory.get("schema") == contract.inventory_schema, "inventory schema mismatch"),
        (
            inventory.get("inventory_checksum") == contract.inventory_checksum,
            "inventory checksum mismatch",
        ),
        (inventory.get("as_of") == contract.as_of.isoformat(), "inventory as_of mismatch"),
    )
    errors = [message for valid, message in checks if not valid]

    scope_exclusions: list[dict[str, Any]] = []
    if contract.required_scope_policy_version is not None:
        policy_version = contract.required_scope_policy_version
        expected_excluded = int(contract.expected_excluded_count or 0)
        if manifest.get("scope_policy_version") != policy_version:
            errors.append("manifest scope policy version mismatch")
        if manifest.get("scope_excluded_count") != expected_excluded:
            errors.append("manifest scope excluded count mismatch")
        if manifest.get("scope_excluded_reason_counts") != {
            EXCLUDED_DISPLAY_NAME_BITOK: expected_excluded
        }:
            errors.append("manifest scope excluded reason counts mismatch")
        scope_audit = inventory.get("scope_audit")
        if not isinstance(scope_audit, dict):
            errors.append("inventory scope audit is missing")
        else:
            if scope_audit.get("scope_policy_version") != policy_version:
                errors.append("inventory scope policy version mismatch")
            if scope_audit.get("source_item_count") != (
                contract.expected_member_count + expected_excluded
            ):
                errors.append("inventory scope source count mismatch")
            if scope_audit.get("included_item_count") != contract.expected_member_count:
                errors.append("inventory scope included count mismatch")
            if scope_audit.get("excluded_item_count") != expected_excluded:
                errors.append("inventory scope excluded count mismatch")
            if scope_audit.get("excluded_reason_counts") != {
                EXCLUDED_DISPLAY_NAME_BITOK: expected_excluded
            }:
                errors.append("inventory scope excluded reason counts mismatch")
            raw_scope_exclusions = scope_audit.get("exclusions")
            if isinstance(raw_scope_exclusions, list) and all(
                isinstance(row, dict) for row in raw_scope_exclusions
            ):
                scope_exclusions = [dict(row) for row in raw_scope_exclusions]
            else:
                errors.append("inventory scope exclusions are invalid")
        scope_transition = inventory.get("scope_transition")
        if not isinstance(scope_transition, dict):
            errors.append("inventory scope transition is missing")
        else:
            if scope_transition.get("scope_policy_version") != policy_version:
                errors.append("inventory scope transition policy mismatch")
            if scope_transition.get("source_member_count") != (
                contract.expected_member_count + expected_excluded
            ):
                errors.append("inventory scope transition source count mismatch")
            if scope_transition.get("excluded_member_count") != expected_excluded:
                errors.append("inventory scope transition excluded count mismatch")
            if scope_transition.get("target_member_count") != contract.expected_member_count:
                errors.append("inventory scope transition target count mismatch")

    source_gates = manifest.get("source_gates")
    if not isinstance(source_gates, dict):
        errors.append("manifest source_gates are missing")
    else:
        for gate in sorted(REQUIRED_SOURCE_GATES):
            state = source_gates.get(gate)
            if not isinstance(state, dict) or state.get("status") != "pass":
                errors.append(f"source gate did not pass: {gate}")

    manifest_hashes = manifest.get("artifact_sha256")
    if not isinstance(manifest_hashes, dict):
        errors.append("manifest artifact_sha256 is missing")
        manifest_hashes = {}
    for filename, expected_hash in sorted(contract.artifact_sha256.items()):
        manifest_hash = manifest_hashes.get(filename)
        actual_hash = _sha256(resolved_path / filename)
        if manifest_hash != expected_hash:
            errors.append(f"manifest SHA-256 mismatch for {filename}")
        if actual_hash != expected_hash:
            errors.append(f"file SHA-256 mismatch for {filename}")

    if contract.required_scope_policy_version is not None:
        exclusions_payload = _load_json(resolved_path / "exclusions.json")
        if exclusions_payload.get("schema") != "display_scope_exclusions.v1":
            errors.append("exclusions artifact schema mismatch")
        if exclusions_payload.get("scope_policy_version") != (
            contract.required_scope_policy_version
        ):
            errors.append("exclusions artifact policy version mismatch")
        if exclusions_payload.get("items") != scope_exclusions:
            errors.append("exclusions artifact does not match inventory scope audit")

    raw_items = inventory.get("items")
    if not isinstance(raw_items, list):
        errors.append("inventory items must be a list")
        raw_items = []
    items: list[dict[str, Any]] = []
    product_ids: list[int] = []
    leaked_scope_product_ids: list[int] = []
    family_items: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for offset, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            errors.append(f"inventory item {offset} is not an object")
            continue
        try:
            product_id = int(raw_item.get("product_id"))
        except (TypeError, ValueError):
            errors.append(f"inventory item {offset} has invalid product_id")
            continue
        family_key = str(raw_item.get("proposed_family_id") or "").strip()
        segment_id = str(raw_item.get("segment_id") or "").strip()
        if product_id <= 0:
            errors.append(f"inventory item {offset} has non-positive product_id")
        if not family_key or len(family_key) > 80:
            errors.append(f"inventory item {offset} has invalid proposed_family_id")
        if not segment_id or len(segment_id) > 160:
            errors.append(f"inventory item {offset} has invalid segment_id")
        item = dict(raw_item)
        item["product_id"] = product_id
        if (
            contract.required_scope_policy_version is not None
            and display_scope_exclusion_reason(item.get("name")) is not None
        ):
            leaked_scope_product_ids.append(product_id)
        product_ids.append(product_id)
        items.append(item)
        family_items[family_key].append(item)

    duplicate_product_ids = sorted(
        product_id for product_id, count in Counter(product_ids).items() if count > 1
    )
    if duplicate_product_ids:
        errors.append(
            "duplicate product membership: "
            + ", ".join(str(value) for value in duplicate_product_ids[:20])
        )
    if leaked_scope_product_ids:
        errors.append(
            "scope-excluded products leaked into membership: "
            + ", ".join(str(value) for value in leaked_scope_product_ids[:20])
        )
    if len(items) != contract.expected_member_count:
        errors.append(f"member count mismatch: {len(items)} != {contract.expected_member_count}")
    if len(family_items) != contract.expected_family_count:
        errors.append(
            f"family count mismatch: {len(family_items)} != {contract.expected_family_count}"
        )
    summary = inventory.get("summary")
    if not isinstance(summary, dict):
        errors.append("inventory summary is missing")
    else:
        if summary.get("included_display_sku_count") != contract.expected_member_count:
            errors.append("summary included_display_sku_count mismatch")
        if summary.get("proposed_family_count") != contract.expected_family_count:
            errors.append("summary proposed_family_count mismatch")

    if errors:
        raise DisplayFamilyRegistryError("; ".join(sorted(set(errors))))

    frozen_family_items = {
        key: tuple(sorted(rows, key=lambda item: int(item["product_id"])))
        for key, rows in sorted(family_items.items())
    }
    return PreparedDisplayFamilyBundle(
        path=resolved_path,
        manifest=manifest,
        inventory=inventory,
        items=tuple(sorted(items, key=lambda item: int(item["product_id"]))),
        family_items=frozen_family_items,
        effective_from=contract.as_of,
        membership_checksum=_membership_checksum(items),
    )


def _chunks(values: Sequence[int], size: int = 500) -> Iterable[Sequence[int]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def active_display_family_registry_version(
    session: Session,
) -> DisplayFamilyRegistryVersion | None:
    return session.scalar(
        select(DisplayFamilyRegistryVersion).where(DisplayFamilyRegistryVersion.status == "active")
    )


def load_active_display_family_member_contexts(
    session: Session,
    *,
    nomenclature_codes: Sequence[str],
) -> dict[str, ActiveDisplayFamilyMemberContext]:
    """Load the verified active family membership keyed by the 1C item code.

    Family identity is read only from the active registry.  Product names are
    never parsed here, so downstream calculations cannot silently create a
    parallel family classifier.
    """

    version = active_display_family_registry_version(session)
    if version is None:
        raise DisplayFamilyRegistryError("active display-family registry version is missing")
    readback = readback_display_family_registry_version(session, version)
    if not readback["ok"]:
        raise DisplayFamilyRegistryError(
            "active display-family registry readback failed: "
            + ", ".join(str(value) for value in readback["errors"])
        )

    result: dict[str, ActiveDisplayFamilyMemberContext] = {}
    for code_batch in normalized_text_batches(nomenclature_codes):
        rows = session.execute(
            select(
                Product.code_1c,
                DisplayFamilyMember.product_id,
                DisplayFamilyMember.segment_id,
                DisplayFamilyMember.quality_segment,
                DisplayFamilyMember.construction_segment,
                DisplayFamilyMember.requires_manual_review,
                DisplayFamilyMember.warning_codes_json.label("member_warning_codes"),
                DisplayFamilyMember.matching_evidence_json,
                DisplayFamily.id,
                DisplayFamily.family_key,
                DisplayFamily.member_count,
                DisplayFamily.review_member_count,
                DisplayFamily.matching_review_member_count,
                DisplayFamily.warning_codes_json.label("family_warning_codes"),
                DisplayFamily.phone_models_json,
            )
            .join(Product, Product.id == DisplayFamilyMember.product_id)
            .join(DisplayFamily, DisplayFamily.id == DisplayFamilyMember.family_id)
            .where(
                DisplayFamilyMember.registry_version_id == version.id,
                Product.code_1c.in_(code_batch),
            )
            .order_by(Product.code_1c, DisplayFamilyMember.product_id)
        ).all()
        for row in rows:
            code = str(row.code_1c or "").strip()
            if not code:
                continue
            family_labels = sorted(
                {
                    label
                    for model in row.phone_models_json or []
                    if isinstance(model, Mapping) and (label := _phone_model_label(model))
                }
            )
            context = ActiveDisplayFamilyMemberContext(
                registry_version_id=version.id,
                registry_version_number=version.version_number,
                registry_inventory_checksum=version.inventory_checksum,
                family_record_id=int(row.id),
                family_key=str(row.family_key),
                family_label=", ".join(family_labels) or str(row.family_key),
                family_member_count=int(row.member_count),
                family_review_member_count=int(row.review_member_count),
                family_matching_review_member_count=int(row.matching_review_member_count),
                family_warning_codes=tuple(_string_list(row.family_warning_codes)),
                product_id=int(row.product_id),
                segment_id=str(row.segment_id),
                quality_segment=str(row.quality_segment),
                construction_segment=str(row.construction_segment),
                requires_manual_review=bool(row.requires_manual_review),
                member_warning_codes=tuple(_string_list(row.member_warning_codes)),
                matching_evidence=dict(row.matching_evidence_json or {}),
            )
            existing = result.get(code)
            if existing is not None and existing != context:
                raise DisplayFamilyRegistryError(
                    f"duplicate active family membership for nomenclature code {code}"
                )
            result[code] = context
    return result


def build_display_family_bootstrap_plan(
    session: Session,
    bundle: PreparedDisplayFamilyBundle,
) -> DisplayFamilyBootstrapPlan:
    product_ids = sorted(int(item["product_id"]) for item in bundle.items)
    existing_product_ids: set[int] = set()
    for chunk in _chunks(product_ids):
        existing_product_ids.update(
            int(value)
            for value in session.scalars(select(Product.id).where(Product.id.in_(chunk))).all()
        )
    missing_product_ids = tuple(sorted(set(product_ids) - existing_product_ids))
    registry_schema_ready = all(
        inspect(session.get_bind()).has_table(table_name)
        for table_name in (
            DisplayFamilyRegistryVersion.__tablename__,
            DisplayFamily.__tablename__,
            DisplayFamilyMember.__tablename__,
            DisplayFamilyDecisionEvent.__tablename__,
        )
    )
    active_version = (
        active_display_family_registry_version(session) if registry_schema_ready else None
    )
    checksum_version = (
        session.scalar(
            select(DisplayFamilyRegistryVersion).where(
                DisplayFamilyRegistryVersion.inventory_checksum
                == str(bundle.manifest["inventory_checksum"])
            )
        )
        if registry_schema_ready
        else None
    )
    max_version_number = (
        session.scalar(select(func.max(DisplayFamilyRegistryVersion.version_number)))
        if registry_schema_ready
        else None
    )
    next_version_number = int(max_version_number or 0) + 1

    blockers: list[str] = []
    action = "create_initial_active_version"
    idempotent = False
    version_number: int | None = next_version_number
    replaces_version_number: int | None = None
    if missing_product_ids:
        blockers.append("bundle_contains_missing_products")
    if not registry_schema_ready:
        blockers.append("registry_migration_required")
    if checksum_version is not None:
        version_number = checksum_version.version_number
        if checksum_version.status == "active":
            action = "readback_existing_active_version"
            idempotent = True
        else:
            blockers.append("approved_checksum_exists_but_is_not_active")
            action = "blocked"
    elif active_version is not None:
        action = "create_successor_active_version"
        replaces_version_number = active_version.version_number
        if bundle.effective_from < active_version.effective_from:
            blockers.append("successor_effective_date_precedes_active_version")
        active_readback = readback_display_family_registry_version(session, active_version)
        if not active_readback["ok"]:
            blockers.append("active_registry_readback_failed")

    return DisplayFamilyBootstrapPlan(
        ready=not blockers,
        registry_schema_ready=registry_schema_ready,
        action=action,
        idempotent=idempotent,
        version_number=version_number,
        active_version_number=active_version.version_number if active_version else None,
        replaces_version_number=replaces_version_number,
        existing_checksum_version_number=(
            checksum_version.version_number if checksum_version else None
        ),
        expected_family_count=len(bundle.family_items),
        expected_member_count=len(bundle.items),
        existing_product_count=len(existing_product_ids),
        missing_product_ids=missing_product_ids,
        blockers=tuple(blockers),
        inventory_checksum=str(bundle.manifest["inventory_checksum"]),
        membership_checksum=bundle.membership_checksum,
        bundle_path=str(bundle.path),
    )


def _family_projection(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    phone_models: dict[tuple[Any, ...], dict[str, Any]] = {}
    phone_model_ids: set[int] = set()
    signatures: set[tuple[str, ...]] = set()
    segments: set[str] = set()
    warnings: set[str] = set()
    notes: set[str] = set()
    status_counts: Counter[str] = Counter()
    scope_reason_counts: Counter[str] = Counter()
    for row in rows:
        for raw_model in row.get("phone_models") or []:
            if not isinstance(raw_model, dict):
                continue
            model = dict(raw_model)
            model_id = model.get("id")
            if model_id is not None:
                phone_model_ids.add(int(model_id))
            key = (
                model.get("id"),
                model.get("brand"),
                model.get("model_name"),
                model.get("variant"),
            )
            phone_models[key] = model
        signature = tuple(_string_list(row.get("physical_model_signature")))
        if signature:
            signatures.add(signature)
        segments.add(str(row.get("segment_id") or "unknown"))
        warnings.update(_string_list(row.get("proposal_warnings")))
        notes.update(_string_list(row.get("proposal_notes")))
        status_counts[str(row.get("proposal_status") or "unknown")] += 1
        scope_reason_counts.update(_string_list(row.get("scope_reasons")))
    return {
        "member_count": len(rows),
        "is_singleton": len(rows) == 1,
        "total_current_stock_qty": sum(int(row.get("current_stock_qty") or 0) for row in rows),
        "review_member_count": sum(bool(row.get("requires_manual_review")) for row in rows),
        "matching_review_member_count": sum(
            bool((row.get("matching_audit") or {}).get("requires_review")) for row in rows
        ),
        "quality_unknown_member_count": sum(
            str(row.get("quality_segment") or "unknown") == "unknown" for row in rows
        ),
        "construction_unknown_member_count": sum(
            str(row.get("construction_segment") or "unknown") == "unknown" for row in rows
        ),
        "phone_model_ids_json": sorted(phone_model_ids),
        "phone_models_json": sorted(
            phone_models.values(),
            key=lambda model: (
                str(model.get("brand") or ""),
                str(model.get("model_name") or ""),
                str(model.get("variant") or ""),
                int(model.get("id") or 0),
            ),
        ),
        "physical_model_signatures_json": [list(value) for value in sorted(signatures)],
        "segment_ids_json": sorted(segments),
        "warning_codes_json": sorted(warnings),
        "note_codes_json": sorted(notes),
        "evidence_snapshot_json": {
            "proposal_status_counts": dict(sorted(status_counts.items())),
            "scope_reason_counts": dict(sorted(scope_reason_counts.items())),
        },
    }


def _product_snapshot(item: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "article",
        "nomenclature_code",
        "name",
        "color",
        "quality",
        "construction",
        "display_type",
        "has_frame",
        "has_ic_pad",
        "has_binding_no_solder",
        "phone_models",
        "model_keys",
        "physical_model_signature",
        "related_model_signature",
        "scope_classification_reason",
        "scope_classification_warnings",
        "available_at_status",
        "last_sale_at",
        "has_recent_or_open_order",
        "is_active",
        "is_marked_for_deletion",
    )
    return {key: item.get(key) for key in keys}


def readback_display_family_registry_version(
    session: Session,
    version: DisplayFamilyRegistryVersion,
    *,
    expected_bundle: PreparedDisplayFamilyBundle | None = None,
) -> dict[str, Any]:
    family_count = int(
        session.scalar(
            select(func.count(DisplayFamily.id)).where(
                DisplayFamily.registry_version_id == version.id
            )
        )
        or 0
    )
    member_count = int(
        session.scalar(
            select(func.count(DisplayFamilyMember.id)).where(
                DisplayFamilyMember.registry_version_id == version.id
            )
        )
        or 0
    )
    membership_rows = session.execute(
        select(
            DisplayFamily.family_key,
            DisplayFamilyMember.product_id,
            DisplayFamilyMember.segment_id,
        )
        .join(DisplayFamilyMember, DisplayFamilyMember.family_id == DisplayFamily.id)
        .where(DisplayFamily.registry_version_id == version.id)
        .order_by(
            DisplayFamily.family_key,
            DisplayFamilyMember.product_id,
            DisplayFamilyMember.segment_id,
        )
    ).all()
    membership_checksum = _membership_checksum(
        {
            "proposed_family_id": family_key,
            "product_id": product_id,
            "segment_id": segment_id,
        }
        for family_key, product_id, segment_id in membership_rows
    )
    errors: list[str] = []
    if family_count != version.expected_family_count:
        errors.append("family_count_mismatch")
    if member_count != version.expected_member_count:
        errors.append("member_count_mismatch")
    if membership_checksum != version.membership_checksum:
        errors.append("membership_checksum_mismatch")
    if expected_bundle is not None:
        if family_count != len(expected_bundle.family_items):
            errors.append("bundle_family_count_mismatch")
        if member_count != len(expected_bundle.items):
            errors.append("bundle_member_count_mismatch")
        if membership_checksum != expected_bundle.membership_checksum:
            errors.append("bundle_membership_checksum_mismatch")
    return {
        "schema": "display_family_registry_readback.v1",
        "ok": not errors,
        "version_id": version.id,
        "version_number": version.version_number,
        "status": version.status,
        "effective_from": version.effective_from.isoformat(),
        "inventory_checksum": version.inventory_checksum,
        "membership_checksum": membership_checksum,
        "family_count": family_count,
        "member_count": member_count,
        "errors": sorted(set(errors)),
    }


def apply_display_family_bootstrap(
    session: Session,
    bundle: PreparedDisplayFamilyBundle,
    *,
    actor: str,
    reason: str = INITIAL_BOOTSTRAP_REASON,
) -> dict[str, Any]:
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise DisplayFamilyRegistryError("actor is required")
    if not reason:
        raise DisplayFamilyRegistryError("reason is required")
    if session.in_transaction():
        raise DisplayFamilyRegistryError("bootstrap requires a fresh database session")

    with session.begin():
        plan = build_display_family_bootstrap_plan(session, bundle)
        if not plan.ready:
            raise DisplayFamilyRegistryError("bootstrap is blocked: " + ", ".join(plan.blockers))
        if plan.idempotent:
            version = session.scalar(
                select(DisplayFamilyRegistryVersion).where(
                    DisplayFamilyRegistryVersion.inventory_checksum == plan.inventory_checksum
                )
            )
            if version is None:
                raise DisplayFamilyRegistryError("idempotent registry version disappeared")
            readback = readback_display_family_registry_version(
                session, version, expected_bundle=bundle
            )
            if not readback["ok"]:
                raise DisplayFamilyRegistryError(
                    "existing registry readback failed: " + ", ".join(readback["errors"])
                )
            return {
                "applied": False,
                "idempotent": True,
                "plan": plan.as_dict(),
                "readback": readback,
            }

        replaced_version: DisplayFamilyRegistryVersion | None = None
        if plan.replaces_version_number is not None:
            replaced_version = session.scalar(
                select(DisplayFamilyRegistryVersion)
                .where(DisplayFamilyRegistryVersion.status == "active")
                .with_for_update()
            )
            if (
                replaced_version is None
                or replaced_version.version_number != plan.replaces_version_number
            ):
                raise DisplayFamilyRegistryError(
                    "active registry version changed during successor activation"
                )
            replaced_readback = readback_display_family_registry_version(session, replaced_version)
            if not replaced_readback["ok"]:
                raise DisplayFamilyRegistryError(
                    "active registry readback failed before successor activation: "
                    + ", ".join(replaced_readback["errors"])
                )
            replaced_version.status = "superseded"
            replaced_version.superseded_at = datetime.now(UTC)
            session.flush()

        manifest_hashes = bundle.manifest["artifact_sha256"]
        inventory = bundle.inventory
        source_evidence = {
            key: value
            for key, value in inventory.items()
            if key not in {"items", "summary", "inventory_checksum"}
        }
        version = DisplayFamilyRegistryVersion(
            version_number=int(plan.version_number or 1),
            status="active",
            effective_from=bundle.effective_from,
            source_schema=str(bundle.manifest["schema"]),
            source_bundle_path=str(bundle.path),
            inventory_checksum=plan.inventory_checksum,
            membership_checksum=bundle.membership_checksum,
            inventory_sha256=str(manifest_hashes["inventory.json"]),
            inventory_csv_sha256=str(manifest_hashes["inventory.csv"]),
            report_sha256=str(manifest_hashes["report.html"]),
            source_quality_checksum=str(bundle.manifest["source_quality_checksum"]),
            expected_family_count=len(bundle.family_items),
            expected_member_count=len(bundle.items),
            actual_family_count=len(bundle.family_items),
            actual_member_count=len(bundle.items),
            source_manifest_json=dict(bundle.manifest),
            source_summary_json=dict(inventory.get("summary") or {}),
            evidence_snapshot_json=source_evidence,
            created_by=actor,
        )
        session.add(version)
        session.flush()

        for family_key, rows in bundle.family_items.items():
            projection = _family_projection(rows)
            family = DisplayFamily(
                registry_version_id=version.id,
                family_key=family_key,
                **projection,
            )
            session.add(family)
            session.flush()
            for item in rows:
                session.add(
                    DisplayFamilyMember(
                        registry_version_id=version.id,
                        family_id=family.id,
                        product_id=int(item["product_id"]),
                        segment_id=str(item.get("segment_id") or "unknown"),
                        proposal_status=str(item.get("proposal_status") or "unknown"),
                        quality_segment=str(item.get("quality_segment") or "unknown"),
                        construction_segment=str(item.get("construction_segment") or "unknown"),
                        requires_manual_review=bool(item.get("requires_manual_review")),
                        current_stock_qty=int(item.get("current_stock_qty") or 0),
                        warning_codes_json=_string_list(item.get("proposal_warnings")),
                        note_codes_json=_string_list(item.get("proposal_notes")),
                        scope_reasons_json=_string_list(item.get("scope_reasons")),
                        product_snapshot_json=_product_snapshot(item),
                        matching_evidence_json=dict(item.get("matching_audit") or {}),
                        identity_evidence_json=dict(item.get("identity_evidence") or {}),
                        evidence_snapshot_json=dict(item),
                    )
                )

        session.add(
            DisplayFamilyDecisionEvent(
                registry_version_id=version.id,
                action=(
                    "successor_activate" if replaced_version is not None else "bootstrap_activate"
                ),
                actor=actor,
                reason=reason,
                effective_at=bundle.effective_from,
                evidence_snapshot_json={
                    "source_bundle_path": str(bundle.path),
                    "source_schema": bundle.manifest["schema"],
                    "inventory_checksum": plan.inventory_checksum,
                    "membership_checksum": bundle.membership_checksum,
                    "artifact_sha256": dict(manifest_hashes),
                    "family_count": len(bundle.family_items),
                    "member_count": len(bundle.items),
                    "source_gates": dict(bundle.manifest["source_gates"]),
                    "replaces_version_id": (
                        replaced_version.id if replaced_version is not None else None
                    ),
                    "replaces_version_number": (
                        replaced_version.version_number if replaced_version is not None else None
                    ),
                },
            )
        )
        session.flush()
        readback = readback_display_family_registry_version(
            session, version, expected_bundle=bundle
        )
        if not readback["ok"]:
            raise DisplayFamilyRegistryError(
                "bootstrap readback failed: " + ", ".join(readback["errors"])
            )
        active_after = active_display_family_registry_version(session)
        if active_after is None or active_after.id != version.id:
            raise DisplayFamilyRegistryError("successor activation did not select the new version")
    return {"applied": True, "idempotent": False, "plan": plan.as_dict(), "readback": readback}


def plan_display_family_registry_rollback(
    session: Session, target_version_number: int
) -> dict[str, Any]:
    active = active_display_family_registry_version(session)
    target = session.scalar(
        select(DisplayFamilyRegistryVersion).where(
            DisplayFamilyRegistryVersion.version_number == target_version_number
        )
    )
    blockers: list[str] = []
    if active is None:
        blockers.append("active_registry_version_missing")
    if target is None:
        blockers.append("target_registry_version_missing")
    if active is not None and target is not None and active.id == target.id:
        blockers.append("target_registry_version_is_already_active")
    readback = (
        readback_display_family_registry_version(session, target) if target is not None else None
    )
    if readback is not None and not readback["ok"]:
        blockers.append("target_registry_version_readback_failed")
    return {
        "schema": "display_family_registry_rollback_plan.v1",
        "ready": not blockers,
        "from_version_number": active.version_number if active else None,
        "target_version_number": target_version_number,
        "target_status": target.status if target else None,
        "blockers": blockers,
        "target_readback": readback,
    }


def rollback_display_family_registry(
    session: Session,
    target_version_number: int,
    *,
    actor: str,
    reason: str,
    effective_at: date,
) -> dict[str, Any]:
    if not actor.strip() or not reason.strip():
        raise DisplayFamilyRegistryError("actor and reason are required for rollback")
    if session.in_transaction():
        raise DisplayFamilyRegistryError("rollback requires a fresh database session")
    with session.begin():
        plan = plan_display_family_registry_rollback(session, target_version_number)
        if not plan["ready"]:
            raise DisplayFamilyRegistryError("rollback is blocked: " + ", ".join(plan["blockers"]))
        active = active_display_family_registry_version(session)
        target = session.scalar(
            select(DisplayFamilyRegistryVersion).where(
                DisplayFamilyRegistryVersion.version_number == target_version_number
            )
        )
        if active is None or target is None:
            raise DisplayFamilyRegistryError("rollback state changed during transaction")
        now = datetime.now(UTC)
        active.status = "rolled_back"
        active.rolled_back_at = now
        session.flush()
        target.status = "active"
        target.superseded_at = None
        target.rolled_back_at = None
        session.flush()
        session.add(
            DisplayFamilyDecisionEvent(
                registry_version_id=target.id,
                action="rollback_activate",
                actor=actor.strip(),
                reason=reason.strip(),
                effective_at=effective_at,
                evidence_snapshot_json={
                    "from_version_id": active.id,
                    "from_version_number": active.version_number,
                    "target_version_id": target.id,
                    "target_version_number": target.version_number,
                    "target_inventory_checksum": target.inventory_checksum,
                },
            )
        )
        session.flush()
        readback = readback_display_family_registry_version(session, target)
        if not readback["ok"] or readback["status"] != "active":
            raise DisplayFamilyRegistryError("rollback readback failed")
    return {"applied": True, "plan": plan, "readback": readback}


def _version_payload(version: DisplayFamilyRegistryVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "version_number": version.version_number,
        "status": version.status,
        "effective_from": version.effective_from,
        "source_schema": version.source_schema,
        "inventory_checksum": version.inventory_checksum,
        "membership_checksum": version.membership_checksum,
        "family_count": version.actual_family_count,
        "member_count": version.actual_member_count,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "superseded_at": version.superseded_at,
        "rolled_back_at": version.rolled_back_at,
    }


def display_family_registry_summary(session: Session) -> dict[str, Any]:
    active = active_display_family_registry_version(session)
    version_count = int(session.scalar(select(func.count(DisplayFamilyRegistryVersion.id))) or 0)
    if active is None:
        return {
            "active_version": None,
            "version_count": version_count,
            "family_count": 0,
            "member_count": 0,
            "singleton_family_count": 0,
            "multi_sku_family_count": 0,
            "review_member_count": 0,
            "matching_review_member_count": 0,
            "quality_unknown_member_count": 0,
            "warning_counts": {},
            "status_counts": {},
        }
    aggregates = session.execute(
        select(
            func.count(DisplayFamily.id),
            func.sum(DisplayFamily.review_member_count),
            func.sum(DisplayFamily.matching_review_member_count),
            func.sum(DisplayFamily.quality_unknown_member_count),
        ).where(DisplayFamily.registry_version_id == active.id)
    ).one()
    singleton_count = int(
        session.scalar(
            select(func.count(DisplayFamily.id)).where(
                DisplayFamily.registry_version_id == active.id,
                DisplayFamily.is_singleton.is_(True),
            )
        )
        or 0
    )
    summary = active.source_summary_json or {}
    return {
        "active_version": _version_payload(active),
        "version_count": version_count,
        "family_count": int(aggregates[0] or 0),
        "member_count": active.actual_member_count,
        "singleton_family_count": singleton_count,
        "multi_sku_family_count": int(aggregates[0] or 0) - singleton_count,
        "review_member_count": int(aggregates[1] or 0),
        "matching_review_member_count": int(aggregates[2] or 0),
        "quality_unknown_member_count": int(aggregates[3] or 0),
        "warning_counts": dict(summary.get("warning_counts") or {}),
        "status_counts": dict(summary.get("status_counts") or {}),
    }


def _phone_model_label(model: Mapping[str, Any]) -> str:
    return " ".join(
        str(value).strip()
        for value in (model.get("brand"), model.get("model_name"), model.get("variant"))
        if value is not None and str(value).strip()
    )


def _family_payload(family: DisplayFamily) -> dict[str, Any]:
    return {
        "id": family.id,
        "family_key": family.family_key,
        "member_count": family.member_count,
        "is_singleton": family.is_singleton,
        "total_current_stock_qty": family.total_current_stock_qty,
        "review_member_count": family.review_member_count,
        "matching_review_member_count": family.matching_review_member_count,
        "quality_unknown_member_count": family.quality_unknown_member_count,
        "construction_unknown_member_count": family.construction_unknown_member_count,
        "phone_model_ids": list(family.phone_model_ids_json or []),
        "phone_models": [
            label
            for label in (_phone_model_label(model) for model in family.phone_models_json or [])
            if label
        ],
        "segment_ids": list(family.segment_ids_json or []),
        "warning_codes": list(family.warning_codes_json or []),
        "note_codes": list(family.note_codes_json or []),
    }


def list_active_display_families(
    session: Session,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
    singleton: bool | None = None,
    has_warnings: bool | None = None,
    needs_review: bool | None = None,
    matching_review: bool | None = None,
    quality_unknown: bool | None = None,
) -> dict[str, Any]:
    active = active_display_family_registry_version(session)
    if active is None:
        return {"items": [], "page": page, "page_size": page_size, "total": 0}
    query = select(DisplayFamily).where(DisplayFamily.registry_version_id == active.id)
    if singleton is not None:
        query = query.where(DisplayFamily.is_singleton.is_(singleton))
    if has_warnings is True:
        query = query.where(DisplayFamily.warning_codes_json != [])
    elif has_warnings is False:
        query = query.where(DisplayFamily.warning_codes_json == [])
    if needs_review is True:
        query = query.where(DisplayFamily.review_member_count > 0)
    elif needs_review is False:
        query = query.where(DisplayFamily.review_member_count == 0)
    if matching_review is True:
        query = query.where(DisplayFamily.matching_review_member_count > 0)
    elif matching_review is False:
        query = query.where(DisplayFamily.matching_review_member_count == 0)
    if quality_unknown is True:
        query = query.where(DisplayFamily.quality_unknown_member_count > 0)
    elif quality_unknown is False:
        query = query.where(DisplayFamily.quality_unknown_member_count == 0)
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        product_match = (
            select(DisplayFamilyMember.id)
            .join(Product, Product.id == DisplayFamilyMember.product_id)
            .where(
                DisplayFamilyMember.family_id == DisplayFamily.id,
                or_(
                    Product.name.ilike(pattern),
                    Product.article.ilike(pattern),
                    Product.code_1c.ilike(pattern),
                ),
            )
            .exists()
        )
        query = query.where(or_(DisplayFamily.family_key.ilike(pattern), product_match))
    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = session.scalars(
        query.order_by(DisplayFamily.member_count.desc(), DisplayFamily.family_key)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_family_payload(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def get_active_display_family_detail(session: Session, family_id: int) -> dict[str, Any] | None:
    active = active_display_family_registry_version(session)
    if active is None:
        return None
    family = session.scalar(
        select(DisplayFamily)
        .options(selectinload(DisplayFamily.members))
        .where(
            DisplayFamily.id == family_id,
            DisplayFamily.registry_version_id == active.id,
        )
    )
    if family is None:
        return None
    events = session.scalars(
        select(DisplayFamilyDecisionEvent)
        .where(
            DisplayFamilyDecisionEvent.registry_version_id == active.id,
            or_(
                DisplayFamilyDecisionEvent.family_id == family.id,
                DisplayFamilyDecisionEvent.family_id.is_(None),
            ),
        )
        .order_by(
            DisplayFamilyDecisionEvent.created_at.desc(), DisplayFamilyDecisionEvent.id.desc()
        )
        .limit(100)
    ).all()
    member_evidence_checksums = {
        member.product_id: _mapping_checksum(dict(member.matching_evidence_json or {}))
        for member in family.members
    }
    matching_confirmations: dict[int, DisplayFamilyDecisionEvent] = {}
    for event in reversed(events):
        if event.action != "matching_review_confirmed" or event.product_id is None:
            continue
        expected_checksum = member_evidence_checksums.get(event.product_id)
        stored_checksum = str(
            (event.evidence_snapshot_json or {}).get("matching_evidence_checksum") or ""
        )
        if expected_checksum and stored_checksum == expected_checksum:
            matching_confirmations[int(event.product_id)] = event
    payload = _family_payload(family)
    payload.update(
        {
            "registry_version": _version_payload(active),
            "physical_model_signatures": list(family.physical_model_signatures_json or []),
            "evidence_snapshot": dict(family.evidence_snapshot_json or {}),
            "members": [
                {
                    "id": member.id,
                    "product_id": member.product_id,
                    "segment_id": member.segment_id,
                    "proposal_status": member.proposal_status,
                    "quality_segment": member.quality_segment,
                    "construction_segment": member.construction_segment,
                    "requires_manual_review": member.requires_manual_review,
                    "current_stock_qty": member.current_stock_qty,
                    "warning_codes": list(member.warning_codes_json or []),
                    "note_codes": list(member.note_codes_json or []),
                    "scope_reasons": list(member.scope_reasons_json or []),
                    "product": dict(member.product_snapshot_json or {}),
                    "matching_evidence": dict(member.matching_evidence_json or {}),
                    "identity_evidence": dict(member.identity_evidence_json or {}),
                    "matching_review_confirmed": member.product_id in matching_confirmations,
                    "matching_review_confirmed_at": (
                        matching_confirmations[member.product_id].created_at
                        if member.product_id in matching_confirmations
                        else None
                    ),
                    "matching_review_confirmed_by": (
                        matching_confirmations[member.product_id].actor
                        if member.product_id in matching_confirmations
                        else None
                    ),
                }
                for member in family.members
            ],
            "events": [
                {
                    "id": event.id,
                    "action": event.action,
                    "actor": event.actor,
                    "reason": event.reason,
                    "effective_at": event.effective_at,
                    "created_at": event.created_at,
                    "product_id": event.product_id,
                    "evidence_snapshot": dict(event.evidence_snapshot_json or {}),
                }
                for event in events
            ],
        }
    )
    return payload


def confirm_display_family_matching_review(
    session: Session,
    *,
    family_id: int,
    nomenclature_code: str,
    expected_registry_version_number: int,
    expected_registry_inventory_checksum: str,
    actor: str,
    reason: str = "Сопоставление проверено в помощнике заказов",
) -> dict[str, Any]:
    active = active_display_family_registry_version(session)
    if active is None:
        raise DisplayFamilyRegistryError("active display-family registry version is missing")
    if (
        active.version_number != int(expected_registry_version_number)
        or active.inventory_checksum != str(expected_registry_inventory_checksum or "").strip()
    ):
        raise DisplayFamilyRegistryError("display-family registry changed; refresh the order")
    code = str(nomenclature_code or "").strip()
    member = session.scalar(
        select(DisplayFamilyMember)
        .join(Product, Product.id == DisplayFamilyMember.product_id)
        .where(
            DisplayFamilyMember.registry_version_id == active.id,
            DisplayFamilyMember.family_id == family_id,
            Product.code_1c == code,
        )
    )
    if member is None:
        raise DisplayFamilyRegistryError("display-family member was not found in active registry")
    matching_evidence = dict(member.matching_evidence_json or {})
    if not matching_evidence.get("requires_review"):
        raise DisplayFamilyRegistryError("matching review is not required for this member")
    evidence_checksum = _mapping_checksum(matching_evidence)
    existing = session.scalar(
        select(DisplayFamilyDecisionEvent)
        .where(
            DisplayFamilyDecisionEvent.registry_version_id == active.id,
            DisplayFamilyDecisionEvent.family_id == family_id,
            DisplayFamilyDecisionEvent.product_id == member.product_id,
            DisplayFamilyDecisionEvent.action == "matching_review_confirmed",
        )
        .order_by(
            DisplayFamilyDecisionEvent.created_at.desc(),
            DisplayFamilyDecisionEvent.id.desc(),
        )
    )
    if (
        existing is not None
        and str((existing.evidence_snapshot_json or {}).get("matching_evidence_checksum") or "")
        == evidence_checksum
    ):
        return _matching_confirmation_payload(
            existing,
            active=active,
            family_id=family_id,
            nomenclature_code=code,
            idempotent=True,
        )
    event = DisplayFamilyDecisionEvent(
        registry_version_id=active.id,
        family_id=family_id,
        product_id=member.product_id,
        action="matching_review_confirmed",
        actor=str(actor or "").strip() or "unknown",
        reason=str(reason or "").strip() or "Сопоставление проверено",
        effective_at=date.today(),
        evidence_snapshot_json={
            "registry_version_number": active.version_number,
            "registry_inventory_checksum": active.inventory_checksum,
            "matching_evidence_checksum": evidence_checksum,
            "matching_evidence": matching_evidence,
            "nomenclature_code": code,
        },
    )
    session.add(event)
    session.flush()
    return _matching_confirmation_payload(
        event,
        active=active,
        family_id=family_id,
        nomenclature_code=code,
        idempotent=False,
    )


def matching_review_confirmations_by_code(session: Session) -> dict[str, dict[str, Any]]:
    active = active_display_family_registry_version(session)
    if active is None:
        return {}
    rows = session.execute(
        select(
            DisplayFamilyDecisionEvent,
            Product.code_1c,
            DisplayFamilyMember.matching_evidence_json,
        )
        .join(Product, Product.id == DisplayFamilyDecisionEvent.product_id)
        .join(
            DisplayFamilyMember,
            (DisplayFamilyMember.registry_version_id == active.id)
            & (DisplayFamilyMember.product_id == DisplayFamilyDecisionEvent.product_id)
            & (DisplayFamilyMember.family_id == DisplayFamilyDecisionEvent.family_id),
        )
        .where(
            DisplayFamilyDecisionEvent.registry_version_id == active.id,
            DisplayFamilyDecisionEvent.action == "matching_review_confirmed",
        )
        .order_by(
            DisplayFamilyDecisionEvent.created_at,
            DisplayFamilyDecisionEvent.id,
        )
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for event, code_1c, matching_evidence in rows:
        code = str(code_1c or "").strip()
        if not code:
            continue
        stored_checksum = str(
            (event.evidence_snapshot_json or {}).get("matching_evidence_checksum") or ""
        )
        if stored_checksum != _mapping_checksum(dict(matching_evidence or {})):
            continue
        result[code] = {
            "confirmed_at": event.created_at,
            "confirmed_by": event.actor,
            "registry_version_number": active.version_number,
            "registry_inventory_checksum": active.inventory_checksum,
        }
    return result


def _matching_confirmation_payload(
    event: DisplayFamilyDecisionEvent,
    *,
    active: DisplayFamilyRegistryVersion,
    family_id: int,
    nomenclature_code: str,
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "family_id": family_id,
        "nomenclature_code": nomenclature_code,
        "registry_version_number": active.version_number,
        "registry_inventory_checksum": active.inventory_checksum,
        "confirmed_at": event.created_at,
        "confirmed_by": event.actor,
        "idempotent": idempotent,
    }


def _mapping_checksum(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def list_display_family_registry_versions(
    session: Session, *, limit: int = 50
) -> list[dict[str, Any]]:
    versions = session.scalars(
        select(DisplayFamilyRegistryVersion)
        .order_by(DisplayFamilyRegistryVersion.version_number.desc())
        .limit(limit)
    ).all()
    return [_version_payload(version) for version in versions]
