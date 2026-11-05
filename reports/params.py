"""Query parameters of ``GET /api/reports/{key}`` (API_CONTRACT_V3 §11.1).

Period: the V2 §8.1 rules and messages (``stats.periods``): default the last 30 days, ``date_to ≤ today``,
max 366 days. ``monthly_production``: default 1 January … today, max 731 days, aligned to whole months
(``date_from`` → the 1st of its month, ``date_to`` → the end of its month, capped at today).
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from rest_framework.exceptions import NotFound, ValidationError

from accounts.models import User
from common.ids import fits_db_int
from common.roles import Role, role_of
from farm.models import Household, PoultryType
from stats.periods import Period, days_before, parse_dates, validate_period

FILTER_NAMES = ("region", "district", "household", "poultry_type")
MSG_NOT_FOUND = "Xonadon topilmadi."
MSG_TOO_LONG = "Davr {days} kundan oshmasligi kerak."
POULTRY_LABELS = dict(PoultryType.choices)


@dataclass(frozen=True)
class ReportParams:
    period: Period
    region: str | None
    district: str | None
    household: Household | None
    poultry_type: str | None
    user: User
    role: str
    today: date

    def filters(self, supported: tuple[str, ...]) -> dict[str, Any]:
        """Echo of the filters the report supports (§11.3 ``filters``)."""
        values = {
            "region": self.region,
            "district": self.district,
            "household": self.household.pk if self.household is not None else None,
            "poultry_type": self.poultry_type,
        }
        return {name: values[name] for name in FILTER_NAMES if name in supported}

    def filter_lines(self, supported: tuple[str, ...]) -> list[tuple[str, str]]:
        """``[("Tuman", "Navobod tumani"), ...]`` for the file headers (only filters that are set)."""
        lines: list[tuple[str, str]] = []
        if "region" in supported and self.region:
            lines.append(("Viloyat", self.region))
        if "district" in supported and self.district:
            lines.append(("Tuman", self.district))
        if "household" in supported and self.household is not None:
            lines.append(("Xonadon", f"{self.household.code} {self.household.name}"))
        if "poultry_type" in supported and self.poultry_type:
            lines.append(("Parranda turi", POULTRY_LABELS[self.poultry_type]))
        return lines


def _text(params: Mapping[str, Any], name: str) -> str | None:
    value = params.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 120:
        raise ValidationError({name: ["Qiymat 120 belgidan oshmasligi kerak."]})
    return value


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def resolve_report_period(params: Mapping[str, Any], today: date, *, monthly: bool, max_days: int) -> Period:
    values = parse_dates(params)
    date_to = values["date_to"] or today
    date_from = values["date_from"]
    if monthly:
        if date_from is None:
            date_from = date(date_to.year, 1, 1)
        validate_period(date_from, date_to, today, max_days=None)
        date_from = date_from.replace(day=1)
        date_to = min(month_end(date_to), today)
    else:
        if date_from is None:
            date_from = days_before(date_to, 30 - 1)
        validate_period(date_from, date_to, today, max_days=None)
    if (date_to - date_from).days + 1 > max_days:
        raise ValidationError({"non_field_errors": [MSG_TOO_LONG.format(days=max_days)]})
    return Period(date_from, date_to)


def resolve_household(params: Mapping[str, Any], user: User, role: str) -> Household | None:
    raw = params.get("household")
    raw = raw.strip() if isinstance(raw, str) else ""
    own = getattr(user, "household", None) if role == Role.OWNER else None
    if role == Role.OWNER and own is None:
        raise NotFound(MSG_NOT_FOUND)
    if not raw:
        return own
    try:
        pk = int(raw)
    except ValueError:
        raise ValidationError({"household": ["Butun son kiriting."]}) from None
    if not fits_db_int(pk):
        raise NotFound(MSG_NOT_FOUND)
    if own is not None:
        if pk != own.pk:
            raise NotFound(MSG_NOT_FOUND)
        return own
    household = Household.objects.filter(pk=pk).first()
    if household is None:
        raise NotFound(MSG_NOT_FOUND)
    return household


def resolve(params: Mapping[str, Any], user: User, today: date, *, monthly: bool, max_days: int) -> ReportParams:
    role = role_of(user)
    period = resolve_report_period(params, today, monthly=monthly, max_days=max_days)
    poultry_type = _text(params, "poultry_type")
    if poultry_type is not None and poultry_type not in PoultryType.values:
        raise ValidationError({"poultry_type": [f"“{poultry_type}” ruxsat etilgan qiymat emas."]})
    return ReportParams(
        period=period,
        region=_text(params, "region"),
        district=_text(params, "district"),
        household=resolve_household(params, user, role),
        poultry_type=poultry_type,
        user=user,
        role=role,
        today=today,
    )


def restrict(params: ReportParams, supported: tuple[str, ...]) -> ReportParams:
    """Drop the filters a report does not support (§11.1: “ignored, not an error”), so no builder can be
    narrowed by a filter that its ``filters`` echo and file header do not show.

    The OWNER's forced household is never dropped (isolation beats the ignore rule).
    """
    changes: dict[str, Any] = {}
    for name in FILTER_NAMES:
        if name in supported or getattr(params, name) is None:
            continue
        if name == "household" and params.role == Role.OWNER:
            continue
        changes[name] = None
    return replace(params, **changes) if changes else params
