from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "docs" / "Instruction.ExpertiseWave1.Team.md"
OUTPUT_PATH = ROOT / "build" / "docs" / "Instruction.ExpertiseWave1.Team.pdf"

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

PAGE_W = 1240
PAGE_H = 1754
MARGIN_X = 88
MARGIN_Y = 82
CONTENT_W = PAGE_W - MARGIN_X * 2

COLOR_TEXT = "#203040"
COLOR_MUTED = "#677487"
COLOR_BLUE = "#0F4C81"
COLOR_TEAL = "#1B5E75"
COLOR_BORDER = "#D9E2EC"
COLOR_FILL_BLUE = "#EEF5FF"
COLOR_FILL_YELLOW = "#FFF6D8"
COLOR_FILL_TABLE = "#E7F0FA"
COLOR_BG = "#FFFFFF"


@dataclass(frozen=True)
class Paragraph:
    text: str
    kind: str


@dataclass(frozen=True)
class Bullet:
    text: str


@dataclass(frozen=True)
class Table:
    rows: list[tuple[str, str]]


Block = Paragraph | Bullet | Table


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


F_TITLE = _font(FONT_BOLD, 34)
F_SUBTITLE = _font(FONT_REGULAR, 18)
F_H1 = _font(FONT_BOLD, 24)
F_H2 = _font(FONT_BOLD, 20)
F_BODY = _font(FONT_REGULAR, 17)
F_BODY_BOLD = _font(FONT_BOLD, 17)
F_LABEL = _font(FONT_BOLD, 17)
F_SMALL = _font(FONT_REGULAR, 15)


def _strip_inline_markup(value: str) -> str:
    return value.replace("`", "").replace("**", "").strip()


def _parse_markdown(markdown: str) -> tuple[str, str, list[Block]]:
    title = ""
    subtitle = ""
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
            blocks.append(Paragraph(current_section, "h1"))
            continue
        if stripped.startswith("### "):
            flush_status_rows()
            blocks.append(Paragraph(_strip_inline_markup(stripped[4:]), "h2"))
            continue
        if stripped.endswith(":") and not stripped.startswith("- "):
            flush_status_rows()
            blocks.append(Paragraph(_strip_inline_markup(stripped), "label"))
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
        kind = "body"
        if current_section == "1. Что изменилось" and stripped.startswith("Старый чат "):
            kind = "warning"
        blocks.append(Paragraph(_strip_inline_markup(stripped), kind))

    flush_status_rows()
    return title, subtitle, blocks


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, extra: int = 6) -> int:
    box = draw.textbbox((0, 0), "Абв 123", font=font)
    return (box[3] - box[1]) + extra


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_width(draw, candidate, font) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


class PdfCanvas:
    def __init__(self) -> None:
        self.pages: list[Image.Image] = []
        self._new_page()

    def _new_page(self) -> None:
        page = Image.new("RGB", (PAGE_W, PAGE_H), COLOR_BG)
        self.pages.append(page)
        self.image = page
        self.draw = ImageDraw.Draw(self.image)
        self.y = MARGIN_Y
        self._draw_page_header()

    def _draw_page_header(self) -> None:
        self.draw.rounded_rectangle(
            (MARGIN_X, 36, PAGE_W - MARGIN_X, 54),
            radius=9,
            fill=COLOR_BLUE,
        )
        self.draw.text((MARGIN_X, 64), "Экспертиза Wave 1", font=F_SMALL, fill=COLOR_MUTED)
        self.draw.text(
            (PAGE_W - MARGIN_X - 250, 64), "Инструкция для команды", font=F_SMALL, fill=COLOR_MUTED
        )
        self.y = max(self.y, 110)

    def ensure_space(self, needed: int) -> None:
        if self.y + needed <= PAGE_H - MARGIN_Y:
            return
        self._new_page()

    def add_title(self, title: str, subtitle: str) -> None:
        self.ensure_space(180)
        self.draw.text((MARGIN_X, self.y), title, font=F_TITLE, fill=COLOR_BLUE)
        self.y += _line_height(self.draw, F_TITLE, 14)
        self.draw.text((MARGIN_X, self.y), subtitle, font=F_SUBTITLE, fill=COLOR_MUTED)
        self.y += 44
        self.draw.rounded_rectangle(
            (MARGIN_X, self.y, PAGE_W - MARGIN_X, self.y + 98),
            radius=22,
            fill=COLOR_FILL_BLUE,
            outline=COLOR_BORDER,
            width=2,
        )
        lead = (
            "Рабочая инструкция для сотрудников подразделений, ОКК и координаторов. "
            "Используется как основной операционный порядок по контуру «Экспертиза»."
        )
        self._draw_wrapped_text(
            lead, MARGIN_X + 24, self.y + 20, CONTENT_W - 48, F_BODY_BOLD, COLOR_TEXT
        )
        self.y += 126

    def _draw_wrapped_text(
        self,
        text: str,
        x: int,
        y: int,
        width: int,
        font: ImageFont.FreeTypeFont,
        fill: str,
    ) -> int:
        lines = _wrap_text(self.draw, text, font, width)
        lh = _line_height(self.draw, font)
        current_y = y
        for line in lines:
            self.draw.text((x, current_y), line, font=font, fill=fill)
            current_y += lh
        return current_y - y

    def add_paragraph(self, text: str, kind: str) -> None:
        if kind == "h1":
            self.ensure_space(70)
            self.draw.text((MARGIN_X, self.y), text, font=F_H1, fill=COLOR_BLUE)
            self.y += _line_height(self.draw, F_H1, 10)
            self.draw.rounded_rectangle(
                (MARGIN_X, self.y, MARGIN_X + 120, self.y + 6),
                radius=3,
                fill=COLOR_BLUE,
            )
            self.y += 24
            return
        if kind == "h2":
            self.ensure_space(52)
            self.draw.text((MARGIN_X, self.y), text, font=F_H2, fill=COLOR_TEAL)
            self.y += _line_height(self.draw, F_H2, 8)
            return
        if kind == "label":
            self.ensure_space(36)
            self.draw.text((MARGIN_X, self.y), text, font=F_LABEL, fill=COLOR_BLUE)
            self.y += _line_height(self.draw, F_LABEL, 4)
            return
        if kind == "warning":
            box_lines = _wrap_text(self.draw, text, F_BODY_BOLD, CONTENT_W - 48)
            needed = len(box_lines) * _line_height(self.draw, F_BODY_BOLD) + 34
            self.ensure_space(needed)
            self.draw.rounded_rectangle(
                (MARGIN_X, self.y, PAGE_W - MARGIN_X, self.y + needed),
                radius=18,
                fill=COLOR_FILL_YELLOW,
                outline="#E0C56E",
                width=2,
            )
            current_y = self.y + 16
            for line in box_lines:
                self.draw.text((MARGIN_X + 24, current_y), line, font=F_BODY_BOLD, fill="#7C5A00")
                current_y += _line_height(self.draw, F_BODY_BOLD)
            self.y += needed + 10
            return

        lines = _wrap_text(self.draw, text, F_BODY, CONTENT_W)
        needed = len(lines) * _line_height(self.draw, F_BODY) + 4
        self.ensure_space(needed)
        for line in lines:
            self.draw.text((MARGIN_X, self.y), line, font=F_BODY, fill=COLOR_TEXT)
            self.y += _line_height(self.draw, F_BODY)
        self.y += 4

    def add_bullet(self, text: str) -> None:
        lines = _wrap_text(self.draw, text, F_BODY, CONTENT_W - 38)
        needed = len(lines) * _line_height(self.draw, F_BODY) + 4
        self.ensure_space(needed)
        bullet_y = self.y + 7
        self.draw.ellipse((MARGIN_X + 4, bullet_y, MARGIN_X + 14, bullet_y + 10), fill=COLOR_BLUE)
        x = MARGIN_X + 28
        for line in lines:
            self.draw.text((x, self.y), line, font=F_BODY, fill=COLOR_TEXT)
            self.y += _line_height(self.draw, F_BODY)
        self.y += 2

    def add_table(self, rows: list[tuple[str, str]]) -> None:
        row_padding_y = 12
        left_w = 275
        right_w = CONTENT_W - left_w
        header_h = 44
        sample_lh = _line_height(self.draw, F_BODY, 4)
        table_height = header_h
        wrapped_rows: list[tuple[list[str], list[str], int]] = []
        for left, right in rows:
            left_lines = _wrap_text(self.draw, left, F_BODY_BOLD, left_w - 24)
            right_lines = _wrap_text(self.draw, right, F_BODY, right_w - 24)
            height = max(len(left_lines), len(right_lines)) * sample_lh + row_padding_y * 2
            wrapped_rows.append((left_lines, right_lines, height))
            table_height += height

        self.ensure_space(table_height + 12)
        x0 = MARGIN_X
        x1 = MARGIN_X + left_w
        x2 = MARGIN_X + CONTENT_W
        y = self.y
        self.draw.rounded_rectangle(
            (x0, y, x2, y + table_height),
            radius=18,
            outline="#B4C7E7",
            width=2,
            fill="#FFFFFF",
        )
        self.draw.rectangle(
            (x0, y, x2, y + header_h), fill=COLOR_FILL_TABLE, outline="#B4C7E7", width=2
        )
        self.draw.line((x1, y, x1, y + table_height), fill="#B4C7E7", width=2)
        self.draw.text((x0 + 16, y + 12), "Статус", font=F_BODY_BOLD, fill=COLOR_BLUE)
        self.draw.text((x1 + 16, y + 12), "Что это значит", font=F_BODY_BOLD, fill=COLOR_BLUE)
        current_y = y + header_h
        for left_lines, right_lines, height in wrapped_rows:
            self.draw.line((x0, current_y, x2, current_y), fill="#DCE4EE", width=1)
            ty = current_y + row_padding_y
            for line in left_lines:
                self.draw.text((x0 + 16, ty), line, font=F_BODY_BOLD, fill=COLOR_TEXT)
                ty += sample_lh
            ty = current_y + row_padding_y
            for line in right_lines:
                self.draw.text((x1 + 16, ty), line, font=F_BODY, fill=COLOR_TEXT)
                ty += sample_lh
            current_y += height
        self.y += table_height + 18

    def save_pdf(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        rgb_pages = [page.convert("RGB") for page in self.pages]
        first, *rest = rgb_pages
        first.save(path, "PDF", resolution=150.0, save_all=True, append_images=rest)
        return path


def build_pdf() -> Path:
    title, subtitle, blocks = _parse_markdown(SOURCE_PATH.read_text(encoding="utf-8"))
    canvas = PdfCanvas()
    canvas.add_title(title, subtitle)
    for block in blocks:
        if isinstance(block, Paragraph):
            canvas.add_paragraph(block.text, block.kind)
        elif isinstance(block, Bullet):
            canvas.add_bullet(block.text)
        elif isinstance(block, Table):
            canvas.add_table(block.rows)
    return canvas.save_pdf(OUTPUT_PATH)


def main() -> None:
    print(build_pdf())


if __name__ == "__main__":
    main()
