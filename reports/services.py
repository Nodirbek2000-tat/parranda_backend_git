"""Report export service (API_CONTRACT_V3 §11): build → row limits → render → audit.

Views only parse the request and call :func:`preview` / :func:`export`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from audit import services as audit
from audit.models import AuditAction, AuditModule, AuditObjectType
from common.formatting import fmt_number

from .core import ReportData, json_value
from .params import ReportParams
from .registry import ReportDef

ROW_LIMITS = {"json": 500, "xlsx": 20_000, "pdf": 3_000}
MSG_TOO_MANY = "Ma’lumot juda ko‘p ({rows} qator). Davrni yoki filtrlarni toraytiring."
CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}

_SLUG_DROP = re.compile(r"[‘’ʻʼ'`]")
_SLUG_SEP = re.compile(r"[^a-z0-9]+")


def slugify_title(title: str) -> str:
    """``Ombor qoldig‘i`` → ``ombor-qoldigi`` (ASCII, for the plain ``filename``)."""
    text = _SLUG_DROP.sub("", title.lower())
    return _SLUG_SEP.sub("-", text).strip("-") or "hisobot"


def filenames(report: ReportDef, params: ReportParams, extension: str) -> tuple[str, str]:
    """(ASCII ``kunlik-ishlab-chiqarish_2026-11-01_2026-11-04.xlsx``, UTF-8 ``Kunlik ishlab chiqarish 01.11.2026-04.11.2026.xlsx``)."""
    period = params.period
    ascii_name = f"{slugify_title(report.title)}_{period.date_from.isoformat()}_{period.date_to.isoformat()}.{extension}"
    utf8_name = (
        f"{report.title} {period.date_from.strftime('%d.%m.%Y')}-{period.date_to.strftime('%d.%m.%Y')}.{extension}"
    )
    return ascii_name, utf8_name


def content_disposition(report: ReportDef, params: ReportParams, extension: str) -> str:
    ascii_name, utf8_name = filenames(report, params, extension)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(utf8_name, safe='')}"


def period_line(params: ReportParams) -> str:
    period = params.period
    return f"Davr: {period.date_from.strftime('%d.%m.%Y')} – {period.date_to.strftime('%d.%m.%Y')}"


def filters_line(report: ReportDef, params: ReportParams) -> str:
    lines = params.filter_lines(report.supported())
    if not lines:
        return "Filtrlar: yo‘q"
    return "Filtrlar: " + "; ".join(f"{label} — {value}" for label, value in lines)


def _check_rows(data: ReportData, fmt: str) -> None:
    limit = ROW_LIMITS[fmt]
    if fmt != "json" and len(data.rows) > limit:
        raise ValidationError({"non_field_errors": [MSG_TOO_MANY.format(rows=fmt_number(len(data.rows)))]})


@dataclass
class Rendered:
    content: bytes
    content_type: str
    disposition: str


def _generated_at() -> datetime:
    return timezone.localtime(timezone.now()).replace(microsecond=0)


def preview(report: ReportDef, params: ReportParams) -> dict[str, Any]:
    """§11.3 JSON preview (max 500 rows + ``truncated``)."""
    data = report.builder(params)
    limit = ROW_LIMITS["json"]
    rows = data.rows[:limit]
    columns = data.columns
    return {
        "key": report.key,
        "title": report.title,
        "period": params.period.as_dict(previous=False),
        "filters": params.filters(report.supported()),
        "generated_at": _generated_at().isoformat(),
        "columns": [column.as_dict() for column in columns],
        "rows": [{column.key: json_value(column, row.get(column.key)) for column in columns} for row in rows],
        "totals": {
            column.key: json_value(column, data.totals[column.key]) for column in columns if column.key in data.totals
        },
        "summary": [{"label": label, "value": value} for label, value in data.summary],
        "row_count": len(data.rows),
        "truncated": len(data.rows) > limit,
    }


def export(report: ReportDef, params: ReportParams, fmt: str) -> Rendered:
    """xlsx / pdf file + audit ``reports / export`` (``after: {key, format, filters}``)."""
    from core.settings_service import get_settings

    data = report.builder(params)
    _check_rows(data, fmt)
    company = get_settings().company_name
    generated_at = _generated_at()
    common = {
        "company": company,
        "title": report.title,
        "period_line": period_line(params),
        "filters_line": filters_line(report, params),
        "generated_at": generated_at,
    }
    if fmt == "xlsx":
        from . import xlsx

        content = xlsx.build(data, user_name=params.user.full_name or params.user.username, **common)
    else:
        from . import pdf

        content = pdf.build(data, **common)
    filters = {
        "date_from": params.period.date_from.isoformat(),
        "date_to": params.period.date_to.isoformat(),
        **{key: value for key, value in params.filters(report.supported()).items() if value is not None},
    }
    audit.record(
        module=AuditModule.REPORTS,
        action=AuditAction.EXPORT,
        actor=params.user,
        object_type=AuditObjectType.REPORT,
        op_id=report.key[:20],
        after={"key": report.key, "format": fmt, "filters": filters, "rows": len(data.rows)},
    )
    return Rendered(content, CONTENT_TYPES[fmt], content_disposition(report, params, fmt))
