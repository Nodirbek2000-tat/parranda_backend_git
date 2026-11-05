"""Act PDF (API_CONTRACT_V4 §6): A4 portrait, built only from the frozen snapshot.

Reuses the report font registration and :func:`reports.pdf.pdf_text` (every string passes through it).
Layout: header (company, code, title, date / period), household block, body, summary, section tables
(repeated header, zebra rows), tasks with values, photo thumbnails (≤ 12), approvals, signatures and the footer
``DL-000001 · Sahifa X / Y``.
"""

from __future__ import annotations

import io
import logging
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    CondPageBreak,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from reports.pdf import FONT, FONT_BOLD, GRID, MUTED, PRIMARY, ZEBRA, _NumberedCanvas, pdf_text

logger = logging.getLogger("nnp.acts")

PAGE = A4
MARGIN = 15 * mm
WIDTH = PAGE[0] - 2 * MARGIN
MAX_PHOTOS = 12
PHOTO_COLUMNS = 4
#: General act: rows printed per role (the snapshot keeps them all).
GENERAL_ROWS_PER_ROLE = 50
LIGHT = colors.HexColor("#E8EEE9")


def _p(value: Any, style: ParagraphStyle) -> Paragraph:
    """Paragraph of escaped, font-safe text (newlines become line breaks)."""
    return Paragraph(escape(pdf_text(str(value or ""))).replace("\n", "<br/>"), style)


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "company": ParagraphStyle("company", fontName=FONT_BOLD, fontSize=10, leading=13, textColor=MUTED),
        "code": ParagraphStyle("code", fontName=FONT_BOLD, fontSize=10, leading=13, textColor=PRIMARY, alignment=2),
        "title": ParagraphStyle("title", fontName=FONT_BOLD, fontSize=15, leading=19, textColor=PRIMARY,
                                alignment=1, spaceBefore=2, spaceAfter=2),
        "meta": ParagraphStyle("meta", fontName=FONT, fontSize=8.5, leading=11.5, textColor=MUTED, alignment=1),
        "h2": ParagraphStyle("h2", fontName=FONT_BOLD, fontSize=10.5, leading=14, textColor=PRIMARY,
                             spaceBefore=6, spaceAfter=3),
        "body": ParagraphStyle("body", fontName=FONT, fontSize=9.5, leading=13.5),
        "cell": ParagraphStyle("cell", fontName=FONT, fontSize=7.8, leading=9.6),
        "head": ParagraphStyle("head", fontName=FONT_BOLD, fontSize=7.8, leading=9.6, textColor=colors.white),
        "label": ParagraphStyle("label", fontName=FONT_BOLD, fontSize=8.5, leading=11),
        "value": ParagraphStyle("value", fontName=FONT, fontSize=8.5, leading=11),
        "small": ParagraphStyle("small", fontName=FONT, fontSize=7, leading=9, textColor=MUTED, alignment=1),
        "note": ParagraphStyle("note", fontName=FONT, fontSize=7.5, leading=10, textColor=MUTED),
    }


def _canvas(code: str) -> type:
    class ActCanvas(_NumberedCanvas):
        def _footer(self, total: int) -> None:
            self.saveState()
            self.setFont(FONT, 7.5)
            self.setFillColor(MUTED)
            self.setStrokeColor(GRID)
            self.line(MARGIN, MARGIN - 2 * mm, PAGE[0] - MARGIN, MARGIN - 2 * mm)
            self.drawString(MARGIN, MARGIN - 6 * mm, pdf_text(code))
            self.drawRightString(PAGE[0] - MARGIN, MARGIN - 6 * mm, pdf_text(f"Sahifa {self._pageNumber} / {total}"))
            self.restoreState()

    return ActCanvas


def _key_value_table(rows: list[tuple[str, str]], s: dict, label_width: float = 55 * mm) -> Table:
    table = Table(
        [[_p(label, s["label"]), _p(value, s["value"])] for label, value in rows],
        colWidths=[label_width, WIDTH - label_width],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _column_widths(columns: list[str], rows: list[list[str]]) -> list[float]:
    natural = []
    for index, column in enumerate(columns):
        longest = max((len(str(row[index])) for row in rows[:200] if index < len(row)), default=0)
        header = max((len(word) for word in column.split()), default=4)
        natural.append(min(max(longest, header, 4), 45) * 4.4 + 8)
    total = sum(natural)
    return [width * WIDTH / total for width in natural]


def _data_table(columns: list[str], rows: list[list[str]], s: dict) -> Table:
    body = [[_p(cell, s["cell"]) for cell in row] + [""] * (len(columns) - len(row)) for row in rows]
    if not body:
        body = [[_p("Ma’lumot yo‘q", s["cell"])] + [""] * (len(columns) - 1)]
    table = Table([[_p(c, s["head"]) for c in columns], *body], colWidths=_column_widths(columns, rows), repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for row_index in range(2, len(body) + 1, 2):
        style.append(("BACKGROUND", (0, row_index), (-1, row_index), ZEBRA))
    if not rows:
        style.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(style))
    return table


def _header(snapshot: dict, s: dict) -> list:
    top = Table(
        [[_p(snapshot.get("company"), s["company"]), _p(snapshot.get("code") or "", s["code"])]],
        colWidths=[WIDTH * 0.7, WIDTH * 0.3],
    )
    top.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, PRIMARY),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    meta = f"Sana: {snapshot.get('date', '')}"
    if snapshot.get("period"):
        meta += f"   ·   Davr: {snapshot['period']}"
    if snapshot.get("role_display"):
        meta += f"   ·   Rol: {snapshot['role_display']}"
    story: list = [top, Spacer(1, 4 * mm), _p(snapshot.get("title"), s["title"]), _p(meta, s["meta"]),
                   Spacer(1, 4 * mm)]
    rows = [("Mas’ul", snapshot.get("responsible") or "—")]
    household = snapshot.get("household")
    if household:
        rows = [
            ("Xonadon", f"{household.get('code', '')} {household.get('name', '')}"),
            ("Xonadon egasi", household.get("owner") or "—"),
            ("Tuman", household.get("district") or "—"),
            ("Manzil", household.get("address") or "—"),
        ] + rows
    info = _key_value_table(rows, s, 35 * mm)
    info.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.white)]))
    story += [info, Spacer(1, 4 * mm)]
    return story


def _sections(snapshot: dict, s: dict) -> list:
    story: list = []
    general = snapshot.get("kind") == "general"
    budget: dict[str, int] = {}
    for section in snapshot.get("sections") or []:
        title = str(section.get("title") or "")
        columns = [str(c) for c in section.get("columns") or []] or [""]
        rows = [[str(cell) for cell in row] for row in section.get("rows") or []]
        note = None
        if general and len(columns) != 2:  # role tables (not the "yakuniy ko‘rsatkichlar" blocks)
            role = title.split(":", 1)[0]
            left = max(GENERAL_ROWS_PER_ROLE - budget.get(role, 0), 0)
            if len(rows) > left:
                note = f"… yana {len(rows) - left} ta qator (to‘liq ro‘yxat dalolatnoma ma’lumotlarida saqlangan)"
                rows = rows[:left]
            budget[role] = budget.get(role, 0) + len(rows)
        story += [CondPageBreak(30 * mm), _p(title, s["h2"]), _data_table(columns, rows, s)]
        if note:
            story.append(_p(note, s["note"]))
        story.append(Spacer(1, 2 * mm))
    return story


def _tasks(snapshot: dict, s: dict) -> list:
    tasks = snapshot.get("tasks") or []
    if not tasks or snapshot.get("kind") == "task":
        return []
    story: list = [CondPageBreak(30 * mm), _p("Bajarilgan vazifalar", s["h2"])]
    for task in tasks:
        head = f"{task.get('code', '')} — {task.get('title', '')}"
        meta = f"Xonadon: {task.get('household') or '—'}   ·   Tasdiqlangan: {task.get('approved_at') or '—'}"
        rows = [(item.get("label", ""), item.get("value", "")) for item in task.get("values") or []]
        block = [_p(head, s["label"]), _p(meta, s["note"])]
        if rows:
            block.append(_key_value_table(rows, s, 60 * mm))
        block.append(Spacer(1, 3 * mm))
        story.append(KeepTogether(block))  # splits by itself when a task is longer than a page
    return story


def _load_image(file_id: int) -> tuple[io.BytesIO, tuple[int, int]] | None:
    """Image bytes and size of an attachment; ``None`` when missing, unreadable or not an image."""
    from core.models import Attachment

    attachment = Attachment.objects.filter(pk=file_id).first()
    if attachment is None or not str(attachment.content_type).startswith("image/"):
        return None
    try:
        with attachment.file.open("rb") as handle:
            data = io.BytesIO(handle.read())
        size = ImageReader(data).getSize()
        data.seek(0)
        return data, size
    except Exception:  # noqa: BLE001 - a broken / missing file must never break the act
        logger.warning("Act photo %s could not be read", file_id)
        return None


def _photos(snapshot: dict, s: dict) -> list:
    cell_width = WIDTH / PHOTO_COLUMNS
    box = cell_width - 4 * mm
    cells: list = []
    for photo in (snapshot.get("photos") or [])[:MAX_PHOTOS]:
        loaded = _load_image(photo.get("file_id"))
        if loaded is None:
            continue
        data, (width, height) = loaded
        scale = min(box / width, box / height)
        cells.append([Image(data, width=width * scale, height=height * scale), _p(photo.get("label"), s["small"])])
    if not cells:
        return []
    rows = [cells[i:i + PHOTO_COLUMNS] for i in range(0, len(cells), PHOTO_COLUMNS)]
    rows[-1] += [""] * (PHOTO_COLUMNS - len(rows[-1]))
    grid = Table(rows, colWidths=[cell_width] * PHOTO_COLUMNS, hAlign="LEFT")
    grid.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return [CondPageBreak(50 * mm), _p("Rasmlar", s["h2"]), grid]


def _signatures(snapshot: dict, s: dict) -> list:
    lines = snapshot.get("signatures") or []
    if not lines:
        return []
    rows = [[_p(line, s["body"])] for line in lines]
    table = Table(rows, colWidths=[WIDTH], hAlign="LEFT")
    table.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return [Spacer(1, 6 * mm), KeepTogether([_p("Imzolar", s["h2"]), table])]


def build(snapshot: dict) -> bytes:
    """PDF bytes of a frozen act snapshot."""
    s = _styles()
    buffer = io.BytesIO()
    code = str(snapshot.get("code") or "")
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 2 * mm,
        title=pdf_text(f"{code} {snapshot.get('title', '')}".strip()),
        author=pdf_text(str(snapshot.get("company") or "")),
        creator="NNP",
    )
    story = _header(snapshot, s)
    if snapshot.get("body"):
        story += [_p(snapshot["body"], s["body"]), Spacer(1, 4 * mm)]
    summary = [(item.get("label", ""), item.get("value", "")) for item in snapshot.get("summary") or []]
    general = snapshot.get("kind") == "general"
    if summary and not general:
        story += [_p("Yakuniy ko‘rsatkichlar", s["h2"]), _key_value_table(summary, s), Spacer(1, 3 * mm)]
    story += _sections(snapshot, s)
    story += _tasks(snapshot, s)
    if summary and general:
        story += [CondPageBreak(40 * mm), _p("Yakuniy natijalar", s["h2"]), _key_value_table(summary, s)]
    story += _photos(snapshot, s)
    approvals = snapshot.get("approvals") or []
    if approvals:
        story += [
            CondPageBreak(30 * mm),
            _p("Tasdiqlashlar", s["h2"]),
            _key_value_table([(a.get("label", ""), f"{a.get('name', '')} · {a.get('at', '')}") for a in approvals], s),
        ]
    story += _signatures(snapshot, s)
    doc.build(story, canvasmaker=_canvas(code or "Dalolatnoma"))
    return buffer.getvalue()
