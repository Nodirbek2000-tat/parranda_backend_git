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

from accounts.models import Role

from . import stages as chain

FONT, FONT_BOLD = "NNP", "NNP-Bold"
PRIMARY = colors.HexColor("#0F3A6B")
GRID = colors.HexColor("#C2CDDA")
HEAD_BG = colors.HexColor("#E6F0FA")
RED = colors.HexColor("#E3262B")
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


def _name(user) -> str:
    return user.full_name if user else "—"


def stage_act_pdf(case, stage) -> bytes:
    """Act (dalolatnoma) of one stage approved by an Administrator: what was done, by whom, and the joint
    confirmation of the executor and the Administrator."""
    _cmap()
    d = chain.get(stage.number)
    base = ParagraphStyle("base", fontName=FONT, fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=10, textColor=colors.HexColor("#5A697B"),
                           alignment=1)
    center = ParagraphStyle("center", parent=base, alignment=1)
    right = ParagraphStyle("right", parent=base, alignment=2)
    h1 = ParagraphStyle("h1", parent=base, fontName=FONT_BOLD, fontSize=16, leading=20, textColor=PRIMARY,
                        alignment=1)
    h2 = ParagraphStyle("h2", parent=base, fontName=FONT_BOLD, fontSize=11, leading=15, textColor=PRIMARY,
                        spaceBefore=8, spaceAfter=4)

    def p(text, style=base):
        return Paragraph(text, style)

    def boxed(rows):
        table = Table(rows, colWidths=[40 * mm, 140 * mm])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("BACKGROUND", (0, 0), (0, -1), HEAD_BG)]))
        return table

    executor, admin = stage.submitted_by, stage.decided_by
    number = f"{case.code}-{stage.number:02d}"
    place = ", ".join(x for x in (case.region, case.district) if x) or "—"
    closed_at = stage.decided_at or stage.submitted_at
    approved_on = timezone.localtime(closed_at).strftime("%d.%m.%Y") if closed_at else "—"
    admin_role = "Administrator (Super Admin)" if admin and admin.role == Role.SUPERADMIN else "Administrator"
    executor_role = Role(d.executor).label
    # stages 1-3 (and growing) have no Administrator approval in the chain: his line stays for a hand signature
    admin_name = _name(admin) if admin else "____________________"

    story = [
        p(f"DALOLATNOMA {pdf_text('№')} {pdf_text(number)}", h1),
        p(f"{stage.number}-bosqich: <b>{pdf_text(d.title)}</b>", center),
        Spacer(1, 4 * mm),
        Table([[p(pdf_text(place)), p(f"{approved_on} y.", right)]], colWidths=[90 * mm, 90 * mm]),
        Spacer(1, 3 * mm),
        p(f"Biz, quyida imzo qo‘yuvchilar — bajaruvchi ({pdf_text(executor_role)}) <b>{pdf_text(_name(executor))}</b> "
          f"va {pdf_text(admin_role)} <b>{pdf_text(admin_name)}</b> — ushbu dalolatnomani {pdf_text(case.code)} "
          f"raqamli xonadon bo‘yicha «{pdf_text(d.title)}» bosqichi ishlari bajarilgani to‘g‘risida tuzdik."),
        p("Xonadon", h2),
        boxed([[p(f"<b>{pdf_text(k)}</b>"), p(pdf_text(v) or "—")] for k, v in (
            ("Xonadon", f"{case.code} — {case.name}"), ("Egasi", case.owner_full_name),
            ("Telefon", case.owner_phone),
            ("Manzil", ", ".join(x for x in (case.region, case.district, case.mfy, case.address) if x)),
            ("Paket", case.package.name),
        )]),
        p("Bajarilgan ishlar", h2),
    ]
    rows = [[p(f"<b>{pdf_text(k)}</b>"), p(pdf_text(v))] for k, v in (
        ("Bosqich", f"{stage.number}. {d.title}"),
        ("Bajaruvchi", f"{_name(executor)} ({executor_role})"),
        ("Bajarilgan vaqt", _dt(stage.submitted_at)),
        ("Muddat", _dt(stage.due_at)),
    )]
    rows.append([p("<b>Ma’lumotlar</b>"), p(_data_lines(stage))])
    returns = list(case.events.filter(stage_number=stage.number, action="return").select_related("actor"))
    if returns:
        rows.append([p("<b>Qaytarilgan</b>"), p("<br/>".join(
            f"{_dt(e.created_at)} — {pdf_text(_name(e.actor))}: {pdf_text(e.note) or '—'}" for e in returns))])
    story.append(boxed(rows))

    thumbs = [_thumb(ph.image, 30 * mm) for ph in case.photos.filter(stage=stage.number)[:5]]
    thumbs = [t for t in thumbs if t is not None]
    if thumbs:
        story += [p("Rasmlar", h2), Table([thumbs], hAlign="LEFT")]

    story += [
        Spacer(1, 5 * mm),
        p(f"Yuqoridagi ishlar to‘liq bajarilganligi <b>bajaruvchi</b> va <b>{pdf_text(admin_role)}</b> tomonidan "
          f"birgalikda tasdiqlandi." if admin else
          f"Yuqoridagi ishlar to‘liq bajarilganligi <b>bajaruvchi</b> tomonidan tasdiqlandi va "
          f"<b>{pdf_text(admin_role)}</b> imzosi bilan tasdiqlanadi."),
        Spacer(1, 4 * mm),
    ]
    stamp = Table([
        [p("<b>BIRGALIKDA TASDIQLANGAN</b>" if admin else "<b>BAJARUVCHI TOMONIDAN TASDIQLANGAN</b>",
           ParagraphStyle("st", parent=center, textColor=PRIMARY, fontSize=11))],
        [p(f"Bajaruvchi: <b>{pdf_text(_name(executor))}</b> · {_dt(stage.submitted_at)}", center)],
        [p(f"{pdf_text(admin_role)}: <b>{pdf_text(admin_name)}</b>" + (f" · {_dt(stage.decided_at)}" if admin else ""),
           center)],
        [p("NNP tizimida elektron tasdiqlangan", small)],
    ], colWidths=[125 * mm], hAlign="CENTER")
    stamp.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.4, PRIMARY), ("LINEBELOW", (0, 0), (-1, 0), 0.8, RED),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    sign = Table([
        [p(f"<b>Bajaruvchi</b><br/>{pdf_text(executor_role)}"), p(pdf_text(_name(executor))), p("_____________________")],
        [p(f"<b>{pdf_text(admin_role)}</b>"), p(pdf_text(admin_name)), p("_____________________")],
    ], colWidths=[55 * mm, 70 * mm, 55 * mm], rowHeights=11 * mm)
    sign.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story += [KeepTogether([stamp, Spacer(1, 4 * mm), p("Imzolar", h2), sign])]

    def footer(canvas, doc):
        canvas.setFont(FONT, 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 8 * mm,
                          safe_text(f"NNP — Dalolatnoma {number} · Sahifa {doc.page} · {_dt(timezone.now())}"))

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title=f"Dalolatnoma {number}").build(
        story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
