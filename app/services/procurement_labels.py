from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import Settings, get_settings
from app.schemas.procurement_labels import (
    ProcurementLabelGenerateResponse,
    ProcurementLabelOrderPreview,
    ProcurementLabelRow,
)
from app.services.bank_payments_bitrix import BitrixDiskClient, BitrixDiskError

LABEL_WIDTH_MM = 58
LABEL_HEIGHT_MM = 40
LABEL_DPI = 300
LABEL_WIDTH_PX = round(LABEL_WIDTH_MM / 25.4 * LABEL_DPI)
LABEL_HEIGHT_PX = round(LABEL_HEIGHT_MM / 25.4 * LABEL_DPI)
BLOCK_NON_VED = "Карточка не в контуре ВЭД импорт"
BLOCK_MISSING_ONEC = "Не найден 1С-код товара"
BLOCK_MISSING_SKU = "Не найден SKU/артикул товара"
BLOCK_MISSING_BARCODE = "Не найден barcode/GTIN"
BLOCK_MISSING_CERTIFICATE = "Нет связки товара с сертификатом/ДС"

EAN_LEFT_ODD = {
    "0": "0001101",
    "1": "0011001",
    "2": "0010011",
    "3": "0111101",
    "4": "0100011",
    "5": "0110001",
    "6": "0101111",
    "7": "0111011",
    "8": "0110111",
    "9": "0001011",
}
EAN_LEFT_EVEN = {
    "0": "0100111",
    "1": "0110011",
    "2": "0011011",
    "3": "0100001",
    "4": "0011101",
    "5": "0111001",
    "6": "0000101",
    "7": "0010001",
    "8": "0001001",
    "9": "0010111",
}
EAN_RIGHT = {
    "0": "1110010",
    "1": "1100110",
    "2": "1101100",
    "3": "1000010",
    "4": "1011100",
    "5": "1001110",
    "6": "1010000",
    "7": "1000100",
    "8": "1001000",
    "9": "1110100",
}
EAN_PARITY = {
    "0": "OOOOOO",
    "1": "OOEOEE",
    "2": "OOEEOE",
    "3": "OOEEEO",
    "4": "OEOOEE",
    "5": "OEEOOE",
    "6": "OEEEOO",
    "7": "OEOEOE",
    "8": "OEOEEO",
    "9": "OEEOEO",
}


@dataclass(frozen=True)
class BitrixFileResult:
    file_id: str
    url: str


class ProcurementLabelsBitrixClient:
    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 10.0,
        urlopen: Any = urllib.request.urlopen,
    ) -> None:
        self.webhook_url = webhook_url.rstrip("/")
        self.timeout = timeout
        self._urlopen = urlopen

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.webhook_url}/{method}.json",
            data=json.dumps(params or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with self._urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            message = (
                f"Bitrix24 {method}: {payload['error']} " f"{payload.get('error_description', '')}"
            ).strip()
            raise RuntimeError(message)
        return payload

    def get_item(self, *, entity_type_id: int, item_id: str) -> dict[str, Any]:
        payload = self.call("crm.item.get", {"entityTypeId": entity_type_id, "id": item_id})
        result = payload.get("result") or {}
        item = result.get("item") if isinstance(result, dict) else {}
        if not isinstance(item, dict):
            raise RuntimeError("Bitrix24 crm.item.get returned invalid payload")
        return item

    def update_item(self, *, entity_type_id: int, item_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        self.call(
            "crm.item.update",
            {"entityTypeId": entity_type_id, "id": item_id, "fields": fields},
        )


def clean_string(value: Any) -> str:
    return str(value or "").strip()


def load_json_file(path: Path) -> Any:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_mapping(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path = Path(settings.procurement_labels_mapping_path)
    payload = load_json_file(path)
    return payload if isinstance(payload, dict) else {}


def crm_item_rest_field_name(field: str) -> str:
    raw = clean_string(field)
    if not raw:
        return ""
    builtins = {
        "ID": "id",
        "TITLE": "title",
        "STAGE_ID": "stageId",
        "CATEGORY_ID": "categoryId",
        "ASSIGNED_BY_ID": "assignedById",
    }
    upper = raw.upper()
    if upper in builtins:
        return builtins[upper]
    if upper.startswith("UF_CRM_"):
        parts = [part for part in raw.split("_")[2:] if part]
        return "ufCrm" + "".join(part[:1].upper() + part[1:].lower() for part in parts)
    return raw


def item_value(item: dict[str, Any], mapping: dict[str, Any], logical_key: str) -> Any:
    field = ((mapping.get("field_map") or {}).get(logical_key)) or logical_key
    candidates = [
        str(field),
        crm_item_rest_field_name(str(field)),
        logical_key,
    ]
    for key in candidates:
        if key in item:
            return item[key]
    return None


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    raw = clean_string(value).replace(" ", "").replace(",", ".")
    if not raw:
        return Decimal("0")
    return Decimal(raw)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def source_number_candidates(number: str) -> list[str]:
    original = clean_string(number)
    if not original:
        return []
    candidates = [original]
    match = re.match(r"^(.*?)(\d+)$", original)
    if not match:
        return candidates
    prefix, digits = match.groups()
    numeric = int(digits)
    for width in (len(digits) - 1, len(digits) + 1):
        if width <= 0:
            continue
        candidate = prefix + str(numeric).zfill(width)
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def normalize_contour(value: Any) -> str:
    token = clean_string(value).casefold().replace(" ", "").replace("_", "").replace("-", "")
    if token in {"vedimport", "вэдимпорт"}:
        return "ved_import"
    if token in {"cargo", "карго"}:
        return "cargo"
    if token in {"ordinary", "обычный", "обычная"}:
        return "ordinary"
    return token


def item_procurement_contour(item: dict[str, Any], mapping: dict[str, Any]) -> str:
    category_id = _int_or_none(item.get("categoryId") or item.get("CATEGORY_ID"))
    ved_category = _int_or_none(
        ((mapping.get("category_map") or {}).get("ved_import") or {}).get("id")
    )
    if category_id is not None and ved_category is not None and category_id == ved_category:
        return "ved_import"

    raw_value = item_value(item, mapping, "procurement_contour")
    enum_id = clean_string(
        ((mapping.get("enum_map") or {}).get("procurement_contour") or {}).get("ved_import")
    )
    if enum_id and clean_string(raw_value) == enum_id:
        return "ved_import"
    return normalize_contour(raw_value)


def item_onec_number(item: dict[str, Any], mapping: dict[str, Any]) -> str:
    value = clean_string(item_value(item, mapping, "onec_source_number"))
    if value:
        return value
    title = clean_string(item.get("title") or item.get("TITLE"))
    match = re.search(r"[А-ЯA-Z]{2,}\d{6,}", title)
    return match.group(0) if match else ""


def load_lookup_catalog(path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list):
        return {}
    result: dict[str, Any] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        for key_name in ("onec_item_code", "sku", "item_ref"):
            key = clean_string(row.get(key_name))
            if key:
                result[key] = row
    return result


def catalog_lookup(catalog: dict[str, Any], keys: list[str], value_key: str) -> str:
    for key in keys:
        if not key:
            continue
        value = catalog.get(key)
        if isinstance(value, dict):
            resolved = clean_string(value.get(value_key) or value.get(value_key.upper()))
            if resolved:
                return resolved
        elif isinstance(value, str):
            if value_key in {"barcode", "gtin", "certificate_id"}:
                return clean_string(value)
    return ""


def certificate_status(catalog: dict[str, Any], keys: list[str]) -> tuple[str, str, bool]:
    for key in keys:
        if not key:
            continue
        value = catalog.get(key)
        if not value:
            continue
        if isinstance(value, str):
            return "covered", clean_string(value), True
        if isinstance(value, dict):
            status = clean_string(
                value.get("status") or value.get("certificate_status") or "covered"
            )
            certificate_id = clean_string(
                value.get("certificate_id") or value.get("declaration_id") or value.get("number")
            )
            eac_allowed = bool(value.get("eac", status == "covered"))
            return status, certificate_id, eac_allowed and status == "covered"
    return "missing", "", False


def fetch_onec_supplier_order_lines(engine: Engine, onec_number: str) -> list[dict[str, Any]]:
    candidates = source_number_candidates(onec_number)
    if not candidates:
        return []
    params = {f"number_{idx}": value for idx, value in enumerate(candidates)}
    placeholders = ", ".join(f":number_{idx}" for idx in range(len(candidates)))
    query = text(f"""
        WITH target_doc AS (
          SELECT TOP 1 doc._IDRRef
          FROM dbo._Document133 AS doc WITH (NOLOCK)
          WHERE LTRIM(RTRIM(doc._Number)) IN ({placeholders})
          ORDER BY doc._Date_Time DESC
        )
        SELECT
          LTRIM(RTRIM(doc._Number)) AS order_number,
          vt._LineNo2516 AS line_no,
          CONVERT(varchar(34), item._IDRRef, 1) AS item_ref_hex,
          item._Code AS onec_item_code,
          item._Description AS item_name,
          item._Fld836 AS sku,
          unit._Description AS unit,
          vt._Fld2520 AS quantity,
          vt._Fld2529 AS price,
          vt._Fld2526 AS amount
        FROM target_doc
        JOIN dbo._Document133 AS doc WITH (NOLOCK)
          ON doc._IDRRef = target_doc._IDRRef
        JOIN dbo._Document133_VT2515 AS vt WITH (NOLOCK)
          ON vt._Document133_IDRRef = doc._IDRRef
        LEFT JOIN dbo._Reference62 AS item WITH (NOLOCK)
          ON item._IDRRef = vt._Fld2523RRef
        LEFT JOIN dbo._Reference41 AS unit WITH (NOLOCK)
          ON unit._IDRRef = vt._Fld2517RRef
        ORDER BY vt._LineNo2516 ASC
        """)
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(query, params).mappings()]


def build_preview_from_sources(
    *,
    item_id: str,
    entity_type_id: int,
    bitrix_item: dict[str, Any],
    onec_lines: list[dict[str, Any]],
    mapping: dict[str, Any],
    barcode_catalog: dict[str, Any] | None = None,
    certificate_catalog: dict[str, Any] | None = None,
) -> ProcurementLabelOrderPreview:
    barcode_catalog = barcode_catalog or {}
    certificate_catalog = certificate_catalog or {}
    title = clean_string(bitrix_item.get("title") or bitrix_item.get("TITLE"))
    contour = item_procurement_contour(bitrix_item, mapping)
    onec_number = item_onec_number(bitrix_item, mapping)
    global_blockers: list[str] = []
    if contour != "ved_import":
        global_blockers.append(BLOCK_NON_VED)
    if not onec_number:
        global_blockers.append("Не найден номер 1С заказа в карточке")
    if onec_number and not onec_lines:
        global_blockers.append(f"Не найдены строки заказа поставщику {onec_number} в 1С")

    rows: list[ProcurementLabelRow] = []
    for raw in onec_lines:
        onec_item_code = clean_string(raw.get("onec_item_code"))
        item_name = clean_string(raw.get("item_name"))
        sku = clean_string(raw.get("sku"))
        item_ref_hex = clean_string(raw.get("item_ref_hex"))
        keys = [onec_item_code, sku, item_ref_hex, item_name]
        barcode = (
            catalog_lookup(barcode_catalog, keys, "barcode")
            or catalog_lookup(barcode_catalog, keys, "gtin")
            or clean_string(raw.get("barcode") or raw.get("gtin"))
        )
        cert_status, cert_id, eac_allowed = certificate_status(certificate_catalog, keys)
        blockers: list[str] = []
        if not onec_item_code:
            blockers.append(BLOCK_MISSING_ONEC)
        if not sku:
            blockers.append(BLOCK_MISSING_SKU)
        if not barcode:
            blockers.append(BLOCK_MISSING_BARCODE)
        if cert_status != "covered" or not cert_id:
            blockers.append(BLOCK_MISSING_CERTIFICATE)
        rows.append(
            ProcurementLabelRow(
                line_no=int(raw.get("line_no") or 0),
                onec_item_code=onec_item_code,
                item_name=item_name,
                sku=sku,
                barcode=barcode,
                unit=clean_string(raw.get("unit")) or "шт",
                quantity=_decimal(raw.get("quantity")),
                price=_decimal(raw.get("price")) if raw.get("price") is not None else None,
                amount=_decimal(raw.get("amount")) if raw.get("amount") is not None else None,
                certificate_id=cert_id,
                certificate_status=cert_status,
                eac_allowed=eac_allowed,
                status="blocked" if blockers else "ready",
                blockers=blockers,
            )
        )

    blockers = global_blockers + [
        f"строка {row.line_no}: {message}" for row in rows for message in row.blockers
    ]
    blocked = bool(blockers)
    return ProcurementLabelOrderPreview(
        item_id=item_id,
        entity_type_id=entity_type_id,
        onec_number=onec_number,
        title=title,
        contour=contour,
        status="blocked" if blocked else "draft",
        ready=not blocked,
        blocked=blocked,
        blockers=blockers,
        rows=rows,
    )


def _safe_slug(value: str) -> str:
    text_value = clean_string(value) or "order"
    return re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "-", text_value).strip("-") or "order"


def next_artifact_version(artifact_dir: Path, onec_number: str) -> int:
    slug = _safe_slug(onec_number)
    pattern = re.compile(rf"^ved-labels-{re.escape(slug)}-v(\d+)\.zip$")
    max_version = 0
    if artifact_dir.exists():
        for path in artifact_dir.glob(f"ved-labels-{slug}-v*.zip"):
            match = pattern.match(path.name)
            if match:
                max_version = max(max_version, int(match.group(1)))
    return max_version + 1


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _trim_text(
    draw: ImageDraw.ImageDraw, text_value: str, font: ImageFont.ImageFont, width: int
) -> str:
    value = clean_string(text_value)
    if draw.textlength(value, font=font) <= width:
        return value
    suffix = "..."
    while value and draw.textlength(value + suffix, font=font) > width:
        value = value[:-1]
    return value + suffix if value else suffix


def _ean13_check_digit(first_12: str) -> str:
    total = 0
    for index, digit in enumerate(first_12):
        total += int(digit) * (1 if index % 2 == 0 else 3)
    return str((10 - (total % 10)) % 10)


def _ean13_bits(value: str) -> str | None:
    digits = re.sub(r"\D+", "", value)
    if len(digits) == 12:
        digits += _ean13_check_digit(digits)
    if len(digits) != 13:
        return None
    if _ean13_check_digit(digits[:12]) != digits[-1]:
        return None
    parity = EAN_PARITY[digits[0]]
    bits = "101"
    for digit, mode in zip(digits[1:7], parity, strict=True):
        bits += EAN_LEFT_ODD[digit] if mode == "O" else EAN_LEFT_EVEN[digit]
    bits += "01010"
    for digit in digits[7:]:
        bits += EAN_RIGHT[digit]
    bits += "101"
    return bits


def _draw_barcode(
    draw: ImageDraw.ImageDraw,
    *,
    barcode: str,
    left: int,
    top: int,
    width: int,
    height: int,
) -> None:
    bits = _ean13_bits(barcode)
    if not bits:
        for index in range(44):
            if (hash((barcode, index)) & 1) == 0:
                x0 = left + round(index * width / 44)
                x1 = left + round((index + 0.55) * width / 44)
                draw.rectangle((x0, top, x1, top + height), fill="black")
        return
    module = max(1, width // len(bits))
    barcode_width = module * len(bits)
    offset = left + max(0, (width - barcode_width) // 2)
    for index, bit in enumerate(bits):
        if bit == "1":
            x0 = offset + index * module
            draw.rectangle((x0, top, x0 + module - 1, top + height), fill="black")


def render_label_png(row: ProcurementLabelRow, path: Path) -> None:
    image = Image.new("RGB", (LABEL_WIDTH_PX, LABEL_HEIGHT_PX), "white")
    draw = ImageDraw.Draw(image)
    margin = 28
    title_font = _font(30, bold=True)
    text_font = _font(24)
    small_font = _font(20)
    eac_font = _font(44, bold=True)
    max_width = LABEL_WIDTH_PX - margin * 2

    draw.rectangle((0, 0, LABEL_WIDTH_PX - 1, LABEL_HEIGHT_PX - 1), outline="black", width=3)
    draw.text(
        (margin, 22),
        _trim_text(draw, row.sku, title_font, max_width),
        fill="black",
        font=title_font,
    )
    draw.text(
        (margin, 62),
        _trim_text(draw, row.item_name, text_font, max_width),
        fill="black",
        font=text_font,
    )
    draw.text((margin, 98), f"1C: {row.onec_item_code}", fill="black", font=small_font)
    if row.eac_allowed:
        draw.text((LABEL_WIDTH_PX - 120, 92), "EAC", fill="black", font=eac_font)

    barcode_top = 158
    _draw_barcode(
        draw,
        barcode=row.barcode,
        left=margin,
        top=barcode_top,
        width=max_width,
        height=140,
    )
    barcode_text = _trim_text(draw, row.barcode, title_font, max_width)
    text_width = draw.textlength(barcode_text, font=title_font)
    draw.text(
        ((LABEL_WIDTH_PX - text_width) / 2, barcode_top + 148),
        barcode_text,
        fill="black",
        font=title_font,
    )
    draw.text(
        (margin, LABEL_HEIGHT_PX - 40),
        _trim_text(draw, row.certificate_id, small_font, max_width),
        fill="black",
        font=small_font,
    )
    image.save(path, format="PNG", dpi=(LABEL_DPI, LABEL_DPI))


def write_register_xlsx(preview: ProcurementLabelOrderPreview, path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "label-register"
    headers = [
        "line_no",
        "onec_order",
        "onec_item_code",
        "sku",
        "barcode_gtin",
        "item_name",
        "qty",
        "unit",
        "certificate_id",
        "certificate_status",
        "eac_on_label",
        "status",
        "blockers",
    ]
    ws.append(headers)
    for row in preview.rows:
        ws.append(
            [
                row.line_no,
                preview.onec_number,
                row.onec_item_code,
                row.sku,
                row.barcode,
                row.item_name,
                float(row.quantity),
                row.unit,
                row.certificate_id,
                row.certificate_status,
                "yes" if row.eac_allowed else "no",
                row.status,
                "; ".join(row.blockers),
            ]
        )
    for column_cells in ws.columns:
        length = max(len(clean_string(cell.value)) for cell in column_cells)
        ws.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 10), 48)
    wb.save(path)


def write_factory_readme(preview: ProcurementLabelOrderPreview, path: Path) -> None:
    lines = [
        f"Партия: {preview.onec_number}",
        f"Карточка Bitrix: {preview.item_id}",
        "Формат этикетки: 58x40 мм, PNG, 300 dpi.",
        "На этикетке нет Честного знака и РСТ.",
        "EAC выводится только для строк, где товар связан с подтвержденной ДС/сертификатом.",
        "Печатать по одному макету на SKU; количество брать из label-register.xlsx.",
        "",
        "Файлы:",
        "- labels-preview/*.png — макеты товарных этикеток",
        "- label-register.xlsx — соответствие 1С -> SKU -> barcode/GTIN -> строка заказа",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_artifacts(
    preview: ProcurementLabelOrderPreview,
    *,
    artifact_dir: Path,
    base_url: str = "",
    version: int | None = None,
) -> tuple[int, Path, str]:
    if preview.blocked:
        raise ValueError("Cannot generate labels while preview is blocked")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    resolved_version = version or next_artifact_version(artifact_dir, preview.onec_number)
    slug = _safe_slug(preview.onec_number)
    work_dir = artifact_dir / f"{slug}-v{resolved_version}"
    labels_dir = work_dir / "labels-preview"
    labels_dir.mkdir(parents=True, exist_ok=True)

    for row in preview.rows:
        filename = f"line-{row.line_no:03d}-{_safe_slug(row.sku or row.onec_item_code)}.png"
        render_label_png(row, labels_dir / filename)
    write_register_xlsx(preview, work_dir / "label-register.xlsx")
    write_factory_readme(preview, work_dir / "factory-labels-readme.txt")

    zip_path = artifact_dir / f"ved-labels-{slug}-v{resolved_version}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(work_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(work_dir).as_posix())

    if base_url:
        zip_url = f"{base_url.rstrip('/')}/api/procurement-labels/artifacts/{quote(zip_path.name)}"
    else:
        zip_url = zip_path.as_posix()
    return resolved_version, zip_path, zip_url


def upload_zip_to_disk(
    *,
    settings: Settings,
    zip_path: Path,
) -> BitrixFileResult | None:
    webhook_url = (
        settings.procurement_labels_bitrix_webhook_url
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
    )
    if not webhook_url:
        return None
    if not settings.procurement_labels_bitrix_root_folder_id:
        return None
    client = BitrixDiskClient(
        webhook_url,
        timeout=int(settings.procurement_labels_bitrix_rest_timeout_seconds),
    )
    try:
        result = client.upload_file(
            int(settings.procurement_labels_bitrix_root_folder_id),
            filename=zip_path.name,
            content=zip_path.read_bytes(),
        )
    except BitrixDiskError as exc:
        raise RuntimeError(str(exc)) from exc
    file_id = clean_string(result.get("ID") or result.get("id") or result.get("fileId"))
    url = clean_string(
        result.get("DETAIL_URL")
        or result.get("detailUrl")
        or result.get("DOWNLOAD_URL")
        or result.get("downloadUrl")
    )
    return BitrixFileResult(file_id=file_id, url=url)


def label_status_fields(
    mapping: dict[str, Any],
    *,
    status: str,
    version: int | None = None,
    zip_url: str | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    field_map = mapping.get("field_map") or {}
    enum_map = mapping.get("enum_map") or {}
    status_field = clean_string(field_map.get("label_generation_status"))
    if status_field:
        enum_id = clean_string((enum_map.get("label_generation_status") or {}).get(status))
        fields[crm_item_rest_field_name(status_field)] = enum_id or status
    version_field = clean_string(field_map.get("label_generation_version"))
    if version_field and version is not None:
        fields[crm_item_rest_field_name(version_field)] = version
    zip_field = clean_string(field_map.get("label_generation_zip_url"))
    if zip_field and zip_url:
        fields[crm_item_rest_field_name(zip_field)] = zip_url
    errors_field = clean_string(field_map.get("label_generation_errors"))
    if errors_field:
        fields[crm_item_rest_field_name(errors_field)] = "\n".join(errors or [])
    approved_at_field = clean_string(field_map.get("label_generation_approved_at"))
    if approved_at_field and status == "approved":
        fields[crm_item_rest_field_name(approved_at_field)] = datetime.now(UTC).isoformat()
    return fields


def update_bitrix_label_status(
    *,
    client: ProcurementLabelsBitrixClient | None,
    entity_type_id: int,
    item_id: str,
    mapping: dict[str, Any],
    status: str,
    version: int | None = None,
    zip_url: str | None = None,
    errors: list[str] | None = None,
) -> None:
    if client is None:
        return
    fields = label_status_fields(
        mapping,
        status=status,
        version=version,
        zip_url=zip_url,
        errors=errors,
    )
    client.update_item(entity_type_id=entity_type_id, item_id=item_id, fields=fields)


def bitrix_client_from_settings(settings: Settings) -> ProcurementLabelsBitrixClient | None:
    webhook_url = (
        settings.procurement_labels_bitrix_webhook_url
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
    )
    if not webhook_url:
        return None
    return ProcurementLabelsBitrixClient(
        webhook_url,
        timeout=settings.procurement_labels_bitrix_rest_timeout_seconds,
    )


def onec_engine_from_settings(settings: Settings) -> Engine:
    if not settings.onec_database_url:
        raise HTTPException(status_code=500, detail="1C database URL is not configured")
    return create_engine(settings.onec_database_url, pool_pre_ping=True)


def build_preview(
    item_id: str,
    *,
    settings: Settings | None = None,
    bitrix_client: ProcurementLabelsBitrixClient | None = None,
    onec_engine: Engine | None = None,
) -> ProcurementLabelOrderPreview:
    settings = settings or get_settings()
    mapping = load_mapping(settings)
    entity_type_id = int(
        ((mapping.get("process") or {}).get("entity_type_id"))
        or settings.procurement_labels_entity_type_id
    )
    client = bitrix_client or bitrix_client_from_settings(settings)
    if client is None:
        raise HTTPException(status_code=500, detail="Bitrix procurement webhook is not configured")
    item = client.get_item(entity_type_id=entity_type_id, item_id=item_id)
    onec_number = item_onec_number(item, mapping)
    engine = onec_engine or onec_engine_from_settings(settings)
    onec_lines = fetch_onec_supplier_order_lines(engine, onec_number) if onec_number else []
    barcode_catalog = load_lookup_catalog(Path(settings.procurement_labels_barcode_catalog_path))
    certificate_catalog = load_lookup_catalog(
        Path(settings.procurement_labels_certificate_catalog_path)
    )
    return build_preview_from_sources(
        item_id=item_id,
        entity_type_id=entity_type_id,
        bitrix_item=item,
        onec_lines=onec_lines,
        mapping=mapping,
        barcode_catalog=barcode_catalog,
        certificate_catalog=certificate_catalog,
    )


def generate_zip(
    item_id: str,
    *,
    settings: Settings | None = None,
    bitrix_client: ProcurementLabelsBitrixClient | None = None,
    onec_engine: Engine | None = None,
    base_url: str = "",
    dry_run: bool = False,
) -> ProcurementLabelGenerateResponse:
    settings = settings or get_settings()
    mapping = load_mapping(settings)
    client = bitrix_client or bitrix_client_from_settings(settings)
    preview = build_preview(
        item_id,
        settings=settings,
        bitrix_client=client,
        onec_engine=onec_engine,
    )
    if preview.blocked:
        if not dry_run:
            update_bitrix_label_status(
                client=client,
                entity_type_id=preview.entity_type_id,
                item_id=item_id,
                mapping=mapping,
                status="blocked",
                errors=preview.blockers,
            )
        return ProcurementLabelGenerateResponse(preview=preview, generated=False)

    version, zip_path, zip_url = build_artifacts(
        preview,
        artifact_dir=Path(settings.procurement_labels_artifact_dir),
        base_url=base_url,
    )
    disk_file = None if dry_run else upload_zip_to_disk(settings=settings, zip_path=zip_path)
    final_url = disk_file.url if disk_file and disk_file.url else zip_url
    preview.artifact_version = version
    preview.zip_url = final_url
    preview.disk_file_id = disk_file.file_id if disk_file else None
    if not dry_run:
        update_bitrix_label_status(
            client=client,
            entity_type_id=preview.entity_type_id,
            item_id=item_id,
            mapping=mapping,
            status="draft",
            version=version,
            zip_url=final_url,
            errors=[],
        )
    return ProcurementLabelGenerateResponse(
        preview=preview,
        generated=True,
        artifact_version=version,
        zip_filename=zip_path.name,
        zip_url=final_url,
        disk_file_id=disk_file.file_id if disk_file else None,
    )


def approve_zip(
    item_id: str,
    *,
    settings: Settings | None = None,
    bitrix_client: ProcurementLabelsBitrixClient | None = None,
) -> tuple[int | None, str | None]:
    settings = settings or get_settings()
    mapping = load_mapping(settings)
    client = bitrix_client or bitrix_client_from_settings(settings)
    entity_type_id = int(
        ((mapping.get("process") or {}).get("entity_type_id"))
        or settings.procurement_labels_entity_type_id
    )
    preview = build_preview(item_id, settings=settings, bitrix_client=client)
    version = preview.artifact_version
    zip_url = preview.zip_url
    update_bitrix_label_status(
        client=client,
        entity_type_id=entity_type_id,
        item_id=item_id,
        mapping=mapping,
        status="approved",
        version=version,
        zip_url=zip_url,
        errors=preview.blockers,
    )
    return version, zip_url
