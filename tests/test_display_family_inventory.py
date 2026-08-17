from datetime import date
from decimal import Decimal

from app.models.device_model import PhoneModel
from app.models.product import Product
from app.models.product_phone_model import ProductPhoneModel
from app.models.product_stock import ProductStock
from app.services.display_family_inventory import (
    AcceptedCompetitorMatchEvidence,
    DisplayInventoryScopeEvidence,
    build_display_family_inventory,
    classify_display_family_product,
    is_display_family_product,
)
from app.services.display_identity import display_identity_for_product


def _display(
    product_id: int,
    *,
    model_ids: tuple[int, ...],
    active: bool = True,
    marked: bool = False,
    variant: str | None = None,
) -> Product:
    product = Product(
        id=product_id,
        article=f"SKU-{product_id}",
        code_1c=f"CODE-{product_id}",
        name="Дисплей Apple iPhone 17 Pro Max Premium Soft OLED без рамки",
        subject="display",
        category="Дисплеи",
        display_quality="Copy High",
        in_frame="без рамки",
        is_active=active,
        is_marked_for_deletion=marked,
    )
    links = []
    for model_id in model_ids:
        model = PhoneModel(
            id=model_id,
            brand="Apple",
            model_name="iPhone 17 Pro Max",
            variant=variant,
            is_active=True,
        )
        link = ProductPhoneModel(
            product_id=product_id,
            phone_model_id=model_id,
            source="manual",
            confidence=Decimal("1.000"),
            is_manual=True,
        )
        link.phone_model = model
        links.append(link)
    product.phone_model_links = links
    product.compatibilities = []
    product.stock = ProductStock(product_id=product_id, quantity=0)
    return product


def test_exact_full_signature_proposes_one_manual_family() -> None:
    payload = build_display_family_inventory(
        [_display(1, model_ids=(10,)), _display(2, model_ids=(10,))],
        evidence_by_code={},
        as_of=date(2026, 8, 16),
    )

    assert len({item["proposed_family_id"] for item in payload["items"]}) == 1
    assert {item["proposal_status"] for item in payload["items"]} == {"proposed_exact_signature"}
    assert all(item["requires_manual_review"] for item in payload["items"])


def test_partial_overlap_never_creates_transitive_family() -> None:
    payload = build_display_family_inventory(
        [
            _display(1, model_ids=(10,)),
            _display(2, model_ids=(10, 11)),
            _display(3, model_ids=(11,)),
        ],
        evidence_by_code={},
        as_of=date(2026, 8, 16),
    )

    assert len({item["proposed_family_id"] for item in payload["items"]}) == 3
    assert all("partial_model_overlap" in item["proposal_warnings"] for item in payload["items"])


def test_unresolved_model_is_safe_singleton() -> None:
    payload = build_display_family_inventory(
        [_display(1, model_ids=()), _display(2, model_ids=())],
        evidence_by_code={},
        as_of=date(2026, 8, 16),
    )

    assert len({item["proposed_family_id"] for item in payload["items"]}) == 2
    assert {item["proposal_status"] for item in payload["items"]} == {"singleton_unresolved_model"}


def test_scope_includes_recent_inactive_sale_and_excludes_old_inactive_sku() -> None:
    recent = _display(1, model_ids=(10,), active=False)
    old = _display(2, model_ids=(11,), active=False)
    payload = build_display_family_inventory(
        [recent, old],
        evidence_by_code={
            "CODE-1": DisplayInventoryScopeEvidence(last_sale_at=date(2026, 1, 1)),
            "CODE-2": DisplayInventoryScopeEvidence(last_sale_at=date(2024, 1, 1)),
        },
        as_of=date(2026, 8, 16),
        history_months=24,
    )

    assert [item["nomenclature_code"] for item in payload["items"]] == ["CODE-1"]
    assert payload["items"][0]["scope_reasons"] == ["sale_within_history_window"]


def test_inventory_checksum_is_independent_of_input_order() -> None:
    first = _display(1, model_ids=(10,))
    second = _display(2, model_ids=(10,))

    left = build_display_family_inventory(
        [first, second], evidence_by_code={}, as_of=date(2026, 8, 16)
    )
    right = build_display_family_inventory(
        [second, first], evidence_by_code={}, as_of=date(2026, 8, 16)
    )

    assert left["inventory_checksum"] == right["inventory_checksum"]


def test_sim_esim_variants_require_related_variant_review() -> None:
    payload = build_display_family_inventory(
        [
            _display(1, model_ids=(10,), variant="SIM"),
            _display(2, model_ids=(11,), variant="eSIM"),
        ],
        evidence_by_code={},
        as_of=date(2026, 8, 16),
    )

    assert len({item["proposed_family_id"] for item in payload["items"]}) == 2
    assert all(
        "connectivity_variant_review" in item["proposal_warnings"] for item in payload["items"]
    )


def test_explicit_display_name_overrides_wrong_subject_with_auditable_conflict() -> None:
    product = _display(1, model_ids=(10,))
    product.subject = "корпус"
    product.subject_1c = "корпус"
    product.category = "Запчасти для телефонов"

    classification = classify_display_family_product(product)
    payload = build_display_family_inventory(
        [product], evidence_by_code={}, as_of=date(2026, 8, 16)
    )

    assert classification.included is True
    assert classification.reason == "explicit_display_module_name"
    assert classification.warnings == ("display_taxonomy_conflict",)
    assert payload["items"][0]["scope_classification_warnings"] == ["display_taxonomy_conflict"]


def test_accessory_name_overrides_wrong_display_taxonomy() -> None:
    product = _display(1, model_ids=(10,))
    product.name = "Сумка для Nintendo Switch OLED (черный)"
    product.subject = "display"
    product.subject_1c = "дисплей"
    product.category = "Дисплеи"

    classification = classify_display_family_product(product)
    payload = build_display_family_inventory(
        [product], evidence_by_code={}, as_of=date(2026, 8, 16)
    )

    assert classification.included is False
    assert classification.reason == "excluded_non_display_name"
    assert classification.warnings == ("display_taxonomy_conflict",)
    assert payload["items"] == []
    assert payload["scope_audit"]["conflict_count"] == 1


def test_known_taxonomy_only_accessories_are_excluded() -> None:
    product = _display(1, model_ids=(10,))
    product.subject = "display"
    product.subject_1c = "дисплей"
    product.category = "Дисплеи"

    for name in (
        "Джостик для Nintendo Switch / OLED / Lite (черный)",
        "Нижняя плата для Huawei Honor 50 SE с разъемом зарядки",
    ):
        product.name = name
        classification = classify_display_family_product(product)
        assert classification.included is False
        assert classification.reason == "excluded_non_display_name"


def test_bitok_is_excluded_before_family_classification_and_grouping() -> None:
    excluded = _display(1, model_ids=(10,))
    excluded.name = "Дисплей Apple iPhone 17 Pro Max (БИТОК)"
    included = _display(2, model_ids=(10,))

    classification = classify_display_family_product(excluded)
    payload = build_display_family_inventory(
        [excluded, included], evidence_by_code={}, as_of=date(2026, 8, 16)
    )

    assert classification.included is False
    assert classification.reason == "excluded_display_name_bitok"
    assert [row["nomenclature_code"] for row in payload["items"]] == ["CODE-2"]
    assert payload["summary"]["excluded_scope_policy_count"] == 1
    assert payload["scope_audit"]["scope_policy_version"] == "display_scope_policy.v1"
    assert payload["scope_audit"]["excluded_reason_counts"] == {"excluded_display_name_bitok": 1}
    assert payload["scope_audit"]["exclusions"] == [
        {
            "nomenclature_code": "CODE-1",
            "name": "Дисплей Apple iPhone 17 Pro Max (БИТОК)",
            "reason_code": "excluded_display_name_bitok",
            "scope_policy_version": "display_scope_policy.v1",
        }
    ]


def test_expected_device_variants_are_notes_not_generic_review_warnings() -> None:
    pro = _display(1, model_ids=(10,), variant="Pro")
    maximum = _display(2, model_ids=(11,), variant="Max")
    for product in (pro, maximum):
        product.phone_model_links[0].phone_model.model_name = "iPhone 17"

    payload = build_display_family_inventory(
        [pro, maximum], evidence_by_code={}, as_of=date(2026, 8, 16)
    )

    assert all(
        "related_device_variant_separation" in item["proposal_notes"] for item in payload["items"]
    )
    assert all(
        "related_model_identity_review" not in item["proposal_warnings"]
        for item in payload["items"]
    )


def test_accepted_matching_conflict_is_read_only_review_evidence() -> None:
    product = _display(1, model_ids=(10,))
    competitor_shape = _display(99, model_ids=(11,))
    competitor_shape.phone_model_links[0].phone_model.model_name = "iPhone 16 Pro Max"
    competitor_shape.display_quality = "Original"
    evidence = AcceptedCompetitorMatchEvidence(
        competitor_item_id=501,
        competitor="example",
        competitor_name="Дисплей-конкурент",
        method="manual",
        identity=display_identity_for_product(competitor_shape),
    )

    payload = build_display_family_inventory(
        [product],
        evidence_by_code={},
        matching_evidence_by_product_id={1: [evidence]},
        as_of=date(2026, 8, 16),
    )

    row = payload["items"][0]
    assert row["phone_model_ids"] == [10]
    assert row["matching_audit"]["matches"][0]["model_relation"] == "disjoint_model_ids"
    assert row["matching_audit"]["requires_review"] is True
    assert "manual_accepted_matching_review" in row["proposal_warnings"]


def test_display_frame_and_module_glass_are_not_family_products() -> None:
    frame = _display(1, model_ids=(10,))
    frame.name = "Рамка дисплея для iPhone 17 Pro Max"
    frame.subject = "корпус"
    frame.subject_1c = "корпус"
    frame.category = "Рамки дисплеев для телефонов"
    glass = _display(2, model_ids=(10,))
    glass.name = "Стекло модуля для iPhone 17 Pro Max"
    glass.subject = "стекло для переклейки"
    glass.subject_1c = "стекло для переклейки"
    glass.category = "Стекла модулей для телефонов"

    assert is_display_family_product(frame) is False
    assert is_display_family_product(glass) is False
    payload = build_display_family_inventory(
        [frame, glass], evidence_by_code={}, as_of=date(2026, 8, 16)
    )
    assert payload["items"] == []
