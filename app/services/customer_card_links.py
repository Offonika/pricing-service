from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from app.core.config import Settings
from app.services.expertise_bitrix import BitrixRestClient

DEFAULT_ONEC_COUNTERPARTY_FIELD = "UF_CRM_MM_ONEC_COUNTERPARTY_IDS"
_HEX_REF_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{32})$")
_UUID_REF_RE = re.compile(
    r"^\{?([0-9a-fA-F]{8})-([0-9a-fA-F]{4})-([0-9a-fA-F]{4})-"
    r"([0-9a-fA-F]{4})-([0-9a-fA-F]{12})\}?$"
)


class CustomerCompanyBitrixClient(Protocol):
    def call(
        self,
        method: str,
        params: list[tuple[str, str]] | None = None,
        *,
        timeout: int = 60,
    ) -> dict[str, Any]: ...


class CustomerCardResolutionError(RuntimeError):
    pass


class CustomerCardNotFound(CustomerCardResolutionError):
    pass


class CustomerCardConflict(CustomerCardResolutionError):
    pass


@dataclass(frozen=True)
class CustomerCardLink:
    company_id: str
    url: str


def _reverse_bytes(hex_value: str) -> str:
    return "".join(
        reversed([hex_value[index : index + 2] for index in range(0, len(hex_value), 2)])
    )


def onec_reference_hex_candidates(value: str) -> tuple[str, ...]:
    normalized = str(value or "").strip()
    hex_match = _HEX_REF_RE.fullmatch(normalized)
    if hex_match:
        return (f"0x{hex_match.group(1).lower()}",)

    uuid_match = _UUID_REF_RE.fullmatch(normalized)
    if not uuid_match:
        raise ValueError("Некорректный GUID контрагента 1С")

    groups = [group.lower() for group in uuid_match.groups()]
    direct = "0x" + "".join(groups)
    sql_guid = (
        "0x"
        + _reverse_bytes(groups[0])
        + _reverse_bytes(groups[1])
        + _reverse_bytes(groups[2])
        + groups[3]
        + groups[4]
    )
    return tuple(dict.fromkeys((direct, sql_guid)))


def onec_reference_hash(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _HEX_REF_RE.fullmatch(normalized):
        raise ValueError("Некорректная hex-ссылка контрагента 1С")
    if not normalized.startswith("0x"):
        normalized = f"0x{normalized}"
    digest = hashlib.sha256(
        f"bitrix-crm-customer-audit-v1|onec-ref|{normalized}".encode()
    ).hexdigest()
    return digest[:24]


def _portal_base_url(webhook_url: str) -> str:
    parsed = urlparse(str(webhook_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CustomerCardResolutionError("Не настроен адрес портала Bitrix")
    return f"{parsed.scheme}://{parsed.netloc}"


def _company_ids(payload: dict[str, Any]) -> set[str]:
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result if isinstance(result, list) else []
    return {
        str(row.get("ID") or row.get("id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("ID") or row.get("id") or "").strip()
    }


def resolve_customer_card_link(
    onec_reference: str,
    *,
    settings: Settings,
    client: CustomerCompanyBitrixClient | None = None,
) -> CustomerCardLink:
    webhook_url = settings.receivable_bitrix_webhook_url or settings.bitrix_box_webhook_base
    if not webhook_url:
        raise CustomerCardResolutionError("Не настроен доступ к Bitrix")

    field_name = (
        str(settings.crm_company_onec_counterparty_ids_field or "").strip()
        or DEFAULT_ONEC_COUNTERPARTY_FIELD
    )
    bitrix = client or BitrixRestClient(webhook_url)
    company_ids: set[str] = set()
    for ref_hex in onec_reference_hex_candidates(onec_reference):
        ref_hash = onec_reference_hash(ref_hex)
        response = bitrix.call(
            "crm.company.list",
            [
                (f"filter[{field_name}]", ref_hash),
                ("select[]", "ID"),
            ],
        )
        company_ids.update(_company_ids(response))

    if not company_ids:
        raise CustomerCardNotFound("Карточка клиента в Bitrix пока не связана с 1С")
    if len(company_ids) > 1:
        raise CustomerCardConflict("Найдено несколько карточек клиента в Bitrix")

    company_id = next(iter(company_ids))
    return CustomerCardLink(
        company_id=company_id,
        url=f"{_portal_base_url(webhook_url)}/crm/company/details/{company_id}/",
    )
