from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "docs" / "Instruction.ExpertiseWave1.Team.md"
OUTPUT_PATH = ROOT / "build" / "docs" / "Instruction.ExpertiseWave1.Team.docx"


@dataclass(frozen=True)
class Paragraph:
    text: str
    style: str


@dataclass(frozen=True)
class Bullet:
    text: str
    level: int = 0


@dataclass(frozen=True)
class Table:
    rows: list[tuple[str, str]]


Block = Paragraph | Bullet | Table


def _read_source() -> str:
    return SOURCE_PATH.read_text(encoding="utf-8")


def _strip_inline_markup(value: str) -> str:
    return value.replace("`", "").replace("**", "").strip()


def _parse_markdown(markdown: str) -> tuple[str, str, list[Block]]:
    title = ""
    subtitle = f"Редакция от {date.today().strftime('%d.%m.%Y')}"
    blocks: list[Block] = []
    current_section = ""
    status_rows: list[tuple[str, str]] = []

    def flush_status_rows() -> None:
        nonlocal status_rows
        if status_rows:
            blocks.append(Table(rows=status_rows))
            status_rows = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_status_rows()
            continue

        if stripped.startswith("# "):
            title = _strip_inline_markup(stripped[2:])
            continue

        if stripped.startswith("Дата актуализации:"):
            subtitle = _strip_inline_markup(stripped)
            continue

        if stripped.startswith("## "):
            flush_status_rows()
            current_section = _strip_inline_markup(stripped[3:])
            blocks.append(Paragraph(text=current_section, style="Heading1Custom"))
            continue

        if stripped.startswith("### "):
            flush_status_rows()
            blocks.append(
                Paragraph(text=_strip_inline_markup(stripped[4:]), style="Heading2Custom")
            )
            continue

        if stripped.endswith(":") and not stripped.startswith("- "):
            flush_status_rows()
            blocks.append(Paragraph(text=_strip_inline_markup(stripped), style="Label"))
            continue

        if stripped.startswith("- "):
            bullet_text = _strip_inline_markup(stripped[2:])
            if current_section == "4. Что означают статусы" and " — " in bullet_text:
                left, right = bullet_text.split(" — ", 1)
                status_rows.append((left.strip(), right.strip()))
            else:
                flush_status_rows()
                blocks.append(Bullet(text=bullet_text))
            continue

        flush_status_rows()
        style = "BodyText"
        if current_section == "1. Что изменилось" and stripped.startswith("Старый чат "):
            style = "WarningBox"
        elif current_section == "5. Что делает система автоматически" and stripped.startswith(
            "Плановые фоновые процессы"
        ):
            style = "Label"
        blocks.append(Paragraph(text=_strip_inline_markup(stripped), style=style))

    flush_status_rows()
    return title, subtitle, blocks


def _xml_header() -> str:
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _content_types_xml() -> str:
    return _xml_header() + """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
""".strip()


def _rels_xml() -> str:
    return _xml_header() + """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
""".strip()


def _document_rels_xml() -> str:
    return _xml_header() + """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>
""".strip()


def _core_xml(title: str) -> str:
    return _xml_header() + f"""
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(title)}</dc:title>
  <dc:subject>Экспертиза Wave 1</dc:subject>
  <dc:creator>Codex</dc:creator>
  <cp:keywords>Экспертиза, Bitrix24, 1С, ОКК</cp:keywords>
  <dc:description>Рабочая инструкция для команды по контуру Экспертиза Wave 1</dc:description>
</cp:coreProperties>
""".strip()


def _app_xml() -> str:
    return _xml_header() + """
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex</Application>
</Properties>
""".strip()


def _styles_xml() -> str:
    return _xml_header() + """
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Aptos" w:hAnsi="Aptos" w:cs="Aptos"/>
        <w:sz w:val="22"/>
        <w:szCs w:val="22"/>
        <w:color w:val="243447"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr>
        <w:spacing w:after="120" w:line="276" w:lineRule="auto"/>
      </w:pPr>
    </w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:rPr>
      <w:rFonts w:ascii="Aptos" w:hAnsi="Aptos" w:cs="Aptos"/>
      <w:sz w:val="22"/>
      <w:szCs w:val="22"/>
      <w:color w:val="243447"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="TitleCustom">
    <w:name w:val="Title Custom"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:jc w:val="center"/>
      <w:spacing w:before="240" w:after="140"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:sz w:val="34"/>
      <w:szCs w:val="34"/>
      <w:color w:val="0F4C81"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="SubtitleCustom">
    <w:name w:val="Subtitle Custom"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:jc w:val="center"/>
      <w:spacing w:after="260"/>
    </w:pPr>
    <w:rPr>
      <w:sz w:val="22"/>
      <w:szCs w:val="22"/>
      <w:color w:val="5B6770"/>
      <w:i/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="LeadBox">
    <w:name w:val="Lead Box"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:after="180"/>
      <w:ind w:left="180" w:right="180"/>
      <w:shd w:val="clear" w:color="auto" w:fill="EAF3FF"/>
      <w:pBdr>
        <w:top w:val="single" w:sz="8" w:space="1" w:color="9FBAD0"/>
        <w:left w:val="single" w:sz="8" w:space="1" w:color="9FBAD0"/>
        <w:bottom w:val="single" w:sz="8" w:space="1" w:color="9FBAD0"/>
        <w:right w:val="single" w:sz="8" w:space="1" w:color="9FBAD0"/>
      </w:pBdr>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:color w:val="1F2D3D"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1Custom">
    <w:name w:val="Heading 1 Custom"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:spacing w:before="240" w:after="100"/>
      <w:outlineLvl w:val="0"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:sz w:val="28"/>
      <w:szCs w:val="28"/>
      <w:color w:val="0F4C81"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2Custom">
    <w:name w:val="Heading 2 Custom"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:spacing w:before="160" w:after="60"/>
      <w:outlineLvl w:val="1"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:sz w:val="24"/>
      <w:szCs w:val="24"/>
      <w:color w:val="154360"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Label">
    <w:name w:val="Label"/>
    <w:basedOn w:val="Normal"/>
    <w:rPr>
      <w:b/>
      <w:color w:val="0F4C81"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="BodyText">
    <w:name w:val="Body Text"/>
    <w:basedOn w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="WarningBox">
    <w:name w:val="Warning Box"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr>
      <w:spacing w:after="180"/>
      <w:ind w:left="180" w:right="180"/>
      <w:shd w:val="clear" w:color="auto" w:fill="FFF2CC"/>
      <w:pBdr>
        <w:top w:val="single" w:sz="8" w:space="1" w:color="D6B656"/>
        <w:left w:val="single" w:sz="8" w:space="1" w:color="D6B656"/>
        <w:bottom w:val="single" w:sz="8" w:space="1" w:color="D6B656"/>
        <w:right w:val="single" w:sz="8" w:space="1" w:color="D6B656"/>
      </w:pBdr>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:color w:val="7D6608"/>
    </w:rPr>
  </w:style>
</w:styles>
""".strip()


def _numbering_xml() -> str:
    return _xml_header() + """
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="1">
    <w:multiLevelType w:val="hybridMultilevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="•"/>
      <w:lvlJc w:val="left"/>
      <w:pPr>
        <w:ind w:left="720" w:hanging="360"/>
      </w:pPr>
      <w:rPr>
        <w:rFonts w:ascii="Aptos" w:hAnsi="Aptos" w:hint="default"/>
      </w:rPr>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1">
    <w:abstractNumId w:val="1"/>
  </w:num>
</w:numbering>
""".strip()


def _run(text: str, *, bold: bool = False) -> str:
    escaped = escape(text)
    bold_xml = "<w:b/>" if bold else ""
    return f'<w:r><w:rPr>{bold_xml}</w:rPr><w:t xml:space="preserve">{escaped}</w:t></w:r>'


def _paragraph(text: str, style: str, *, page_break_before: bool = False) -> str:
    page_break_xml = "<w:pageBreakBefore/>" if page_break_before else ""
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/>{page_break_xml}</w:pPr>' f"{_run(text)}</w:p>"


def _bullet(text: str, level: int = 0) -> str:
    return (
        "<w:p>"
        "<w:pPr>"
        '<w:pStyle w:val="BodyText"/>'
        "<w:numPr>"
        f'<w:ilvl w:val="{level}"/>'
        '<w:numId w:val="1"/>'
        "</w:numPr>"
        "</w:pPr>"
        f"{_run(text)}"
        "</w:p>"
    )


def _table(rows: list[tuple[str, str]]) -> str:
    header = """
<w:tr>
  <w:tc>
    <w:tcPr><w:tcW w:w="2800" w:type="dxa"/><w:shd w:val="clear" w:fill="DCE6F1"/></w:tcPr>
    <w:p><w:pPr><w:pStyle w:val="Label"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>Статус</w:t></w:r></w:p>
  </w:tc>
  <w:tc>
    <w:tcPr><w:tcW w:w="6200" w:type="dxa"/><w:shd w:val="clear" w:fill="DCE6F1"/></w:tcPr>
    <w:p><w:pPr><w:pStyle w:val="Label"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>Что это значит</w:t></w:r></w:p>
  </w:tc>
</w:tr>
""".strip()
    body = []
    for left, right in rows:
        body.append(f"""
<w:tr>
  <w:tc>
    <w:tcPr><w:tcW w:w="2800" w:type="dxa"/></w:tcPr>
    <w:p><w:pPr><w:pStyle w:val="BodyText"/></w:pPr>{_run(left, bold=True)}</w:p>
  </w:tc>
  <w:tc>
    <w:tcPr><w:tcW w:w="6200" w:type="dxa"/></w:tcPr>
    <w:p><w:pPr><w:pStyle w:val="BodyText"/></w:pPr>{_run(right)}</w:p>
  </w:tc>
</w:tr>
""".strip())
    return (
        "<w:tbl>"
        "<w:tblPr>"
        '<w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="9000" w:type="dxa"/>'
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="8" w:space="0" w:color="B4C7E7"/>'
        '<w:left w:val="single" w:sz="8" w:space="0" w:color="B4C7E7"/>'
        '<w:bottom w:val="single" w:sz="8" w:space="0" w:color="B4C7E7"/>'
        '<w:right w:val="single" w:sz="8" w:space="0" w:color="B4C7E7"/>'
        '<w:insideH w:val="single" w:sz="6" w:space="0" w:color="D9E2F3"/>'
        '<w:insideV w:val="single" w:sz="6" w:space="0" w:color="D9E2F3"/>'
        "</w:tblBorders>"
        "</w:tblPr>"
        '<w:tblGrid><w:gridCol w:w="2800"/><w:gridCol w:w="6200"/></w:tblGrid>'
        f"{header}{''.join(body)}"
        "</w:tbl>"
    )


def _document_xml(title: str, subtitle: str, blocks: list[Block]) -> str:
    body: list[str] = [
        _paragraph(title, "TitleCustom"),
        _paragraph(subtitle, "SubtitleCustom"),
        _paragraph(
            "Рабочая инструкция для сотрудников подразделений, ОКК и координаторов. Используется как основной операционный порядок по контуру «Экспертиза» Wave 1.",
            "LeadBox",
        ),
    ]
    for block in blocks:
        if isinstance(block, Paragraph):
            body.append(_paragraph(block.text, block.style))
        elif isinstance(block, Bullet):
            body.append(_bullet(block.text, block.level))
        elif isinstance(block, Table):
            body.append(_table(block.rows))

    body.append("""
<w:sectPr>
  <w:pgSz w:w="11906" w:h="16838"/>
  <w:pgMar w:top="1100" w:right="900" w:bottom="1100" w:left="900" w:header="708" w:footer="708" w:gutter="0"/>
</w:sectPr>
""".strip())

    return (
        _xml_header()
        + """
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
    xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
    xmlns:o="urn:schemas-microsoft-com:office:office"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
    xmlns:v="urn:schemas-microsoft-com:vml"
    xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
    xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    xmlns:w10="urn:schemas-microsoft-com:office:word"
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
    xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
    xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
    xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
    mc:Ignorable="w14 wp14">
  <w:body>
"""
        + "".join(body)
        + """
  </w:body>
</w:document>
""".strip()
    )


def build_docx() -> Path:
    markdown = _read_source()
    title, subtitle, blocks = _parse_markdown(markdown)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _rels_xml())
        archive.writestr("word/_rels/document.xml.rels", _document_rels_xml())
        archive.writestr("word/document.xml", _document_xml(title, subtitle, blocks))
        archive.writestr("word/styles.xml", _styles_xml())
        archive.writestr("word/numbering.xml", _numbering_xml())
        archive.writestr("docProps/core.xml", _core_xml(title))
        archive.writestr("docProps/app.xml", _app_xml())
    return OUTPUT_PATH


def main() -> None:
    path = build_docx()
    print(path)


if __name__ == "__main__":
    main()
