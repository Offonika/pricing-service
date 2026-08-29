from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.services.importers.onec_mutual_settlements import (
    export_onec_mutual_settlements_opening_csv,
    onec_mutual_settlements_report_allows_implicit_zero_rows,
    parse_onec_mutual_settlements_current_balances,
    parse_onec_mutual_settlements_opening,
)
from tasks.inspect_onec_mutual_settlements_report import (
    inspect_onec_mutual_settlements_report,
)


def _build_uppercase_shared_strings_xlsx() -> bytes:
    shared_strings = [
        "Ведомость по взаиморасчетам с контрагентами",
        "Период: 1 января 2025 г.",
        "RMB",
        "4.Золотой",
        "РБ000001 ",
        "С покупателем",
        "РБ0040473",
        "01.01.2025 0:00:00",
        "01.01.2025 23:59:59",
        "не использовать",
        "РБ0049999",
        "Аванс",
    ]
    shared_items = "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
    shared_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="12" uniqueCount="12">
  {shared_items}
</sst>
"""

    sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="B1" t="s"><v>0</v></c></row>
    <row r="2"><c r="B2" t="s"><v>1</v></c></row>
    <row r="15">
      <c r="B15" t="s"><v>2</v></c><c r="D15"><v>0</v></c><c r="H15"><v>0</v></c>
    </row>
    <row r="16">
      <c r="B16" t="s"><v>3</v></c><c r="D16"><v>346645</v></c><c r="H16"><v>5412358.02</v></c>
    </row>
    <row r="17">
      <c r="B17" t="s"><v>4</v></c><c r="D17"><v>346645</v></c><c r="H17"><v>5412358.02</v></c>
    </row>
    <row r="18">
      <c r="B18" t="s"><v>5</v></c><c r="D18"><v>346645</v></c><c r="H18"><v>5412358.02</v></c>
    </row>
    <row r="19">
      <c r="B19" t="s"><v>6</v></c><c r="D19"><v>346645</v></c><c r="H19"><v>5412358.02</v></c>
    </row>
    <row r="20">
      <c r="D20"><v>346645</v></c><c r="H20"><v>5412358.02</v></c>
    </row>
    <row r="21">
      <c r="B21" t="s"><v>7</v></c><c r="D21"><v>346645</v></c><c r="H21"><v>5412358.02</v></c>
    </row>
    <row r="22">
      <c r="B22" t="s"><v>8</v></c><c r="D22"><v>346645</v></c><c r="H22"><v>5412358.02</v></c>
    </row>
    <row r="23">
      <c r="B23" t="s"><v>9</v></c><c r="D23"><v>10223</v></c><c r="H23"><v>10223</v></c>
    </row>
    <row r="24">
      <c r="B24" t="s"><v>4</v></c><c r="D24"><v>10223</v></c><c r="H24"><v>10223</v></c>
    </row>
    <row r="25">
      <c r="B25" t="s"><v>5</v></c><c r="D25"><v>10223</v></c><c r="H25"><v>10223</v></c>
    </row>
    <row r="26">
      <c r="B26" t="s"><v>10</v></c><c r="D26"><v>10223</v></c><c r="H26"><v>10223</v></c>
    </row>
    <row r="27">
      <c r="B27" t="s"><v>11</v></c><c r="D27"><v>10223</v></c><c r="H27"><v>10223</v></c>
    </row>
    <row r="28">
      <c r="B28" t="s"><v>7</v></c><c r="D28"><v>10223</v></c><c r="H28"><v>10223</v></c>
    </row>
    <row r="29">
      <c r="B29" t="s"><v>8</v></c><c r="D29"><v>10223</v></c><c r="H29"><v>10223</v></c>
    </row>
  </sheetData>
</worksheet>
"""
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="TDSheet" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="xl/workbook.xml"/>
</Relationships>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/SharedStrings.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
"""

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/SharedStrings.xml", shared_xml)
    return buffer.getvalue()


def _build_current_balances_xlsx_with_groups(*, filters: str | None = None) -> bytes:
    shared_strings = [
        "Ведомость по взаиморасчетам с контрагентами",
        "Период: Январь 2025 г. - Февраль 2026 г.",
        "MASTER MOBILE",
        "Покупатель A",
        "Договор с покупателем, руб",
        "Поставщик B",
        "Основной договор с поставщиком, руб",
        "Клиент C",
        "Основной договор, руб",
        "Показатели: Сумма (руб)(кон. остаток);",
        "Группировки строк: Организация; Контрагент; Договор контрагента;",
    ]
    if filters is not None:
        shared_strings.append(f"Отборы: {filters}")
    shared_items = "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
    shared_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">
  {shared_items}
</sst>
"""

    sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="B1" t="s"><v>0</v></c></row>
    <row r="2"><c r="B2" t="s"><v>1</v></c></row>
    <row r="21">
      <c r="B21" t="s"><v>2</v></c>
      <c r="F21"><v>111</v></c>
      <c r="J21"><v>999</v></c>
    </row>
    <row r="22" outlineLevel="1">
      <c r="B22" t="s"><v>3</v></c>
      <c r="F22"><v>100</v></c>
      <c r="J22"><v>150</v></c>
    </row>
    <row r="23" outlineLevel="2">
      <c r="B23" t="s"><v>4</v></c>
      <c r="F23"><v>100</v></c>
      <c r="J23"><v>150</v></c>
    </row>
    <row r="24" outlineLevel="1">
      <c r="B24" t="s"><v>5</v></c>
      <c r="F24"><v>200</v></c>
      <c r="J24"><v>250</v></c>
    </row>
    <row r="25" outlineLevel="2">
      <c r="B25" t="s"><v>6</v></c>
      <c r="F25"><v>200</v></c>
      <c r="J25"><v>250</v></c>
    </row>
    <row r="26" outlineLevel="1">
      <c r="B26" t="s"><v>7</v></c>
      <c r="F26"><v>300</v></c>
      <c r="J26"><v>350</v></c>
    </row>
    <row r="27" outlineLevel="2">
      <c r="B27" t="s"><v>8</v></c>
      <c r="F27"><v>300</v></c>
      <c r="J27"><v>350</v></c>
    </row>
  </sheetData>
</worksheet>
"""
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="TDSheet" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="xl/workbook.xml"/>
</Relationships>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/SharedStrings.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
"""

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/SharedStrings.xml", shared_xml)
    return buffer.getvalue()


def test_parse_onec_mutual_settlements_opening_flattens_hierarchy():
    rows = parse_onec_mutual_settlements_opening(_build_uppercase_shared_strings_xlsx())

    assert len(rows) == 2

    first = rows[0]
    assert first.snapshot_date.isoformat() == "2025-01-01"
    assert first.currency_name == "RMB"
    assert first.contract_name == "4.Золотой"
    assert first.counterparty_code == "РБ000001"
    assert first.contract_kind_name == "С покупателем"
    assert first.contract_code == "РБ0040473"
    assert first.settlement_document is None
    assert str(first.opening_balance_rub) == "5412358.02"

    second = rows[1]
    assert second.contract_name == "не использовать"
    assert second.settlement_document == "Аванс"
    assert str(second.opening_balance_rub) == "10223"


def test_export_onec_mutual_settlements_opening_csv(tmp_path: Path):
    rows = parse_onec_mutual_settlements_opening(_build_uppercase_shared_strings_xlsx())

    output = export_onec_mutual_settlements_opening_csv(rows, tmp_path / "opening.csv")

    content = output.read_text(encoding="utf-8").splitlines()
    assert content[0].startswith("snapshot_date,currency_name,contract_name")
    assert (
        "2025-01-01,RMB,4.Золотой,РБ000001,С покупателем,РБ0040473,,346645,5412358.02,20" in content
    )
    assert (
        "2025-01-01,RMB,не использовать,РБ000001,С покупателем,РБ0049999,Аванс,10223,10223,27"
        in content
    )


def test_parse_current_balances_uses_rub_column_and_filters_non_buyers():
    rows = parse_onec_mutual_settlements_current_balances(
        _build_current_balances_xlsx_with_groups()
    )

    by_name = {row.counterparty_name: row.current_balance_rub for row in rows}
    assert rows[0].snapshot_date.isoformat() == "2026-02-28"
    assert by_name["Покупатель A"] == Decimal("150")
    assert by_name["Клиент C"] == Decimal("350")
    assert "Поставщик B" not in by_name
    assert "MASTER MOBILE" not in by_name


def test_parse_current_balances_all_keeps_non_buyers():
    rows = parse_onec_mutual_settlements_current_balances(
        _build_current_balances_xlsx_with_groups(),
        counterparty_filter_mode="all",
    )

    by_name = {row.counterparty_name: row.current_balance_rub for row in rows}
    assert rows[0].snapshot_date.isoformat() == "2026-02-28"
    assert by_name == {
        "Покупатель A": Decimal("150"),
        "Поставщик B": Decimal("250"),
        "Клиент C": Decimal("350"),
    }
    assert sum(by_name.values(), Decimal("0")) == Decimal("750")


def test_implicit_zero_requires_native_unfiltered_report_scope():
    unfiltered = _build_current_balances_xlsx_with_groups()
    filtered = _build_current_balances_xlsx_with_groups(
        filters="Контрагент В группе из списка (ПОКУПАТЕЛИ);"
    )

    assert onec_mutual_settlements_report_allows_implicit_zero_rows(unfiltered) is True
    assert onec_mutual_settlements_report_allows_implicit_zero_rows(filtered) is False
    assert (
        onec_mutual_settlements_report_allows_implicit_zero_rows(
            _build_uppercase_shared_strings_xlsx()
        )
        is False
    )


def test_inspect_onec_mutual_settlements_report_exports_full_mode(tmp_path: Path):
    report_path = tmp_path / "full-report.xlsx"
    report_path.write_bytes(_build_current_balances_xlsx_with_groups())
    csv_path = tmp_path / "normalized.csv"

    result = inspect_onec_mutual_settlements_report(
        report_path,
        counterparty_filter_mode="all",
        export_csv_path=csv_path,
        top=2,
    )

    assert result["snapshot_date"].isoformat() == "2026-02-28"
    assert result["current_balances"]["counterparty_count"] == 3
    assert result["current_balances"]["total_balance"] == Decimal("750.00")
    assert result["xlsx_diagnostics"]["available"] is True
    assert result["xlsx_diagnostics"]["report_end_date"].isoformat() == "2026-02-28"
    assert {
        "row": 21,
        "label": "MASTER MOBILE",
        "current_balance_rub": Decimal("999.00"),
    } in result["xlsx_diagnostics"]["org_or_total_rows"]

    csv_content = csv_path.read_text(encoding="utf-8")
    assert "Поставщик B" in csv_content
    assert "2026-02-28,Клиент C,350,26" in csv_content
