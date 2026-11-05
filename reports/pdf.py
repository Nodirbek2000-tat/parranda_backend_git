"""PDF export (API_CONTRACT_V3 §11.4) with reportlab: A4 landscape, 12 mm margins, repeated header row,
zebra rows, right-aligned numbers and the footer ``NNP — … · Sahifa 2 / 5 · 04.11.2026 18:20``.

Font: a TrueType font registered at import — ``REPORT_FONT_PATH`` / ``REPORT_FONT_BOLD_PATH`` (env) when set,
otherwise Bitstream Vera shipped inside the reportlab package. Never the built-in Helvetica (no ``‘`` glyph
guarantee). Every string passes :func:`pdf_text`, which maps characters missing from the font's cmap.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .core import NUMERIC, TEXT, Column, ReportData, display_value

logger = logging.getLogger("nnp.reports")

FONT = "NNPReport"
FONT_BOLD = "NNPReport-Bold"
PRIMARY = colors.HexColor("#12372A")
ZEBRA = colors.HexColor("#F4F6F2")
GRID = colors.HexColor("#D9DED6")
MUTED = colors.HexColor("#5B6B63")
FOOTER_TEXT = "NNP — Navobod naslli parranda qisqarmasi"
MARGIN = 12 * mm
PAGE = landscape(A4)

#: Replacements tried before ``?`` for characters the font has no glyph for.
FALLBACKS = {
    "ʻ": "‘",
    "ʼ": "’",
    "`": "‘",
    "´": "’",
    "№": "No",
    " ": " ",
    " ": " ",
    "−": "-",
}


def _font_paths() -> tuple[str, str]:
    regular = os.environ.get("REPORT_FONT_PATH", "").strip()
    bold = os.environ.get("REPORT_FONT_BOLD_PATH", "").strip()
    builtin = Path(reportlab.__file__).resolve().parent / "fonts"
    if not regular or not Path(regular).is_file():
        regular = str(builtin / "Vera.ttf")
        bold = str(builtin / "VeraBd.ttf")
    elif not bold or not Path(bold).is_file():
        bold = regular
    return regular, bold


def _register() -> set[int]:
    regular, bold = _font_paths()
    regular_font = TTFont(FONT, regular)
    pdfmetrics.registerFont(regular_font)
    pdfmetrics.registerFont(TTFont(FONT_BOLD, bold))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
    return set(regular_font.face.charToGlyph.keys())


CMAP: set[int] = _register()


@lru_cache(maxsize=4096)
def _map_char(char: str) -> str:
    if ord(char) in CMAP or char in "\n\t":
        return char
    replacement = FALLBACKS.get(char)
    if replacement is not None and all(ord(item) in CMAP for item in replacement):
        return replacement
    return "?"


def pdf_text(value: str) -> str:
    """Text safe for the registered font: missing glyphs → fallback (``ʻ`` → ``‘``, ``№`` → ``No``) or ``?``."""
    if not value:
        return ""
    if all(ord(char) in CMAP for char in value):
        return value
    return "".join(_map_char(char) for char in value)


class _NumberedCanvas(pdf_canvas.Canvas):
    """Two-pass page numbering: ``Sahifa N / total``."""

    footer_time = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []

    def showPage(self):  # noqa: N802 - reportlab API
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total: int) -> None:
        self.saveState()
        self.setFont(FONT, 7.5)
        self.setFillColor(MUTED)
        text = pdf_text(f"{FOOTER_TEXT} · Sahifa {self._pageNumber} / {total} · {self.footer_time}")
        self.drawCentredString(PAGE[0] / 2, MARGIN / 2, text)
        self.restoreState()


def _column_widths(columns: list[Column], rows: list[list[str]], available: float, font_size: float) -> list[float]:
    """Widths from the content (sampled), scaled to the page width; text columns absorb the slack."""
    sample = rows[:300]
    natural = []
    for index, column in enumerate(columns):
        longest = max((len(row[index]) for row in sample), default=0)
        header = max(len(word) for word in column.label.split()) if column.label else 4
        chars = max(longest, header, 4)
        if column.type == TEXT:
            chars = min(chars, 40)
        natural.append(chars * font_size * 0.56 + 8)
    total = sum(natural)
    if total <= available:
        extra = available - total
        text_columns = [i for i, column in enumerate(columns) if column.type == TEXT] or list(range(len(columns)))
        for i in text_columns:
            natural[i] += extra / len(text_columns)
        return natural
    scale = available / total
    return [width * scale for width in natural]


def build(
    data: ReportData,
    *,
    company: str,
    title: str,
    period_line: str,
    filters_line: str,
    generated_at: datetime,
) -> bytes:
    buffer = io.BytesIO()
    footer_time = timezone.localtime(generated_at).strftime("%d.%m.%Y %H:%M")
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 4 * mm,
        title=pdf_text(title),
        author=pdf_text(company),
        creator="NNP",
    )
    columns = data.columns
    font_size = 7.5 if len(columns) <= 10 else 6.8
    cell_style = ParagraphStyle("cell", fontName=FONT, fontSize=font_size, leading=font_size + 1.6, alignment=TA_LEFT)
    header_style = ParagraphStyle(
        "head", fontName=FONT_BOLD, fontSize=font_size, leading=font_size + 1.6, textColor=colors.white
    )
    title_style = ParagraphStyle("title", fontName=FONT_BOLD, fontSize=15, leading=19, textColor=PRIMARY)
    subtitle_style = ParagraphStyle("subtitle", fontName=FONT_BOLD, fontSize=11.5, leading=15)
    meta_style = ParagraphStyle("meta", fontName=FONT, fontSize=8.5, leading=11.5, textColor=MUTED)

    text_rows = [[pdf_text(display_value(column, row.get(column.key))) for column in columns] for row in data.rows]
    totals_row = None
    if data.totals:
        totals_row = [
            pdf_text(display_value(column, data.totals[column.key])) if column.key in data.totals else ""
            for column in columns
        ]
        if not totals_row[0]:
            totals_row[0] = "Jami"
    widths = _column_widths(columns, text_rows + ([totals_row] if totals_row else []), PAGE[0] - 2 * MARGIN, font_size)

    def cell(text: str, column: Column) -> object:
        if column.type == TEXT and len(text) > 12:
            return Paragraph(escape(text), cell_style)
        return text

    table_rows: list[list[object]] = [[Paragraph(escape(pdf_text(column.label)), header_style) for column in columns]]
    for row in text_rows:
        table_rows.append([cell(text, columns[i]) for i, text in enumerate(row)])
    if totals_row is not None:
        table_rows.append(totals_row)
    if not text_rows:
        table_rows.append(["Ma’lumot yo‘q"] + [""] * (len(columns) - 1))

    style = [
        ("FONT", (0, 0), (-1, -1), FONT, font_size),
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for index, column in enumerate(columns):
        if column.type in NUMERIC:
            style.append(("ALIGN", (index, 1), (index, -1), "RIGHT"))
    for row_index in range(2, len(text_rows) + 1, 2):
        style.append(("BACKGROUND", (0, row_index), (-1, row_index), ZEBRA))
    if totals_row is not None:
        last = len(table_rows) - 1
        style.append(("FONT", (0, last), (-1, last), FONT_BOLD, font_size))
        style.append(("LINEABOVE", (0, last), (-1, last), 0.8, PRIMARY))
    table = Table(table_rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(style))

    story: list[object] = [
        Paragraph(escape(pdf_text(company)), title_style),
        Paragraph(escape(pdf_text(title)), subtitle_style),
        Paragraph(escape(pdf_text(period_line)), meta_style),
        Paragraph(escape(pdf_text(filters_line)), meta_style),
        Spacer(1, 4 * mm),
        table,
    ]
    if data.summary:
        story.append(Spacer(1, 5 * mm))
        summary_rows = [[pdf_text(label), pdf_text(value)] for label, value in data.summary]
        summary_table = Table(summary_rows, colWidths=[70 * mm, 120 * mm], hAlign="LEFT")
        summary_table.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), FONT, 8.5),
                    ("FONT", (0, 0), (0, -1), FONT_BOLD, 8.5),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(summary_table)

    canvas_class = type("ReportCanvas", (_NumberedCanvas,), {"footer_time": footer_time})
    doc.build(story, canvasmaker=canvas_class)
    return buffer.getvalue()
