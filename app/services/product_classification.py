from __future__ import annotations

from app.models import Product

CLASSIFICATION_SOURCE_1C = "1c"
CLASSIFICATION_SOURCE_GENERATED = "generated"


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    return cleaned or None


def recompute_product_classification(product: Product) -> None:
    subject_1c = _clean_text(product.subject_1c)
    subject_generated = _clean_text(product.subject_generated)
    kind_1c = _clean_text(product.vid_nomenklatury_1c)
    kind_generated = _clean_text(product.vid_nomenklatury_generated)

    product.subject_1c = subject_1c
    product.subject_generated = subject_generated
    product.vid_nomenklatury_1c = kind_1c
    product.vid_nomenklatury_generated = kind_generated

    if subject_1c is not None:
        product.subject = subject_1c
        product.subject_source = CLASSIFICATION_SOURCE_1C
    elif subject_generated is not None:
        product.subject = subject_generated
        product.subject_source = CLASSIFICATION_SOURCE_GENERATED
    else:
        product.subject = None
        product.subject_source = None

    if kind_1c is not None:
        product.vid_nomenklatury = kind_1c
        product.vid_nomenklatury_source = CLASSIFICATION_SOURCE_1C
    elif kind_generated is not None:
        product.vid_nomenklatury = kind_generated
        product.vid_nomenklatury_source = CLASSIFICATION_SOURCE_GENERATED
    else:
        product.vid_nomenklatury = None
        product.vid_nomenklatury_source = None
