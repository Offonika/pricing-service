from __future__ import annotations

import calendar
import csv
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Literal

import xlrd

OOXML_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
CONTRACT_KIND_NAMES = {"С покупателем", "С поставщиком", "Прочее"}
PERIOD_ROW_PATTERN = re.compile(r"^\d{2}\.\d{2}\.\d{4}(?: \d{1,2}:\d{2}:\d{2})?$")
COUNTERPARTY_CODE_PATTERN = re.compile(r"^[A-ZА-Я]{0,3}\d{1,}$")
CONTRACT_CODE_PATTERN = re.compile(r"^[A-ZА-Я]{0,3}\d{4,}$")
CurrentBalanceCounterpartyFilterMode = Literal["buyers", "all"]
RUS_MONTHS = {
    "январь": 1,
    "января": 1,
    "февраль": 2,
    "февраля": 2,
    "март": 3,
    "марта": 3,
    "апрель": 4,
    "апреля": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июль": 7,
    "июля": 7,
    "август": 8,
    "августа": 8,
    "сентябрь": 9,
    "сентября": 9,
    "октябрь": 10,
    "октября": 10,
    "ноябрь": 11,
    "ноября": 11,
    "декабрь": 12,
    "декабря": 12,
}


@dataclass(frozen=True)
class OneCMutualSettlementOpeningRow:
    snapshot_date: date
    currency_name: str
    contract_name: str
    counterparty_code: str
    contract_kind_name: str
    contract_code: str
    settlement_document: str | None
    opening_balance: Decimal
    opening_balance_rub: Decimal
    source_row: int


@dataclass(frozen=True)
class OneCMutualSettlementCurrentBalanceRow:
    snapshot_date: date
    counterparty_name: str
    current_balance_rub: Decimal
    source_row: int


@dataclass(frozen=True)
class _SheetRow:
    row_number: int
    outline_level: int
    label: str
    registrator: str
    opening_balance: Decimal | None
    opening_balance_rub: Decimal | None
    current_balance_rub: Decimal | None


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.replace("\xa0", " ").split())


def _parse_decimal(value: str | None) -> Decimal | None:
    cleaned = _normalize_text(value)
    if not cleaned:
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _parse_report_period(value: str) -> date:
    match = re.search(r"период:\s*(\d{1,2})\s+([а-я]+)\s+(\d{4})", value.lower())
    if not match:
        raise ValueError(f"Не удалось распознать дату периода: {value}")
    day = int(match.group(1))
    month_name = match.group(2)
    year = int(match.group(3))
    month = RUS_MONTHS.get(month_name)
    if month is None:
        raise ValueError(f"Неизвестный месяц в периоде: {value}")
    return date(year, month, day)


def _parse_report_period_end(value: str) -> date:
    range_matches = re.findall(r"\d{2}\.\d{2}\.\d{4}", value)
    if range_matches:
        day, month, year = range_matches[-1].split(".")
        return date(int(year), int(month), int(day))
    month_range_match = re.search(
        r"-\s*([А-Яа-яA-Za-z]+)\s+(\d{4})\s*г\.?", value, flags=re.IGNORECASE
    )
    if month_range_match:
        month_name = month_range_match.group(1).lower()
        year = int(month_range_match.group(2))
        month = RUS_MONTHS.get(month_name)
        if month is None:
            raise ValueError(f"Неизвестный месяц в периоде: {value}")
        return date(year, month, calendar.monthrange(year, month)[1])
    return _parse_report_period(value)


def _col_to_num(col: str) -> int:
    result = 0
    for char in col:
        if char.isalpha():
            result = result * 26 + ord(char.upper()) - 64
    return result


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    shared_path = None
    for candidate in ("xl/sharedStrings.xml", "xl/SharedStrings.xml"):
        if candidate in zf.namelist():
            shared_path = candidate
            break
    if shared_path is None:
        return []
    root = ET.fromstring(zf.read(shared_path))
    shared: list[str] = []
    for item in root.findall("m:si", OOXML_NS):
        parts = [node.text or "" for node in item.iterfind(".//m:t", OOXML_NS)]
        shared.append("".join(parts))
    return shared


def _sheet_target(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    relationships = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships}
    first_sheet = workbook.find("m:sheets/m:sheet", OOXML_NS)
    if first_sheet is None:
        raise ValueError("В workbook.xml нет листов")
    rel_id = first_sheet.attrib[
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    ]
    target = rel_map[rel_id]
    if not target.startswith("worksheets/"):
        raise ValueError(f"Неожиданный target листа: {target}")
    return f"xl/{target}"


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iterfind(".//m:t", OOXML_NS))
    value_node = cell.find("m:v", OOXML_NS)
    if value_node is None:
        return ""
    raw_value = value_node.text or ""
    if cell_type == "s":
        return shared_strings[int(raw_value)]
    return raw_value


def _load_rows(content: bytes) -> list[_SheetRow]:
    with zipfile.ZipFile(BytesIO(content)) as zf:
        shared_strings = _load_shared_strings(zf)
        sheet = ET.fromstring(zf.read(_sheet_target(zf)))
    rows: list[_SheetRow] = []
    sheet_data = sheet.find("m:sheetData", OOXML_NS)
    if sheet_data is None:
        return []
    for row in sheet_data:
        values: dict[int, str] = {}
        for cell in row.findall("m:c", OOXML_NS):
            ref = cell.attrib["r"]
            col = "".join(char for char in ref if char.isalpha())
            values[_col_to_num(col)] = _cell_value(cell, shared_strings)
        if not values:
            continue
        rows.append(
            _SheetRow(
                row_number=int(row.attrib["r"]),
                outline_level=int(row.attrib.get("outlineLevel", "0")),
                label=_normalize_text(values.get(2)),
                registrator=_normalize_text(values.get(3)),
                opening_balance=_parse_decimal(values.get(4)),
                opening_balance_rub=_parse_decimal(values.get(8)),
                # In 1C mutual settlements report current RUB balance is in
                # "Сумма (руб) -> кон. остаток" column (J).
                # Keep fallback to F for older/uniform exports without RUB block.
                current_balance_rub=_parse_decimal(values.get(10)) or _parse_decimal(values.get(6)),
            )
        )
    return rows


def _parse_snapshot_date(rows: list[_SheetRow]) -> date:
    for row in rows[:10]:
        if row.label.startswith("Период:"):
            return _parse_report_period(row.label)
    raise ValueError("В отчете не найдена строка периода")


def _parse_report_end_date(rows: list[_SheetRow]) -> date:
    for row in rows[:10]:
        if row.label.startswith("Период:"):
            return _parse_report_period_end(row.label)
    raise ValueError("В отчете не найдена строка периода")


def _is_currency_row(label: str) -> bool:
    return label.lower() == "руб" or bool(re.fullmatch(r"[A-Z]{3,5}", label))


def _is_counterparty_code(label: str) -> bool:
    return bool(COUNTERPARTY_CODE_PATTERN.fullmatch(label.strip()))


def _is_contract_code(label: str) -> bool:
    return bool(CONTRACT_CODE_PATTERN.fullmatch(label.strip()))


def _is_period_row(label: str) -> bool:
    return bool(PERIOD_ROW_PATTERN.fullmatch(label))


def _is_data_row(row: _SheetRow) -> bool:
    return row.opening_balance is not None or row.opening_balance_rub is not None


def _looks_like_contract_block(rows: list[_SheetRow], start: int) -> bool:
    if start + 3 >= len(rows):
        return False
    row0, row1, row2, row3 = rows[start : start + 4]
    if not all(_is_data_row(item) for item in (row0, row1, row2, row3)):
        return False
    if not row0.label or _is_period_row(row0.label):
        return False
    return (
        _is_counterparty_code(row1.label)
        and row2.label in CONTRACT_KIND_NAMES
        and _is_contract_code(row3.label)
    )


def parse_onec_mutual_settlements_opening(content: bytes) -> list[OneCMutualSettlementOpeningRow]:
    all_rows = _load_rows(content)
    rows = [row for row in all_rows if _is_data_row(row)]
    snapshot_date = _parse_snapshot_date(all_rows)

    data_rows = [row for row in rows if row.row_number >= 15]
    normalized: list[OneCMutualSettlementOpeningRow] = []
    i = 0
    current_currency = ""

    while i < len(data_rows):
        row = data_rows[i]
        if _is_period_row(row.label):
            i += 1
            continue
        if not current_currency:
            current_currency = row.label
            i += 1
            continue
        if _is_currency_row(row.label) and not _looks_like_contract_block(data_rows, i):
            current_currency = row.label
            i += 1
            continue
        if not _looks_like_contract_block(data_rows, i):
            i += 1
            continue

        contract_name = data_rows[i].label
        counterparty_code = data_rows[i + 1].label
        contract_kind_name = data_rows[i + 2].label
        contract_code = data_rows[i + 3].label
        i += 4

        while i < len(data_rows):
            settlement_row = data_rows[i]
            if _is_period_row(settlement_row.label):
                i += 1
                continue
            if _is_currency_row(settlement_row.label) and not _looks_like_contract_block(
                data_rows, i
            ):
                current_currency = settlement_row.label
                break
            if _looks_like_contract_block(data_rows, i):
                break

            normalized.append(
                OneCMutualSettlementOpeningRow(
                    snapshot_date=snapshot_date,
                    currency_name=current_currency,
                    contract_name=contract_name,
                    counterparty_code=counterparty_code,
                    contract_kind_name=contract_kind_name,
                    contract_code=contract_code,
                    settlement_document=settlement_row.label or None,
                    opening_balance=settlement_row.opening_balance or Decimal("0"),
                    opening_balance_rub=settlement_row.opening_balance_rub or Decimal("0"),
                    source_row=settlement_row.row_number,
                )
            )
            i += 1
            while i < len(data_rows) and _is_period_row(data_rows[i].label):
                i += 1

    return normalized


def parse_onec_mutual_settlements_opening_file(
    path: Path,
) -> list[OneCMutualSettlementOpeningRow]:
    return parse_onec_mutual_settlements_opening(path.read_bytes())


def load_onec_mutual_settlements_opening_csv(
    path: Path,
) -> list[OneCMutualSettlementOpeningRow]:
    rows: list[OneCMutualSettlementOpeningRow] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                OneCMutualSettlementOpeningRow(
                    snapshot_date=date.fromisoformat(row["snapshot_date"]),
                    currency_name=_normalize_text(row.get("currency_name")),
                    contract_name=_normalize_text(row.get("contract_name")),
                    counterparty_code=_normalize_text(row.get("counterparty_code")),
                    contract_kind_name=_normalize_text(row.get("contract_kind_name")),
                    contract_code=_normalize_text(row.get("contract_code")),
                    settlement_document=_normalize_text(row.get("settlement_document")) or None,
                    opening_balance=_parse_decimal(row.get("opening_balance")) or Decimal("0"),
                    opening_balance_rub=_parse_decimal(row.get("opening_balance_rub"))
                    or Decimal("0"),
                    source_row=int(row["source_row"]),
                )
            )
    return rows


def parse_onec_mutual_settlements_current_balances(
    content: bytes,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> list[OneCMutualSettlementCurrentBalanceRow]:
    rows = _load_rows(content)
    snapshot_date = _parse_report_end_date(rows)
    return _parse_current_balance_rows(
        rows,
        snapshot_date=snapshot_date,
        counterparty_filter_mode=counterparty_filter_mode,
    )


def _parse_current_balance_rows(
    rows: list[_SheetRow],
    *,
    snapshot_date: date,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> list[OneCMutualSettlementCurrentBalanceRow]:
    if counterparty_filter_mode not in {"buyers", "all"}:
        raise ValueError(f"Неизвестный режим фильтра контрагентов: {counterparty_filter_mode}")

    skip_labels = {
        "Организация",
        "Контрагент",
        "Договор контрагента",
        "Период",
        "Документ движения (регистратор)",
        "Сумма (руб)",
        "СОТРУДНИКИ",
        "MASTER MOBILE",
        "Итог",
    }
    normalized: list[OneCMutualSettlementCurrentBalanceRow] = []
    has_hierarchy = any(item.outline_level > 0 for item in rows)
    counterparty_outline_level = 1 if has_hierarchy else 0

    def is_buyer_counterparty(row_index: int) -> bool:
        current_row = rows[row_index]
        has_buyer_contract = False
        has_non_buyer_contract = False
        index = row_index + 1
        while index < len(rows):
            child = rows[index]
            if child.outline_level <= current_row.outline_level:
                break
            label = (child.label or "").casefold()
            if "покупател" in label:
                has_buyer_contract = True
            if any(token in label for token in ("поставщ", "поставк", "сотрудник")):
                has_non_buyer_contract = True
            index += 1
        if has_buyer_contract:
            return True
        if has_non_buyer_contract:
            return False
        return True

    for row_index, row in enumerate(rows):
        if row.outline_level != counterparty_outline_level:
            continue
        if not row.label or row.label in skip_labels or row.label.startswith("Итого"):
            continue
        if ", руб" in row.label:
            continue
        if row.current_balance_rub is None:
            continue
        if counterparty_filter_mode == "buyers" and not is_buyer_counterparty(row_index):
            continue
        normalized.append(
            OneCMutualSettlementCurrentBalanceRow(
                snapshot_date=snapshot_date,
                counterparty_name=row.label,
                current_balance_rub=row.current_balance_rub,
                source_row=row.row_number,
            )
        )
    return normalized


def parse_onec_mutual_settlements_current_balances_file(
    path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> list[OneCMutualSettlementCurrentBalanceRow]:
    return parse_onec_mutual_settlements_current_balances(
        path.read_bytes(),
        counterparty_filter_mode=counterparty_filter_mode,
    )


def parse_onec_mutual_settlements_current_balances_xls_file(
    path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> list[OneCMutualSettlementCurrentBalanceRow]:
    workbook = xlrd.open_workbook(path)
    sheet = workbook.sheet_by_index(0)

    rows: list[_SheetRow] = []
    snapshot_date: date | None = None
    for row_idx in range(sheet.nrows):
        label = _normalize_text(sheet.cell_value(row_idx, 1) if sheet.ncols > 1 else "")
        if snapshot_date is None and label.startswith("Период:"):
            snapshot_date = _parse_report_period_end(label)
        current_balance_rub = (
            _parse_decimal(str(sheet.cell_value(row_idx, 9))) if sheet.ncols > 9 else None
        )
        if current_balance_rub is None and sheet.ncols > 5:
            current_balance_rub = _parse_decimal(str(sheet.cell_value(row_idx, 5)))
        rows.append(
            _SheetRow(
                row_number=row_idx + 1,
                outline_level=0,
                label=label,
                registrator="",
                opening_balance=None,
                opening_balance_rub=None,
                current_balance_rub=current_balance_rub,
            )
        )

    if snapshot_date is None:
        raise ValueError("В xls-отчете не найдена строка периода")

    return _parse_current_balance_rows(
        rows,
        snapshot_date=snapshot_date,
        counterparty_filter_mode=counterparty_filter_mode,
    )


def load_onec_mutual_settlements_current_balances_csv(
    path: Path,
) -> list[OneCMutualSettlementCurrentBalanceRow]:
    rows: list[OneCMutualSettlementCurrentBalanceRow] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            balance = _parse_decimal(row.get("current_balance_rub"))
            if balance is None:
                continue
            rows.append(
                OneCMutualSettlementCurrentBalanceRow(
                    snapshot_date=date.fromisoformat(row["snapshot_date"]),
                    counterparty_name=_normalize_text(row.get("counterparty_name")),
                    current_balance_rub=balance,
                    source_row=int(row.get("source_row") or 0),
                )
            )
    return rows


def load_onec_mutual_settlements_current_balances_file(
    path: Path,
    *,
    counterparty_filter_mode: CurrentBalanceCounterpartyFilterMode = "buyers",
) -> list[OneCMutualSettlementCurrentBalanceRow]:
    if path.suffix.lower() == ".csv":
        return load_onec_mutual_settlements_current_balances_csv(path)
    if path.suffix.lower() == ".xls":
        return parse_onec_mutual_settlements_current_balances_xls_file(
            path,
            counterparty_filter_mode=counterparty_filter_mode,
        )
    return parse_onec_mutual_settlements_current_balances_file(
        path,
        counterparty_filter_mode=counterparty_filter_mode,
    )


def load_onec_mutual_settlements_opening_file(
    path: Path,
) -> list[OneCMutualSettlementOpeningRow]:
    if path.suffix.lower() == ".csv":
        return load_onec_mutual_settlements_opening_csv(path)
    return parse_onec_mutual_settlements_opening_file(path)


def export_onec_mutual_settlements_opening_csv(
    rows: list[OneCMutualSettlementOpeningRow],
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "snapshot_date",
                "currency_name",
                "contract_name",
                "counterparty_code",
                "contract_kind_name",
                "contract_code",
                "settlement_document",
                "opening_balance",
                "opening_balance_rub",
                "source_row",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.snapshot_date.isoformat(),
                    row.currency_name,
                    row.contract_name,
                    row.counterparty_code,
                    row.contract_kind_name,
                    row.contract_code,
                    row.settlement_document or "",
                    f"{row.opening_balance:f}",
                    f"{row.opening_balance_rub:f}",
                    row.source_row,
                ]
            )
    return output_path


__all__ = [
    "CurrentBalanceCounterpartyFilterMode",
    "OneCMutualSettlementOpeningRow",
    "export_onec_mutual_settlements_opening_csv",
    "load_onec_mutual_settlements_opening_csv",
    "load_onec_mutual_settlements_opening_file",
    "parse_onec_mutual_settlements_opening",
    "parse_onec_mutual_settlements_opening_file",
]
