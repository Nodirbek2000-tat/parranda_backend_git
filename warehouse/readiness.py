"""“Are the tables of a newer migration there yet?” — keeps Phase 1 paths working on an unmigrated database.

The live server autoreloads new code before the Fix-phase ``migrate`` runs (docs/SCAFFOLD_NOTES.md, “Live
system”). Phase 1 endpoints that gained V3 data (lots on stock operations, ``nearest_expiry`` on products,
dashboard cards) ask :func:`tables_ready` first and fall back to the Phase 1 behaviour while the tables are
missing.

The check runs on the raw DB-API connection (one cheap catalogue query, not counted by
``assertNumQueries``/``CaptureQueriesContext``). A positive answer is cached for the life of the process
(tables never disappear); a negative one is re-checked at most every :data:`RECHECK_SECONDS`, so the
features switch on by themselves right after ``migrate``.
"""

from __future__ import annotations

import logging
import time

from django.apps import apps
from django.db import DatabaseError, connection
from django.db.models import Model

logger = logging.getLogger("nnp.readiness")

RECHECK_SECONDS = 30.0

_ready: set[str] = set()
_missing_checked_at: dict[str, float] = {}


def _existing_tables(names: list[str]) -> set[str]:
    connection.ensure_connection()
    raw = connection.connection
    cursor = raw.cursor()
    try:
        if connection.vendor == "sqlite":
            marks = ", ".join("?" for _ in names)
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({marks})", names)
        elif connection.vendor == "postgresql":
            cursor.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = ANY(current_schemas(false)) "
                "AND tablename = ANY(%s)",
                [names],
            )
        else:  # pragma: no cover - other backends: trust the migrations
            return set(names)
        return {row[0] for row in cursor.fetchall()}
    finally:
        cursor.close()


def tables_ready(*tables: str) -> bool:
    """``True`` when every table exists (cached once true)."""
    wanted = [name for name in tables if name not in _ready]
    if not wanted:
        return True
    now = time.monotonic()
    recent = [name for name in wanted if now - _missing_checked_at.get(name, -RECHECK_SECONDS) < RECHECK_SECONDS]
    if recent:
        return False
    try:
        found = _existing_tables(wanted)
    except DatabaseError:  # pragma: no cover - broken connection: behave like “not migrated”
        logger.warning("Table check failed", exc_info=True)
        found = set()
    _ready.update(found)
    missing = [name for name in wanted if name not in found]
    for name in missing:
        _missing_checked_at[name] = now
    return not missing


def models_ready(*models: type[Model]) -> bool:
    return tables_ready(*(model._meta.db_table for model in models))


def optional_model(app_label: str, model_name: str, *fields: str) -> type[Model] | None:
    """A model of another V3 team, or ``None`` while it (or one of ``fields``) is not written yet or not migrated.

    Lets dashboards and reports read orders / logistics / vet data as soon as those modules land, without
    importing code that may not exist yet in a parallel branch.
    """
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    names = {field.name for field in model._meta.get_fields()} | {
        getattr(field, "attname", field.name) for field in model._meta.get_fields()
    }
    if any(name not in names for name in fields):
        return None
    if not models_ready(model):
        return None
    return model


def reset_cache() -> None:
    """Tests only."""
    _ready.clear()
    _missing_checked_at.clear()
