from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

CARRIER_RUSSIAN_POST = "russian_post"
CARRIER_CDEK = "cdek"
CARRIER_YANDEX_DELIVERY = "yandex_delivery"

STATUS_REGISTERED = "registered"
STATUS_IN_TRANSIT = "in_transit"
STATUS_ARRIVED = "arrived_at_pickup_point"
STATUS_CANCELLED = "cancelled"
STATUS_EXCEPTION = "exception"


class CustomerReturnCarrierError(ValueError):
    pass


class UnsupportedCustomerReturnCarrier(CustomerReturnCarrierError):
    pass


class InvalidCustomerReturnTrackingNumber(CustomerReturnCarrierError):
    pass


@dataclass(frozen=True)
class NormalizedCarrierStatus:
    status: str
    recognized: bool
    provider_code: str


class CustomerReturnCarrierAdapter(ABC):
    carrier: str
    active: bool = True
    status_map: dict[str, str] = {}

    @abstractmethod
    def normalize_tracking_number(self, value: str) -> str:
        raise NotImplementedError

    def normalize_status(self, provider_code: str) -> NormalizedCarrierStatus:
        normalized_code = re.sub(r"[\s-]+", "_", provider_code.strip().upper())
        if not normalized_code:
            return NormalizedCarrierStatus(
                status=STATUS_EXCEPTION,
                recognized=False,
                provider_code=normalized_code,
            )
        mapped = self.status_map.get(normalized_code)
        return NormalizedCarrierStatus(
            status=mapped or STATUS_EXCEPTION,
            recognized=mapped is not None,
            provider_code=normalized_code,
        )


class RussianPostCustomerReturnAdapter(CustomerReturnCarrierAdapter):
    carrier = CARRIER_RUSSIAN_POST
    status_map = {
        "REGISTERED": STATUS_REGISTERED,
        "ACCEPTED": STATUS_IN_TRANSIT,
        "PROCESSING": STATUS_IN_TRANSIT,
        "IN_TRANSIT": STATUS_IN_TRANSIT,
        "ARRIVED_AT_SORTING_CENTER": STATUS_IN_TRANSIT,
        "DEPARTED_FROM_SORTING_CENTER": STATUS_IN_TRANSIT,
        "ARRIVED_AT_POST_OFFICE": STATUS_ARRIVED,
        "ARRIVED_AT_PICKUP_POINT": STATUS_ARRIVED,
        "READY_FOR_PICKUP": STATUS_ARRIVED,
        "DELIVERED": STATUS_ARRIVED,
        "CANCELLED": STATUS_CANCELLED,
        "NOT_DELIVERED": STATUS_CANCELLED,
        "RETURNED_TO_SENDER": STATUS_CANCELLED,
    }

    def normalize_tracking_number(self, value: str) -> str:
        normalized = re.sub(r"[\s-]+", "", value.strip().upper())
        is_domestic = bool(re.fullmatch(r"\d{14}", normalized))
        is_international = bool(re.fullmatch(r"[A-Z]{2}\d{9}[A-Z]{2}", normalized))
        if not (is_domestic or is_international):
            raise InvalidCustomerReturnTrackingNumber(
                "russian_post tracking number must contain 14 digits or use UPU S10 format"
            )
        return normalized


class CdekCustomerReturnAdapter(CustomerReturnCarrierAdapter):
    carrier = CARRIER_CDEK
    status_map = {
        "REGISTERED": STATUS_REGISTERED,
        "ACCEPTED": STATUS_IN_TRANSIT,
        "RECEIVED_AT_SHIPMENT_WAREHOUSE": STATUS_IN_TRANSIT,
        "IN_TRANSIT": STATUS_IN_TRANSIT,
        "RECEIVED_AT_DELIVERY_WAREHOUSE": STATUS_IN_TRANSIT,
        "READY_FOR_PICKUP": STATUS_ARRIVED,
        "ARRIVED_AT_PICKUP_POINT": STATUS_ARRIVED,
        "DELIVERED": STATUS_ARRIVED,
        "CANCELLED": STATUS_CANCELLED,
        "NOT_DELIVERED": STATUS_CANCELLED,
        "RETURNED_TO_SENDER": STATUS_CANCELLED,
    }

    def normalize_tracking_number(self, value: str) -> str:
        normalized = re.sub(r"\s+", "", value.strip().upper())
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{4,63}", normalized):
            raise InvalidCustomerReturnTrackingNumber(
                "cdek tracking number must contain 5-64 latin letters, digits or hyphens"
            )
        return normalized


class YandexDeliveryCustomerReturnAdapter(CustomerReturnCarrierAdapter):
    carrier = CARRIER_YANDEX_DELIVERY
    active = False

    def normalize_tracking_number(self, value: str) -> str:
        normalized = re.sub(r"\s+", "", value.strip().upper())
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{4,63}", normalized):
            raise InvalidCustomerReturnTrackingNumber(
                "yandex delivery tracking number has invalid format"
            )
        return normalized


_ADAPTERS: dict[str, CustomerReturnCarrierAdapter] = {
    adapter.carrier: adapter
    for adapter in (
        RussianPostCustomerReturnAdapter(),
        CdekCustomerReturnAdapter(),
        YandexDeliveryCustomerReturnAdapter(),
    )
}


def get_customer_return_carrier_adapter(
    carrier: str,
    *,
    include_inactive: bool = False,
) -> CustomerReturnCarrierAdapter:
    key = carrier.strip().lower()
    adapter = _ADAPTERS.get(key)
    if adapter is None or (not adapter.active and not include_inactive):
        raise UnsupportedCustomerReturnCarrier(f"unsupported customer return carrier: {key}")
    return adapter
