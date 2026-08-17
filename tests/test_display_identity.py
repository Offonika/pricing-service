from decimal import Decimal

from app.models.device_model import PhoneModel
from app.models.product import Product
from app.models.product_phone_model import ProductPhoneModel
from app.services.display_identity import display_identity_for_product
from app.services.matching_attributes import product_attribute


def _product(*, product_id: int = 1, model_id: int = 10, variant: str | None = None) -> Product:
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
    product = Product(
        id=product_id,
        article=f"SKU-{product_id}",
        code_1c=f"CODE-{product_id}",
        name="Дисплей iPhone 17 Pro Max Premium Soft OLED без рамки площадка под IC",
        subject="display",
        category="Дисплеи",
        display_quality="Copy High",
        in_frame="без рамки",
        is_active=True,
        is_marked_for_deletion=False,
    )
    product.phone_model_links = [link]
    product.compatibilities = []
    return product


def test_product_display_identity_uses_matching_normalization() -> None:
    product = _product()

    identity = display_identity_for_product(product)

    assert identity.quality == product_attribute(product, "display.quality") == "Copy High"
    assert identity.construction == product_attribute(product, "display.construction")
    assert identity.phone_model_ids == (10,)
    assert identity.physical_model_signature == ("phone-model:10",)
    assert identity.quality_segment == "copy_high"
    assert identity.construction_segment == "soft_oled"
    assert identity.has_frame is False
    assert identity.has_ic_pad is True
    assert identity.segment_id == "copy_high|soft_oled|without_frame|ic_pad"


def test_matrix_manufacturer_tag_is_evidence_not_segment_split() -> None:
    product = _product()
    product.name += " JCID"

    identity = display_identity_for_product(product)

    assert "JCID" in identity.matrix_tags
    assert "jcid" not in identity.segment_id.casefold()


def test_related_signature_ignores_phone_model_variant_but_physical_signature_does_not() -> None:
    sim = display_identity_for_product(_product(product_id=1, model_id=10, variant="SIM"))
    esim = display_identity_for_product(_product(product_id=2, model_id=11, variant="eSIM"))

    assert sim.physical_model_signature != esim.physical_model_signature
    assert sim.related_model_signature == esim.related_model_signature
