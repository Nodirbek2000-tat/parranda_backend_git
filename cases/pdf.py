"""Case act (dalolatnoma) as PDF: household, every stage (who, when, data, notes, photo thumbnails),
history of events and signature lines.

Font: REPORT_FONT_PATH / REPORT_FONT_BOLD_PATH when set, else Bitstream Vera shipped with reportlab
(it has the Uzbek ‘ ’ glyphs). Never the built-in Helvetica. Every string passes `pdf_text`.
"""

import io
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from django.conf import settings
from django.utils import timezone
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import stages as chain

FONT, FONT_BOLD = "NNP", "NNP-Bold"
PRIMARY = colors.HexColor("#12372A")
GRID = colors.HexColor("#C9D1C5")
FALLBACKS = {"ʻ": "‘", "ʼ": "’", "`": "‘", "´": "’", "№": "No", " ": " ", "−": "-", "✓": "+"}


def _font_paths() -> tuple[str, str]:
    regular, bold = settings.REPORT_FONT_PATH.strip(), settings.REPORT_FONT_BOLD_PATH.strip()
    builtin = Path(reportlab.__file__).resolve().parent / "fonts"
    if not regular or not Path(regular).is_file():
        return str(builtin / "Vera.ttf"), str(builtin / "VeraBd.ttf")
    return regular, bold if bold and Path(bold).is_file() else regular


@lru_cache(maxsize=1)
def _cmap() -> frozenset:
    regular, bold = _font_paths()
    font = TTFont(FONT, regular)
    pdfmetrics.registerFont(font)
    pdfmetrics.registerFont(TTFont(FONT_BOLD, bold))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
    return frozenset(font.face.charToGlyph.keys())


def safe_text(value) -> str:
    """Text safe for the font: missing glyphs -> fallback (ʻ -> ‘, № -> No) or '?'."""
    cmap = _cmap()
    out = []
    for char in str(value or ""):
        if ord(char) in cmap or char in "\n\t":
            out.append(char)
        else:
            repl = FALLBACKS.get(char, "?")
            out.append(repl if all(ord(c) in cmap for c in repl) else "?")
    return "".join(out)


def pdf_text(value) -> str:
    """`safe_text` escaped for a Paragraph."""
    return escape(safe_text(value)).replace("\n", "<br/>")


def _dt(value) -> str:
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M") if value else "—"


def _thumb(field, size=38 * mm):
    """Downscaled JPEG of a photo (keeps the PDF small); None when the file is missing/broken."""
    try:
        with field.open("rb") as handle, PILImage.open(handle) as img:
            img = img.convert("RGB")
            img.thumbnail((360, 360))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
    except Exception:  # missing or broken file: the act is still produced
        return None
    buf.seek(0)
    ratio = img.height / img.width if img.width else 1
    return Image(buf, width=size, height=size * ratio)


def _data_lines(stage) -> str:
    d = chain.get(stage.number)
    labels = {f.name: f for f in d.fields}
    rows = []
    for key, value in (stage.data or {}).items():
        if key in ("package_id", "finance"):
            continue
        f = labels.get(key)
        if f and f.choices:
            value = dict(f.choices).get(value, value)
        if key in ("amount", "revenue", "price_per_kg") and isinstance(value, int):
            value = f"{value:,}".replace(",", " ") + " so‘m"
        rows.append(f"<b>{pdf_text(f.label if f else key)}:</b> {pdf_text(value)}")
    if stage.note and stage.note != (stage.data or {}).get("conclusion"):
        rows.append(f"<b>Izoh:</b> {pdf_text(stage.note)}")
    if stage.return_reason:
        rows.append(f"<b>Qaytarish sababi:</b> {pdf_text(stage.return_reason)}")
    return "<br/>".join(rows) or "—"


def case_pdf(case) -> bytes:
    _cmap()
    base = ParagraphStyle("base", fontName=FONT, fontSize=8.5, leading=11)
    h1 = ParagraphStyle("h1", parent=base, fontName=FONT_BOLD, fontSize=15, leading=19, textColor=PRIMARY,
                        alignment=1)
    h2 = ParagraphStyle("h2", parent=base, fontName=FONT_BOLD, fontSize=11, leading=15, textColor=PRIMARY,
                        spaceBefore=8, spaceAfter=4)

    def p(text, style=base):
        return Paragraph(text, style)

    story = [
        p("DALOLATNOMA", h1),
        p(f"Xonadon {pdf_text(case.code)} — {pdf_text(case.name)}", ParagraphStyle("c", parent=base, alignment=1)),
        Spacer(1, 6 * mm),
    ]
    info = [
        ("Egasi", case.owner_full_name), ("Telefon", case.owner_phone),
        ("Manzil", ", ".join(x for x in (case.region, case.district, case.mfy, case.address) if x)),
        ("GPS", f"{case.latitude}, {case.longitude}" if case.latitude is not None else "—"),
        ("Paket", case.package.name), ("Holati", case.get_status_display()),
        ("Yaratilgan", _dt(case.created_at)), ("Yakunlangan", _dt(case.done_at)),
    ]
    if case.reject_reason:
        info.append(("Rad etish sababi", case.reject_reason))
    table = Table([[p(f"<b>{pdf_text(k)}</b>"), p(pdf_text(v) or "—")] for k, v in info], colWidths=[40 * mm, 140 * mm])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [table, p("Bosqichlar", h2)]

    stages = list(case.stages.select_related("submitted_by", "decided_by"))
    photos = list(case.photos.all())
    rows = [[p(f"<b>{pdf_text('№')}</b>"), p("<b>Bosqich</b>"), p("<b>Holat</b>"), p("<b>Bajardi</b>"), p("<b>Tasdiqladi</b>"),
             p("<b>Ma’lumot</b>")]]
    for st in stages:
        rows.append([
            p(str(st.number)), p(pdf_text(chain.get(st.number).title)), p(pdf_text(st.get_status_display())),
            p(f"{pdf_text(st.submitted_by.full_name)}<br/>{_dt(st.submitted_at)}" if st.submitted_by else "—"),
            p(f"{pdf_text(st.decided_by.full_name)}<br/>{_dt(st.decided_at)}" if st.decided_by else "—"),
            p(_data_lines(st)),
        ])
    table = Table(rows, colWidths=[9 * mm, 29 * mm, 22 * mm, 32 * mm, 32 * mm, 58 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7EEE4")),
    ]))
    story.append(table)

    if photos:
        story.append(p("Rasmlar", h2))
        for st in stages:
            thumbs = [t for t in (_thumb(ph.image) for ph in photos if ph.stage == st.number)][:4]
            thumbs = [t for t in thumbs if t is not None]
            if thumbs:
                story.append(KeepTogether([
                    p(f"<b>{st.number}. {pdf_text(chain.get(st.number).title)}</b>"),
                    Table([thumbs], hAlign="LEFT"), Spacer(1, 3 * mm),
                ]))

    story.append(p("Hodisalar tarixi", h2))
    events = [[p("<b>Vaqt</b>"), p(f"<b>{pdf_text('№')}</b>"), p("<b>Amal</b>"), p("<b>Kim</b>"), p("<b>Izoh</b>")]]
    for e in case.events.select_related("actor"):
        events.append([p(_dt(e.created_at)), p(str(e.stage_number)), p(pdf_text(e.get_action_display())),
                       p(pdf_text(e.actor.full_name if e.actor else "—")), p(pdf_text(e.note) or "—")])
    table = Table(events, colWidths=[28 * mm, 10 * mm, 24 * mm, 40 * mm, 80 * mm], repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(table)

    story += [Spacer(1, 10 * mm), p("Imzolar", h2)]
    signers = ["Inspektor", "Direktor", "Administrator", "Xonadon egasi", "Buxgalter", "Super Admin"]
    sign_rows = [[p(pdf_text(name)), p("_______________________"), p("«____» __________ 20___ y.")] for name in signers]
    story.append(Table(sign_rows, colWidths=[40 * mm, 70 * mm, 70 * mm], rowHeights=11 * mm))

    buf = io.BytesIO()

    def footer(canvas, doc):
        canvas.setFont(FONT, 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 8 * mm, safe_text(f"NNP — {case.code} · Sahifa {doc.page} · {_dt(timezone.now())}"))

    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title=f"Dalolatnoma {case.code}").build(
        story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
