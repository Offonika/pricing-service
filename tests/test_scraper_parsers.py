from datetime import datetime

from app.services.scraper.parsers import parse_html_offers, parse_json_offers, parse_offers


def test_parse_json_offers():
    content = '[{"sku":"SKU-1","name":"Item 1","price": "123.45","availability":"true","url":"https://ex/item1","category":"Cat"}]'
    offers = parse_json_offers(content, "comp", datetime.utcnow())
    assert len(offers) == 1
    assert offers[0].external_sku == "SKU-1"
    assert str(offers[0].price_roz) == "123.45"
    assert offers[0].availability is True


def test_parse_html_offers():
    html = '''
    <div class="offer" data-sku="SKU-2" data-price="99.9" data-availability="in_stock" data-url="https://ex/item2" data-category="CatB">Item 2</div>
    <div class="offer" data-sku="SKU-3" data-price="0" data-availability="0" data-url="https://ex/item3" data-category="">Item 3</div>
    '''
    offers = parse_html_offers(html, "comp", datetime.utcnow())
    assert len(offers) == 2
    assert offers[0].external_sku == "SKU-2"
    assert offers[0].availability is True
    assert offers[1].availability is False


def test_parse_offers_prefers_json():
    content = '[{"sku":"SKU-4","name":"Item 4","price":"10","availability":"yes","url":"u"}]'
    offers = parse_offers(content, "comp", datetime.utcnow())
    assert len(offers) == 1
    assert offers[0].external_sku == "SKU-4"
