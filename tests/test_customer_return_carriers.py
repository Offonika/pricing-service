import pytest

from app.services.customer_return_carriers import (
    STATUS_ARRIVED,
    STATUS_EXCEPTION,
    STATUS_IN_TRANSIT,
    InvalidCustomerReturnTrackingNumber,
    UnsupportedCustomerReturnCarrier,
    get_customer_return_carrier_adapter,
)


def test_russian_post_tracking_and_status_normalization() -> None:
    adapter = get_customer_return_carrier_adapter("russian_post")

    assert adapter.normalize_tracking_number("1234 5678 9012 34") == "12345678901234"
    assert adapter.normalize_tracking_number("RA-123456789-RU") == "RA123456789RU"
    assert adapter.normalize_status("arrived-at-post-office").status == STATUS_ARRIVED
    assert adapter.normalize_status("in transit").status == STATUS_IN_TRANSIT


def test_cdek_unknown_status_is_preserved_for_manual_review() -> None:
    adapter = get_customer_return_carrier_adapter("cdek")

    normalized = adapter.normalize_status("new-provider-code")

    assert normalized.status == STATUS_EXCEPTION
    assert normalized.recognized is False
    assert normalized.provider_code == "NEW_PROVIDER_CODE"


def test_invalid_tracking_numbers_are_rejected() -> None:
    with pytest.raises(InvalidCustomerReturnTrackingNumber):
        get_customer_return_carrier_adapter("russian_post").normalize_tracking_number("123")
    with pytest.raises(InvalidCustomerReturnTrackingNumber):
        get_customer_return_carrier_adapter("cdek").normalize_tracking_number("bad track!")


def test_yandex_adapter_is_reserved_but_inactive() -> None:
    with pytest.raises(UnsupportedCustomerReturnCarrier):
        get_customer_return_carrier_adapter("yandex_delivery")

    adapter = get_customer_return_carrier_adapter("yandex_delivery", include_inactive=True)
    assert adapter.active is False
