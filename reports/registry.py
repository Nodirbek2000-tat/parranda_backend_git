"""Report definitions (API_CONTRACT_V3 §11.1–11.2): key, title, roles, supported filters, period rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from common.roles import Role

from . import builders
from .core import ReportData
from .params import ReportParams

FORMATS = ("xlsx", "pdf", "json")
ALL_FILTERS = ("date_from", "date_to", "region", "district", "household", "poultry_type")
NO_POULTRY = ("date_from", "date_to", "region", "district", "household")
DATES_ONLY = ("date_from", "date_to")

A, W, L, V, O = Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS, Role.VET, Role.OWNER


@dataclass(frozen=True)
class ReportDef:
    key: str
    title: str
    description: str
    roles: frozenset[str]
    filters: tuple[str, ...]
    builder: Callable[[ReportParams], ReportData]
    max_days: int = 366
    monthly: bool = False

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "formats": list(FORMATS),
            "filters": list(self.filters),
            "max_days": self.max_days,
        }

    def supported(self) -> tuple[str, ...]:
        """Non-date filters of the report."""
        return tuple(name for name in self.filters if name not in DATES_ONLY)


REPORTS: tuple[ReportDef, ...] = (
    ReportDef(
        "daily_production",
        "Kunlik ishlab chiqarish",
        "Har bir kunlik hisobot: parranda soni, yem, go‘sht, FCR (yem/go‘sht), o‘rtacha vazn va nobud bo‘lish.",
        frozenset({A, W, O}),
        ALL_FILTERS,
        builders.daily_production,
    ),
    ReportDef(
        "monthly_production",
        "Oylik ishlab chiqarish",
        "Oylar kesimida go‘sht, yem sarfi, FCR, parranda boshiga go‘sht, o‘rtacha vazn va o‘lim ko‘rsatkichlari.",
        frozenset({A, W, O}),
        ALL_FILTERS,
        builders.monthly_production,
        max_days=731,
        monthly=True,
    ),
    ReportDef(
        "poultry_movements",
        "Parrandalar harakati",
        "Tarqatish, qaytarish, nobud bo‘lish va chiqim operatsiyalari ro‘yxati.",
        frozenset({A, W}),
        ALL_FILTERS,
        builders.poultry_movements,
    ),
    ReportDef(
        "households_activity",
        "Xonadonlar faoliyati",
        "Xonadonlar bo‘yicha hisobot intizomi, parranda soni, buyurtmalar va yetkazib berishlar.",
        frozenset({A}),
        NO_POULTRY,
        builders.households_activity,
    ),
    ReportDef(
        "mortality",
        "Nobud bo‘lish statistikasi",
        "Xonadonlar va parranda turlari bo‘yicha nobud bo‘lish va o‘lim foizi.",
        frozenset({A, V}),
        ALL_FILTERS,
        builders.mortality,
    ),
    ReportDef(
        "feed_consumption",
        "Yem sarfi",
        "Hisobotlardagi yem sarfi va ombordan berilgan yem, parranda boshiga kunlik sarf va FCR (yem/go‘sht).",
        frozenset({A, W}),
        NO_POULTRY,
        builders.feed_consumption,
    ),
    ReportDef(
        "stock_balance",
        "Ombor qoldig‘i",
        "Mahsulotlar bo‘yicha boshlang‘ich qoldiq, kirim, chiqim, inventarizatsiya va yakuniy qoldiq.",
        frozenset({A, W}),
        DATES_ONLY,
        builders.stock_balance,
    ),
    ReportDef(
        "logistics_efficiency",
        "Logistika samaradorligi",
        "Topshiriqlar muddati, o‘z vaqtida yetkazish foizi va haydovchilar kesimi.",
        frozenset({A, L}),
        NO_POULTRY,
        builders.logistics_efficiency,
    ),
    ReportDef(
        "vet_status",
        "Veterinariya holati",
        "Xonadonlar bo‘yicha tekshiruvlar, emlashlar va ochiq kasalliklar.",
        frozenset({A, V}),
        NO_POULTRY,
        builders.vet_status,
    ),
    ReportDef(
        "household_results",
        "Xonadonlar kesimida natijalar",
        "Har bir xonadon bo‘yicha ishlab chiqarish, o‘lim, intizom, olingan mahsulotlar va emlashlar.",
        frozenset({A, O}),
        ALL_FILTERS,
        builders.household_results,
    ),
)

BY_KEY: dict[str, ReportDef] = {report.key: report for report in REPORTS}


def available_for(role: str | None) -> list[ReportDef]:
    return [report for report in REPORTS if role in report.roles]
