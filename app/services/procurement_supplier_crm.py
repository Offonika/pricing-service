from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any

CRM_SUPPLIER_ORIGINATOR_ID = "mm_onec_supplier"
CRM_SUPPLIER_CONTACT_ORIGINATOR_ID = "mm_onec_supplier_contact"

DEFAULT_CRM_FIELD_MAP = {
    "company": {
        "mm_onec_supplier_ref": "UF_CRM_MM_ONEC_SUPPLIER_REF",
        "mm_onec_supplier_code": "UF_CRM_MM_ONEC_SUPPLIER_CODE",
        "mm_onec_supplier_updated_at": "UF_CRM_MM_ONEC_SUPPLIER_UPDATED_AT",
        "mm_supplier_reg_no": "UF_CRM_MM_SUPPLIER_REG_NO",
        "mm_supplier_role": "UF_CRM_MM_SUPPLIER_ROLE",
        "mm_supplier_country": "UF_CRM_MM_SUPPLIER_COUNTRY",
        "mm_supplier_city": "UF_CRM_MM_SUPPLIER_CITY",
        "mm_wechat": "UF_CRM_MM_WECHAT",
        "mm_whatsapp": "UF_CRM_MM_WHATSAPP",
    },
    "contact": {
        "mm_onec_contact_ref": "UF_CRM_MM_ONEC_CONTACT_REF",
        "mm_onec_contact_code": "UF_CRM_MM_ONEC_CONTACT_CODE",
        "mm_onec_contact_updated_at": "UF_CRM_MM_ONEC_CONTACT_UPDATED_AT",
        "mm_wechat": "UF_CRM_MM_WECHAT",
        "mm_whatsapp": "UF_CRM_MM_WHATSAPP",
    },
}

SUPPLIER_STATUS_TO_PROCUREMENT_ENUM = {
    "resolved_existing": "resolved_existing",
    "created_from_onec": "created_from_onec",
    "manual_review": "manual_review",
    "blocked_duplicate": "blocked_duplicate",
}

CONTOUR_ALIASES = {
    "": "ordinary",
    "ordinary": "ordinary",
    "обычный": "ordinary",
    "обычная": "ordinary",
    "cargo": "cargo",
    "карго": "cargo",
    "vedimport": "ved_import",
    "ved_import": "ved_import",
    "вэдимпорт": "ved_import",
    "вэд импорт": "ved_import",
}


RUBLE_CURRENCY_TOKENS = {
    "643",
    "rub",
    "rur",
    "руб",
    "руб.",
    "рубль",
    "рубли",
    "российскийрубль",
    "российскиерубли",
}


class SupplierSyncError(RuntimeError):
    """Raised when supplier sync cannot safely continue."""


def clean_string(value: Any) -> str:
    return str(value or "").strip()


def currency_token(value: Any) -> str:
    return clean_string(value).casefold().replace(" ", "")


def is_ruble_currency(value: Any) -> bool:
    token = currency_token(value)
    return bool(token and token in RUBLE_CURRENCY_TOKENS)


def is_foreign_currency(value: Any) -> bool:
    token = currency_token(value)
    return bool(token and token not in RUBLE_CURRENCY_TOKENS)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: Any) -> str:
    text = clean_string(value).casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def mask_email(value: Any) -> str:
    email = clean_string(value)
    if "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if not local:
        return f"*@{domain}"
    return f"{local[:1]}***@{domain}"


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", clean_string(value))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def mask_phone(value: Any) -> str:
    phone = normalize_phone(value)
    if len(phone) < 4:
        return ""
    return f"***{phone[-4:]}"


def normalize_name(value: Any) -> str:
    text = clean_string(value).casefold().replace("ё", "е")
    text = re.sub(r"[\s\"'`«».,;:()\\[\\]{}<>/\\\\|_-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def list_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            if isinstance(item, dict):
                values.append(clean_string(item.get("VALUE") or item.get("value")))
            else:
                values.append(clean_string(item))
        return [item for item in values if item]
    text = clean_string(value)
    if not text:
        return []
    if "|" in text:
        return [part.strip() for part in text.split("|") if part.strip()]
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def unique_values(*values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in list_values(value):
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result


def iso_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return clean_string(value)


def crm_field_map(mapping: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    contract = (mapping or {}).get("crm_supplier_sync_contract") or {}
    field_map = contract.get("crm_field_map") or {}
    result = {
        "company": dict(DEFAULT_CRM_FIELD_MAP["company"]),
        "contact": dict(DEFAULT_CRM_FIELD_MAP["contact"]),
    }
    for entity in ("company", "contact"):
        if isinstance(field_map.get(entity), dict):
            result[entity].update(
                {str(key): str(value) for key, value in field_map[entity].items() if value}
            )
    return result


def originator_id(mapping: dict[str, Any] | None = None) -> str:
    contract = (mapping or {}).get("crm_supplier_sync_contract") or {}
    return clean_string(contract.get("originator_id")) or CRM_SUPPLIER_ORIGINATOR_ID


def supplier_title(supplier: dict[str, Any]) -> str:
    return (
        clean_string(supplier.get("title"))
        or clean_string(supplier.get("short_name"))
        or clean_string(supplier.get("name"))
        or clean_string(supplier.get("full_name"))
    )


def supplier_ref(supplier: dict[str, Any]) -> str:
    return clean_string(supplier.get("onec_ref") or supplier.get("ref") or supplier.get("id"))


def supplier_code(supplier: dict[str, Any]) -> str:
    return clean_string(supplier.get("onec_code") or supplier.get("code"))


def supplier_reg_no(supplier: dict[str, Any]) -> str:
    return clean_string(
        supplier.get("tax_id")
        or supplier.get("inn")
        or supplier.get("registration_number")
        or supplier.get("reg_number")
    )


def supplier_phones(supplier: dict[str, Any]) -> list[str]:
    return unique_values(supplier.get("phones"), supplier.get("phone"))


def supplier_emails(supplier: dict[str, Any]) -> list[str]:
    return unique_values(supplier.get("emails"), supplier.get("email"))


def supplier_websites(supplier: dict[str, Any]) -> list[str]:
    return unique_values(supplier.get("websites"), supplier.get("website"), supplier.get("site"))


def bitrix_multi(values: list[str], value_type: str = "WORK") -> list[dict[str, str]]:
    return [{"VALUE": value, "VALUE_TYPE": value_type} for value in values if clean_string(value)]


def bitrix_result_rows(payload: Any) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, dict) else payload
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("items", "companies", "contacts", "fields"):
            values = result.get(key)
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
    return []


def call_list(api: Any, method: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return bitrix_result_rows(api.call(method, params))


def sort_ids(values: set[str] | list[str]) -> list[str]:
    def key(value: str) -> tuple[int, str]:
        return (int(value), value) if str(value).isdigit() else (10**18, str(value))

    return sorted({clean_string(value) for value in values if clean_string(value)}, key=key)


def company_select(field_map: dict[str, dict[str, str]]) -> list[str]:
    return [
        "ID",
        "TITLE",
        "ASSIGNED_BY_ID",
        "ORIGINATOR_ID",
        "ORIGIN_ID",
        "PHONE",
        "EMAIL",
        "WEB",
        "ADDRESS",
        "ADDRESS_CITY",
        "ADDRESS_COUNTRY",
        *field_map["company"].values(),
    ]


def contact_select(field_map: dict[str, dict[str, str]]) -> list[str]:
    return [
        "ID",
        "NAME",
        "LAST_NAME",
        "COMPANY_ID",
        "ORIGINATOR_ID",
        "ORIGIN_ID",
        "PHONE",
        "EMAIL",
        *field_map["contact"].values(),
    ]


def duplicate_company_ids_by_comm(api: Any, comm_type: str, values: list[str]) -> set[str]:
    ids: set[str] = set()
    for value in values:
        if not clean_string(value):
            continue
        payload = api.call("crm.duplicate.findbycomm", {"type": comm_type, "values": [value]})
        result = payload.get("result") if isinstance(payload, dict) else payload
        if isinstance(result, dict):
            ids.update(str(item) for item in result.get("COMPANY") or [] if clean_string(item))
    return ids


def duplicate_contact_ids_by_comm(api: Any, comm_type: str, values: list[str]) -> set[str]:
    ids: set[str] = set()
    for value in values:
        if not clean_string(value):
            continue
        payload = api.call("crm.duplicate.findbycomm", {"type": comm_type, "values": [value]})
        result = payload.get("result") if isinstance(payload, dict) else payload
        if isinstance(result, dict):
            ids.update(str(item) for item in result.get("CONTACT") or [] if clean_string(item))
    return ids


def find_company_candidates(
    api: Any,
    supplier: dict[str, Any],
    *,
    mapping: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    fields = crm_field_map(mapping)
    company_fields = fields["company"]
    select = company_select(fields)
    onec_ref = supplier_ref(supplier)
    reg_no = supplier_reg_no(supplier)
    title = supplier_title(supplier)
    result: dict[str, set[str]] = {
        "onec_ref": set(),
        "registration_or_tax_number": set(),
        "normalized_title": set(),
        "phone_or_email": set(),
    }

    if onec_ref:
        for row in call_list(
            api,
            "crm.company.list",
            {
                "filter": {"=ORIGINATOR_ID": originator_id(mapping), "=ORIGIN_ID": onec_ref},
                "select": select,
            },
        ):
            result["onec_ref"].add(clean_string(row.get("ID")))
        ref_field = company_fields["mm_onec_supplier_ref"]
        for row in call_list(
            api,
            "crm.company.list",
            {"filter": {f"={ref_field}": onec_ref}, "select": select},
        ):
            result["onec_ref"].add(clean_string(row.get("ID")))

    if reg_no:
        reg_field = company_fields["mm_supplier_reg_no"]
        for row in call_list(
            api,
            "crm.company.list",
            {"filter": {f"={reg_field}": reg_no}, "select": select},
        ):
            result["registration_or_tax_number"].add(clean_string(row.get("ID")))

    if title:
        title_key = normalize_name(title)
        for row in call_list(
            api,
            "crm.company.list",
            {"filter": {"TITLE": title}, "select": select},
        ):
            if normalize_name(row.get("TITLE")) == title_key:
                result["normalized_title"].add(clean_string(row.get("ID")))

    result["phone_or_email"].update(
        duplicate_company_ids_by_comm(api, "PHONE", supplier_phones(supplier))
    )
    result["phone_or_email"].update(
        duplicate_company_ids_by_comm(api, "EMAIL", supplier_emails(supplier))
    )

    return {key: sort_ids(value) for key, value in result.items()}


def resolve_company_candidate(candidates: dict[str, list[str]]) -> tuple[str, str, str]:
    for basis in (
        "onec_ref",
        "registration_or_tax_number",
        "normalized_title",
        "phone_or_email",
    ):
        ids = candidates.get(basis) or []
        if len(ids) > 1:
            return "", "blocked_duplicate", f"multiple_company_matches:{basis}:{','.join(ids)}"
        if len(ids) == 1:
            return ids[0], "resolved_existing", basis
    return "", "created_from_onec", "no_match"


def get_company(api: Any, company_id: str) -> dict[str, Any]:
    if not company_id:
        return {}
    payload = api.call("crm.company.get", {"id": company_id})
    result = payload.get("result") if isinstance(payload, dict) else payload
    return result if isinstance(result, dict) else {}


def desired_company_fields(
    supplier: dict[str, Any],
    *,
    mapping: dict[str, Any] | None = None,
    assigned_by_id: str | int | None = None,
) -> dict[str, Any]:
    fields = crm_field_map(mapping)["company"]
    onec_ref = supplier_ref(supplier)
    title = supplier_title(supplier)
    result: dict[str, Any] = {
        "TITLE": title,
        "ORIGINATOR_ID": originator_id(mapping),
        "ORIGIN_ID": onec_ref,
        fields["mm_onec_supplier_ref"]: onec_ref,
        fields["mm_onec_supplier_code"]: supplier_code(supplier),
        fields["mm_onec_supplier_updated_at"]: iso_value(
            supplier.get("onec_updated_at") or supplier.get("updated_at")
        ),
        fields["mm_supplier_reg_no"]: supplier_reg_no(supplier),
        fields["mm_supplier_role"]: ["supplier"],
        fields["mm_supplier_country"]: clean_string(supplier.get("country")),
        fields["mm_supplier_city"]: clean_string(supplier.get("city")),
        fields["mm_wechat"]: clean_string(supplier.get("wechat")),
        fields["mm_whatsapp"]: clean_string(supplier.get("whatsapp")),
        "ADDRESS": clean_string(supplier.get("address")),
        "ADDRESS_CITY": clean_string(supplier.get("city")),
        "ADDRESS_COUNTRY": clean_string(supplier.get("country")),
        "PHONE": bitrix_multi(supplier_phones(supplier)),
        "EMAIL": bitrix_multi(supplier_emails(supplier)),
        "WEB": bitrix_multi(supplier_websites(supplier)),
    }
    if assigned_by_id:
        result["ASSIGNED_BY_ID"] = str(assigned_by_id)
    return {key: value for key, value in result.items() if value not in ("", [], None)}


def multi_value_set(value: Any) -> set[str]:
    return {item.casefold() for item in list_values(value)}


def is_empty_crm_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, list):
        return len(list_values(value)) == 0
    return False


def merge_fill_empty_only(
    current: dict[str, Any],
    desired: dict[str, Any],
    *,
    protected_fields: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    protected_fields = protected_fields or set()
    updates: dict[str, Any] = {}
    conflicts: list[str] = []
    for key, value in desired.items():
        if key in protected_fields:
            continue
        current_value = current.get(key)
        if is_empty_crm_value(current_value):
            updates[key] = value
            continue
        if isinstance(value, list):
            desired_set = multi_value_set(value)
            current_set = multi_value_set(current_value)
            if desired_set and desired_set.issubset(current_set):
                continue
            if desired_set:
                conflicts.append(key)
            continue
        if clean_string(current_value).casefold() != clean_string(value).casefold():
            conflicts.append(key)
    return updates, sorted(set(conflicts))


def company_public_summary(
    supplier: dict[str, Any],
    *,
    company_id: str,
    resolution_status_key: str,
    resolution_basis: str,
    conflicts: list[str],
) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "title": supplier_title(supplier),
        "onec_ref_hash": stable_hash(supplier_ref(supplier)),
        "reg_no_hash": stable_hash(supplier_reg_no(supplier)),
        "phone_masks": [mask_phone(item) for item in supplier_phones(supplier) if mask_phone(item)],
        "email_masks": [mask_email(item) for item in supplier_emails(supplier) if mask_email(item)],
        "resolution_status_key": resolution_status_key,
        "resolution_basis": resolution_basis,
        "conflict_fields": conflicts,
    }


def contact_title(contact: dict[str, Any]) -> str:
    return clean_string(
        contact.get("name")
        or contact.get("full_name")
        or contact.get("title")
        or contact.get("contact_name")
    )


def contact_ref(contact: dict[str, Any]) -> str:
    return clean_string(contact.get("onec_ref") or contact.get("ref") or contact.get("id"))


def contact_code(contact: dict[str, Any]) -> str:
    return clean_string(contact.get("onec_code") or contact.get("code"))


def contact_phones(contact: dict[str, Any]) -> list[str]:
    return unique_values(contact.get("phones"), contact.get("phone"))


def contact_emails(contact: dict[str, Any]) -> list[str]:
    return unique_values(contact.get("emails"), contact.get("email"))


def split_contact_name(value: str) -> tuple[str, str]:
    parts = [part for part in clean_string(value).split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def find_contact_candidates(
    api: Any,
    contact: dict[str, Any],
    *,
    mapping: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    fields = crm_field_map(mapping)
    contact_fields = fields["contact"]
    select = contact_select(fields)
    result: dict[str, set[str]] = {"onec_ref": set(), "phone_or_email": set()}
    ref = contact_ref(contact)
    if ref:
        ref_field = contact_fields["mm_onec_contact_ref"]
        for row in call_list(
            api,
            "crm.contact.list",
            {"filter": {f"={ref_field}": ref}, "select": select},
        ):
            result["onec_ref"].add(clean_string(row.get("ID")))
    result["phone_or_email"].update(
        duplicate_contact_ids_by_comm(api, "PHONE", contact_phones(contact))
    )
    result["phone_or_email"].update(
        duplicate_contact_ids_by_comm(api, "EMAIL", contact_emails(contact))
    )
    return {key: sort_ids(value) for key, value in result.items()}


def resolve_contact_candidate(candidates: dict[str, list[str]]) -> tuple[str, str, str]:
    for basis in ("onec_ref", "phone_or_email"):
        ids = candidates.get(basis) or []
        if len(ids) > 1:
            return "", "blocked_duplicate_contact", f"multiple_contact_matches:{basis}"
        if len(ids) == 1:
            return ids[0], "resolved_existing_contact", basis
    return "", "created_contact_from_onec", "no_match"


def get_contact(api: Any, contact_id: str) -> dict[str, Any]:
    if not contact_id:
        return {}
    payload = api.call("crm.contact.get", {"id": contact_id})
    result = payload.get("result") if isinstance(payload, dict) else payload
    return result if isinstance(result, dict) else {}


def desired_contact_fields(
    contact: dict[str, Any],
    *,
    company_id: str,
    mapping: dict[str, Any] | None = None,
    assigned_by_id: str | int | None = None,
) -> dict[str, Any]:
    fields = crm_field_map(mapping)["contact"]
    name, last_name = split_contact_name(contact_title(contact))
    result: dict[str, Any] = {
        "NAME": name,
        "LAST_NAME": last_name,
        "COMPANY_ID": company_id,
        "ORIGINATOR_ID": CRM_SUPPLIER_CONTACT_ORIGINATOR_ID,
        "ORIGIN_ID": contact_ref(contact),
        fields["mm_onec_contact_ref"]: contact_ref(contact),
        fields["mm_onec_contact_code"]: contact_code(contact),
        fields["mm_onec_contact_updated_at"]: iso_value(
            contact.get("onec_updated_at") or contact.get("updated_at")
        ),
        fields["mm_wechat"]: clean_string(contact.get("wechat")),
        fields["mm_whatsapp"]: clean_string(contact.get("whatsapp")),
        "PHONE": bitrix_multi(contact_phones(contact)),
        "EMAIL": bitrix_multi(contact_emails(contact)),
    }
    if assigned_by_id:
        result["ASSIGNED_BY_ID"] = str(assigned_by_id)
    return {key: value for key, value in result.items() if value not in ("", [], None)}


def sync_supplier_contact(
    api: Any,
    contact: dict[str, Any],
    *,
    company_id: str,
    mapping: dict[str, Any] | None = None,
    apply: bool = False,
    assigned_by_id: str | int | None = None,
) -> dict[str, Any]:
    candidates = find_contact_candidates(api, contact, mapping=mapping)
    contact_id, status, basis = resolve_contact_candidate(candidates)
    desired = desired_contact_fields(
        contact,
        company_id=company_id,
        mapping=mapping,
        assigned_by_id=assigned_by_id,
    )
    result = {
        "title": contact_title(contact),
        "contact_id": contact_id,
        "status": f"would_{status}" if not apply and status.startswith("created") else status,
        "resolution_basis": basis,
        "phone_masks": [mask_phone(item) for item in contact_phones(contact) if mask_phone(item)],
        "email_masks": [mask_email(item) for item in contact_emails(contact) if mask_email(item)],
        "conflict_fields": [],
        "updated_field_names": [],
    }
    if status == "blocked_duplicate_contact":
        result["blocked_reason"] = basis
        return result
    if status == "created_contact_from_onec":
        result["updated_field_names"] = sorted(desired)
        if apply:
            created = api.call("crm.contact.add", {"fields": desired})
            created_result = created.get("result") if isinstance(created, dict) else created
            result["contact_id"] = clean_string(created_result)
            result["status"] = "created_contact_from_onec"
        return result

    current = get_contact(api, contact_id)
    updates, conflicts = merge_fill_empty_only(current, desired)
    result["conflict_fields"] = conflicts
    result["updated_field_names"] = sorted(updates)
    if apply and updates:
        api.call("crm.contact.update", {"id": contact_id, "fields": updates})
    return result


def sync_supplier_to_crm(
    api: Any,
    supplier: dict[str, Any],
    *,
    mapping: dict[str, Any] | None = None,
    apply: bool = False,
    assigned_by_id: str | int | None = None,
) -> dict[str, Any]:
    title = supplier_title(supplier)
    if not title:
        return {
            "status": "blocked_manual_review",
            "resolution_status_key": "manual_review",
            "blocker_comment": "Не заполнено название поставщика из 1С.",
        }

    candidates = find_company_candidates(api, supplier, mapping=mapping)
    company_id, resolution_status_key, resolution_basis = resolve_company_candidate(candidates)
    if resolution_status_key == "blocked_duplicate":
        blocker = f"CRM-поставщик не определен: {resolution_basis}."
        return {
            "status": "blocked_duplicate",
            "resolution_status_key": "blocked_duplicate",
            "resolution_basis": resolution_basis,
            "company_id": "",
            "company": company_public_summary(
                supplier,
                company_id="",
                resolution_status_key=resolution_status_key,
                resolution_basis=resolution_basis,
                conflicts=[],
            ),
            "candidates": candidates,
            "blocker_comment": blocker,
            "contacts": [],
        }

    desired = desired_company_fields(supplier, mapping=mapping, assigned_by_id=assigned_by_id)
    contacts: list[dict[str, Any]] = []
    updated_field_names: list[str] = []
    conflicts: list[str] = []
    status = (
        "would_create_company"
        if resolution_status_key == "created_from_onec" and not apply
        else resolution_status_key
    )

    if resolution_status_key == "created_from_onec":
        updated_field_names = sorted(desired)
        if apply:
            created = api.call("crm.company.add", {"fields": desired})
            created_result = created.get("result") if isinstance(created, dict) else created
            company_id = clean_string(created_result)
            status = "created_company"
    else:
        current = get_company(api, company_id)
        updates, conflicts = merge_fill_empty_only(current, desired)
        updated_field_names = sorted(updates)
        if apply and updates:
            api.call("crm.company.update", {"id": company_id, "fields": updates})
        status = "updated_existing_company" if updates and apply else "resolved_existing_company"

    target_company_id = company_id or "(new_company)"
    for contact in supplier.get("contacts") or []:
        if isinstance(contact, dict):
            contacts.append(
                sync_supplier_contact(
                    api,
                    contact,
                    company_id=target_company_id,
                    mapping=mapping,
                    apply=apply and bool(company_id),
                    assigned_by_id=assigned_by_id,
                )
            )

    contact_conflicts = [
        f"contact:{item['title']}:{','.join(item.get('conflict_fields') or [])}"
        for item in contacts
        if item.get("conflict_fields")
    ]
    blocker_parts = []
    if conflicts:
        blocker_parts.append("CRM не перезаписана, есть отличия: " + ", ".join(conflicts))
    if contact_conflicts:
        blocker_parts.append("Контакты требуют проверки: " + "; ".join(contact_conflicts))

    return {
        "status": status,
        "resolution_status_key": resolution_status_key,
        "resolution_basis": resolution_basis,
        "company_id": company_id,
        "company": company_public_summary(
            supplier,
            company_id=company_id,
            resolution_status_key=resolution_status_key,
            resolution_basis=resolution_basis,
            conflicts=conflicts,
        ),
        "updated_field_names": updated_field_names,
        "conflict_fields": conflicts,
        "contacts": contacts,
        "blocker_comment": "\n".join(blocker_parts),
    }


def normalize_procurement_contour(
    value: Any, *, is_open_supplier_order: bool = False, currency: Any = None
) -> str:
    raw_value = clean_string(value)
    if not raw_value:
        if is_foreign_currency(currency):
            return "cargo"
        if is_ruble_currency(currency):
            return "ordinary"
        return "cargo" if is_open_supplier_order else "ordinary"
    direct_key = raw_value.casefold()
    if direct_key in CONTOUR_ALIASES:
        return CONTOUR_ALIASES[direct_key]
    compact_key = direct_key.replace("-", "").replace("_", "").replace(" ", "")
    if compact_key in CONTOUR_ALIASES:
        return CONTOUR_ALIASES[compact_key]
    raise ValueError(f"Unsupported procurement contour value from 1C: {raw_value!r}")


def procurement_status_enum_id(mapping: dict[str, Any], status_key: str) -> str:
    contract = mapping.get("crm_supplier_sync_contract") or {}
    enum_map = contract.get("procurement_supplier_status_enum") or {}
    return clean_string(enum_map.get(status_key))


def procurement_field(mapping: dict[str, Any], logical_key: str) -> str:
    return clean_string((mapping.get("field_map") or {}).get(logical_key))


def truthy_order_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raw = clean_string(value).casefold()
    return raw in {"1", "y", "yes", "true", "да", "истина"}


def order_has_value(onec_order: dict[str, Any], *keys: str) -> bool:
    return any(clean_string(onec_order.get(key)) for key in keys)


def procurement_stage_key(logical_key: str, onec_order: dict[str, Any]) -> str:
    explicit = clean_string(
        onec_order.get("procurement_stage_key") or onec_order.get("stage_key")
    )
    if explicit:
        return explicit
    if logical_key == "ved_import":
        if order_has_value(onec_order, "expected_receipt_date", "Поступление"):
            return "receiving"
        if truthy_order_value(onec_order.get("posted") or onec_order.get("is_posted")):
            return "docs_collection"
        return "need"
    if logical_key != "cargo":
        return "need"
    if order_has_value(onec_order, "cargo_dropoff_date", "Сдача в карго"):
        return "cargo_dropoff"
    if order_has_value(onec_order, "supplier_dispatch_date", "Отправка постав."):
        return "supplier_dispatch"
    if order_has_value(onec_order, "payment_date", "Оплата"):
        return "payment_request"
    if truthy_order_value(onec_order.get("posted") or onec_order.get("is_posted")):
        return "supplier_order"
    return "need"


def build_procurement_order_bitrix_fields(
    onec_order: dict[str, Any],
    supplier_result: dict[str, Any],
    *,
    mapping: dict[str, Any],
    on_supplier_conflict: str = "create_card_with_blocker",
) -> dict[str, Any]:
    logical_key = normalize_procurement_contour(
        onec_order.get("procurement_contour") or onec_order.get("КонтурЗакупки"),
        is_open_supplier_order=bool(onec_order.get("is_open_supplier_order")),
        currency=onec_order.get("currency") or onec_order.get("Валюта"),
    )
    category = (mapping.get("category_map") or {}).get(logical_key) or {}
    stage_map = (mapping.get("stage_map") or {}).get(logical_key) or {}
    enum_map = (mapping.get("enum_map") or {}).get("procurement_contour") or {}
    category_id = category.get("id")
    stage_key = procurement_stage_key(logical_key, onec_order)
    stage_id = stage_map.get(stage_key) or stage_map.get("need") or next(iter(stage_map.values()), "")
    contour_field = procurement_field(mapping, "procurement_contour")
    contour_enum_id = enum_map.get(logical_key)
    missing = [
        name
        for name, value in [
            ("category", category_id),
            ("stage", stage_id),
            ("procurement_contour_field", contour_field),
            ("procurement_contour_enum", contour_enum_id),
        ]
        if not value
    ]
    if missing:
        raise KeyError(f"Bitrix procurement mapping is incomplete: {', '.join(missing)}")

    blocked_supplier = supplier_result.get("resolution_status_key") in {
        "blocked_duplicate",
        "manual_review",
    }
    if blocked_supplier and on_supplier_conflict == "block_import":
        raise SupplierSyncError(clean_string(supplier_result.get("blocker_comment")))

    fields: dict[str, Any] = {
        "categoryId": int(category_id),
        "stageId": str(stage_id),
        contour_field: str(contour_enum_id),
    }
    company_id = clean_string(supplier_result.get("company_id"))
    supplier_company_field = procurement_field(mapping, "supplier_company")
    if company_id and supplier_company_field:
        fields[supplier_company_field] = company_id

    onec_ref = supplier_ref(onec_order.get("supplier") or {}) or clean_string(
        supplier_result.get("company", {}).get("onec_ref")
    )
    supplier_ref_field = procurement_field(mapping, "supplier_onec_ref")
    if onec_ref and supplier_ref_field:
        fields[supplier_ref_field] = onec_ref

    status_key = clean_string(supplier_result.get("resolution_status_key"))
    status_field = procurement_field(mapping, "supplier_resolution_status")
    status_enum_id = procurement_status_enum_id(mapping, status_key)
    if status_field and status_enum_id:
        fields[status_field] = status_enum_id

    basis_field = procurement_field(mapping, "supplier_resolution_basis")
    if basis_field and supplier_result.get("resolution_basis"):
        fields[basis_field] = clean_string(supplier_result.get("resolution_basis"))

    conflict_text = clean_string(supplier_result.get("blocker_comment"))
    conflicts_field = procurement_field(mapping, "supplier_conflicts")
    if conflicts_field and conflict_text:
        fields[conflicts_field] = conflict_text
    blocker_field = procurement_field(mapping, "blocker_comment")
    if blocker_field and conflict_text:
        fields[blocker_field] = conflict_text

    for logical_date_key, order_keys in {
        "supplier_dispatch_date": ("supplier_dispatch_date", "Отправка постав."),
        "cargo_dropoff_date": ("cargo_dropoff_date", "Сдача в карго"),
        "expected_receipt_date": ("expected_receipt_date", "Поступление"),
    }.items():
        field_name = procurement_field(mapping, logical_date_key)
        value = next((onec_order.get(key) for key in order_keys if onec_order.get(key)), None)
        if field_name and value:
            fields[field_name] = iso_value(value)

    return {
        "logical_key": logical_key,
        "stage_key": stage_key,
        "blocked_supplier": blocked_supplier,
        "fields": fields,
    }


def sync_suppliers_to_crm(
    api: Any,
    suppliers: list[dict[str, Any]],
    *,
    mapping: dict[str, Any] | None = None,
    apply: bool = False,
    assigned_by_id: str | int | None = None,
) -> dict[str, Any]:
    rows = [
        sync_supplier_to_crm(
            api,
            supplier,
            mapping=mapping,
            apply=apply,
            assigned_by_id=assigned_by_id,
        )
        for supplier in suppliers
    ]
    counts: dict[str, int] = {}
    for row in rows:
        status = clean_string(row.get("status")) or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {
        "generated_at": utcnow_iso(),
        "mode": "apply" if apply else "dry-run",
        "pii_policy": "phone/email masked; no bank details exported",
        "rows": rows,
        "summary": {"rows": len(rows), "status_counts": dict(sorted(counts.items()))},
    }
