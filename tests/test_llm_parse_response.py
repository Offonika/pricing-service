from app.services.competitor_matching import _parse_llm_response, _sanitize_llm_models


def test_parse_llm_items_contract():
    payload = {
        "brand": "samsung",
        "items": [
            {"model": "Galaxy A5 2016", "codes": ["A510F", "a510f"]},
            {"model": "Galaxy A5 2017", "codes": []},
        ],
    }
    parsed = _parse_llm_response(payload)
    assert parsed is not None
    assert parsed.brand == "samsung"
    assert parsed.models == ["galaxy a5 2016", "galaxy a5 2017"]
    assert parsed.items is not None
    assert parsed.items[0].model == "galaxy a5 2016"
    assert parsed.items[0].codes == ["A510F"]
    assert parsed.items[1].codes == []


def test_parse_llm_legacy_contract():
    payload = {"brand": "apple", "models": ["iphone 12", "iphone 12 pro"], "variant": "A2403/A2407"}
    parsed = _parse_llm_response(payload)
    assert parsed is not None
    assert parsed.brand == "apple"
    assert parsed.models == ["iphone 12", "iphone 12 pro"]
    assert parsed.variant == "A2403/A2407"


def test_sanitize_llm_rejects_generic_brand():
    payload = {"brand": "generic", "models": ["adapter microusb type c"]}
    parsed = _parse_llm_response(payload)
    assert parsed is not None
    sanitized, reason = _sanitize_llm_models(parsed, item_name="USB adapter")
    assert sanitized is None
    assert reason == "llm_blocked_generic_brand"


def test_sanitize_llm_rejects_cross_brand_models():
    payload = {"brand": "sony", "models": ["nokia 500", "sony xperia e"]}
    parsed = _parse_llm_response(payload)
    assert parsed is not None
    sanitized, reason = _sanitize_llm_models(
        parsed, item_name="Шлейф для Nokia 500 / Sony Xperia E"
    )
    assert sanitized is not None
    assert sanitized.models == ["sony xperia e"]
    assert reason == "llm_sanitized"


def test_sanitize_llm_rejects_too_many_models():
    payload = {
        "brand": "samsung",
        "models": [
            "galaxy a3 2016",
            "galaxy a5 2016",
            "galaxy a7 2016",
            "galaxy s6",
            "galaxy note 5",
        ],
    }
    parsed = _parse_llm_response(payload)
    assert parsed is not None
    sanitized, reason = _sanitize_llm_models(parsed, item_name="compat list")
    assert sanitized is None
    assert reason == "llm_multi_model_overflow"


def test_sanitize_llm_rejects_wearable():
    payload = {"brand": "apple", "models": ["watch se 2022"]}
    parsed = _parse_llm_response(payload)
    assert parsed is not None
    sanitized, reason = _sanitize_llm_models(parsed, item_name="Стекло для Apple Watch SE 2022")
    assert sanitized is None
    assert reason == "llm_blocked_wearable"
