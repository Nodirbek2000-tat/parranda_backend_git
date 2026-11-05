"""Excel export (API_CONTRACT_V3 §11.4) with openpyxl in write-only mode (constant memory for any size).

Sheet “Hisobot”: rows 1–4 company / title / period / filters, row 5 empty, row 6 header (bold, ``#12372A``
fill, white text), data from row 7, bold totals row; frozen header, autofilter, number formats, real Excel
dates (``DD.MM.YYYY``), widths from content (max 50). Sheet “Xulosa”: summary, generation time and user.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .core import BOOL, DATE, DATETIME, DECIMAL, EMPTY, INT, NUMERIC, PERCENT, Column, ReportData

HEADER_FILL = PatternFill("solid", fgColor="12372A")
HEADER_FONT = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
TITLE_FONT = Font(bold=True, size=14)
SUBTITLE_FONT = Font(bold=True, size=12)
HEADER_ROW = 6
MAX_WIDTH = 50

FORMATS = {
    INT: "#,##0",
    DATE: "DD.MM.YYYY",
    DATETIME: "DD.MM.YYYY HH:MM",
}


def number_format(column: Column) -> str | None:
    if column.type == DECIMAL:
        return "#,##0" if column.places == 0 else "#,##0." + "0" * column.places
    if column.type == PERCENT:
        return '0.' + "0" * column.places + '"%"' if column.places else '0"%"'
    return FORMATS.get(column.type)


def excel_value(column: Column, value: Any) -> Any:
    if value is None:
        return EMPTY if column.type not in NUMERIC else None
    if column.type == DATETIME and isinstance(value, datetime):
        return timezone.localtime(value).replace(tzinfo=None, microsecond=0)
    if column.type == DATE and isinstance(value, datetime):
        return timezone.localtime(value).date()
    if column.type == BOOL:
        return "Ha" if value else "Yo‘q"
    if column.type == INT:
        return int(value)
    if column.type in (DECIMAL, PERCENT):
        from stats.metrics import round_half_up

        return round_half_up(value, column.places)
    return value


def _width(value: Any, column: Column) -> int:
    if value is None:
        return 2
    if isinstance(value, (date, datetime)):
        return 16 if column.type == DATETIME else 11
    if isinstance(value, (int, float, Decimal)):
        return len(f"{value:,.{column.places}f}") + 1 if column.type in (DECIMAL, PERCENT) else len(f"{value:,}") + 1
    return max((len(line) for line in str(value).splitlines()), default=0) + 1


def _plain(sheet: Any, value: Any) -> Any:
    """Text that openpyxl would store as a formula (leading ``=``) is written as a plain string cell."""
    if isinstance(value, str) and value.startswith("="):
        cell = WriteOnlyCell(sheet, value=value)
        cell.data_type = "s"
        return cell
    return value


def build(
    data: ReportData,
    *,
    company: str,
    title: str,
    period_line: str,
    filters_line: str,
    generated_at: datetime,
    user_name: str,
) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Hisobot")
    columns = data.columns

    cells_rows: list[list[Any]] = []
    widths = [len(column.label) + 2 for column in columns]
    for row in data.rows:
        values = [excel_value(column, row.get(column.key)) for column in columns]
        for index, value in enumerate(values):
            widths[index] = max(widths[index], _width(value, columns[index]))
        cells_rows.append(values)
    totals = [excel_value(column, data.totals[column.key]) if column.key in data.totals else None for column in columns]
    for index, value in enumerate(totals):
        if value is not None:
            widths[index] = max(widths[index], _width(value, columns[index]))

    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = min(max(width, 6), MAX_WIDTH)
    last_letter = get_column_letter(len(columns))
    sheet.freeze_panes = f"A{HEADER_ROW + 1}"
    sheet.auto_filter.ref = f"A{HEADER_ROW}:{last_letter}{HEADER_ROW + max(len(cells_rows), 1)}"

    def styled(value: Any, *, font: Font | None = None, fill: PatternFill | None = None, fmt: str | None = None,
               wrap: bool = False) -> WriteOnlyCell:
        cell = WriteOnlyCell(sheet, value=value)
        if cell.data_type == "f":
            cell.data_type = "s"  # user text such as “=HYPERLINK(…)” stays text (formula injection)
        if font is not None:
            cell.font = font
        if fill is not None:
            cell.fill = fill
        if fmt is not None:
            cell.number_format = fmt
        if wrap:
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        return cell

    sheet.append([styled(company, font=TITLE_FONT)])
    sheet.append([styled(title, font=SUBTITLE_FONT)])
    sheet.append([period_line])
    sheet.append([_plain(sheet, filters_line)])
    sheet.append([])
    sheet.append([styled(column.label, font=HEADER_FONT, fill=HEADER_FILL, wrap=True) for column in columns])
    formats = [number_format(column) for column in columns]
    for values in cells_rows:
        sheet.append(
            [styled(value, fmt=formats[i]) if formats[i] and value not in (None, EMPTY) else _plain(sheet, value)
             for i, value in enumerate(values)]
        )
    if data.totals:
        row = []
        for index, value in enumerate(totals):
            if index == 0 and value is None:
                row.append(styled("Jami", font=BOLD))
            elif value is None:
                row.append(styled(None, font=BOLD))
            else:
                row.append(styled(value, font=BOLD, fmt=formats[index]))
        sheet.append(row)

    summary = workbook.create_sheet("Xulosa")
    summary.column_dimensions["A"].width = 34
    summary.column_dimensions["B"].width = 48
    summary.append([styled(title, font=SUBTITLE_FONT)])
    summary.append([period_line])
    summary.append([filters_line])
    summary.append([])
    for label, value in data.summary:
        summary.append([styled(label, font=BOLD), _plain(summary, value)])
    summary.append([])
    summary.append([styled("Qatorlar", font=BOLD), len(data.rows)])
    summary.append(
        [styled("Yaratilgan", font=BOLD), timezone.localtime(generated_at).strftime("%d.%m.%Y %H:%M")]
    )
    summary.append([styled("Yaratdi", font=BOLD), user_name])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
