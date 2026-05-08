from tasks.match_competitor_items import (
    _brand_for_compat,
    _infer_brand_from_model,
    _resolve_model_variants,
)


def test_variant_by_order_mapping():
    name = "Display for iPhone 12/12 Pro (A2403/A2407)"
    models = ["iphone 12", "iphone 12 pro"]
    variants, strategy = _resolve_model_variants(name, "apple", models, None)
    assert strategy == "by_order"
    assert variants == [("A2403", None), ("A2407", None)]


def test_variant_parentheses_mapping():
    name = "Display iPhone 12 (A2403/A2404) / 12 Pro (A2407)"
    models = ["iphone 12", "iphone 12 pro"]
    variants, strategy = _resolve_model_variants(name, "apple", models, None)
    assert strategy == "per_model_parentheses"
    assert variants == [("A2403/A2404", None), ("A2407", None)]


def test_variant_ambiguous_sets_notes():
    name = "Display iPhone 12/12 Pro (A2403/A2407/A2408)"
    models = ["iphone 12", "iphone 12 pro"]
    variants, strategy = _resolve_model_variants(name, "apple", models, None)
    assert strategy == "ambiguous_null"
    assert variants == [
        (None, "device_codes=A2403/A2407/A2408"),
        (None, "device_codes=A2403/A2407/A2408"),
    ]


def test_variant_by_order_for_samsung_codes():
    name = "Display Galaxy A5 2016/2017 A510F/A520F"
    models = ["galaxy a5 2016", "galaxy a5 2017"]
    variants, strategy = _resolve_model_variants(name, "samsung", models, None)
    assert strategy == "by_order"
    assert variants == [("A510F", None), ("A520F", None)]


def test_variant_parentheses_for_samsung_codes():
    name = "Display Galaxy A5 2016 (A510F) / A5 2017 (A520F)"
    models = ["galaxy a5 2016", "galaxy a5 2017"]
    variants, strategy = _resolve_model_variants(name, "samsung", models, None)
    assert strategy == "per_model_parentheses"
    assert variants == [("A510F", None), ("A520F", None)]


def test_infer_brand_from_model_acer():
    assert _infer_brand_from_model("acer iconia tab a100") == "acer"


def test_brand_for_compat_falls_back_to_model_brand_acer():
    assert _brand_for_compat(None, None, "acer iconia tab a101") == "acer"


def test_infer_brand_from_model_keeps_explay_when_mixed_with_acer():
    assert _infer_brand_from_model("acer iconia tab explay mid 725") == "explay"
