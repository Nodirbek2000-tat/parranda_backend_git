"""Vet parts of ``GET /api/bot/users/{tid}/tasks`` (API_CONTRACT_V3 §14.2).

The endpoint lives in the logistics app (``logistics.bot``); the vet app plugs in through
``logistics.bot.register_extension`` from :class:`vet.apps.VetConfig`:

- VET: the whole payload (``visits_today``, ``visits_overdue``, ``vaccinations_due``, ``vaccinations_overdue``,
  ``new_requests``) — own and unassigned work, each list ≤ 10;
- OWNER: ``upcoming_vet`` (next planned visit / vaccination of the own household);
- ADMIN: ``vet_overdue`` (overdue visits + overdue vaccinations).
"""

from __future__ import annotations

from typing import Any

from common.roles import Role

from . import selectors


def vet_extension(user, payload: dict[str, Any]) -> None:
    if not selectors.tables_ready():
        # Unmigrated database (before the Fix-phase ``migrate``): empty lists instead of a 500.
        payload.update(
            {"visits_today": [], "visits_overdue": [], "vaccinations_due": [], "vaccinations_overdue": [],
             "new_requests": 0}
        )
        return
    data = selectors.bot_tasks(user)
    data.pop("role", None)
    payload.update(data)


def owner_extension(user, payload: dict[str, Any]) -> None:
    if not selectors.tables_ready():
        return
    household = getattr(user, "household", None)
    if household is None:
        return
    data = selectors.owner_dashboard(household)
    payload["upcoming_vet"] = [item for item in (data["next_visit"], data["next_vaccination"]) if item]


def admin_extension(user, payload: dict[str, Any]) -> None:
    if not selectors.tables_ready():
        return
    payload["vet_overdue"] = selectors.admin_vet_overdue()


def register() -> None:
    try:
        from logistics import bot
    except ImportError:  # pragma: no cover - logistics is always installed
        return
    bot.register_extension(Role.VET, vet_extension)
    bot.register_extension(Role.OWNER, owner_extension)
    bot.register_extension(Role.ADMIN, admin_extension)
