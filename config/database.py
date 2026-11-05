"""``DATABASE_URL`` parsing without extra dependencies.

SQLite stays the default (``DB_PATH`` or ``db.sqlite3``). ``DATABASE_URL`` switches the project to
PostgreSQL (``postgres://user:password@host:5432/name?sslmode=require``) or to another SQLite file
(``sqlite:///absolute/path.sqlite3``). The PostgreSQL driver (``psycopg``) is only needed when such a
URL is configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

SQLITE_OPTIONS = {
    # SQLite ignores SELECT ... FOR UPDATE. IMMEDIATE transactions take the write lock when
    # atomic() starts, so concurrent stock/poultry operations are serialized (instead of failing
    # with "database is locked" on lock upgrade).
    "transaction_mode": "IMMEDIATE",
    "timeout": 20,
}

_POSTGRES_SCHEMES = {"postgres", "postgresql", "pgsql"}
#: Query parameters passed to the PostgreSQL driver as connection options.
_POSTGRES_OPTIONS = {
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "connect_timeout",
    "application_name",
    "options",
    "target_session_attrs",
}


class DatabaseUrlError(ValueError):
    """``DATABASE_URL`` cannot be understood."""


def sqlite_config(path: Path | str) -> dict[str, Any]:
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path), "OPTIONS": dict(SQLITE_OPTIONS)}


def parse_database_url(url: str, *, conn_max_age: int = 0) -> dict[str, Any]:
    """Translate a database URL into a Django ``DATABASES`` entry."""
    url = url.strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "sqlite":
        # sqlite:///relative.db → "relative.db", sqlite:////abs/path.db → "/abs/path.db",
        # sqlite:///E:/data/nnp.db → "E:/data/nnp.db"
        path = unquote(parts.path)
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]  # Windows drive letter
        elif path.startswith("/") and not path.startswith("//"):
            path = path[1:]
        elif path.startswith("//"):
            path = path[1:]
        if not path:
            raise DatabaseUrlError("SQLite URL needs a file path: sqlite:///path/to/db.sqlite3")
        return sqlite_config(path)
    if scheme not in _POSTGRES_SCHEMES:
        raise DatabaseUrlError(f"Unsupported DATABASE_URL scheme: {scheme or '(empty)'}")
    name = unquote(parts.path.lstrip("/"))
    if not name:
        raise DatabaseUrlError("PostgreSQL URL needs a database name: postgres://user:pass@host:5432/name")
    try:
        port = parts.port
    except ValueError as exc:
        raise DatabaseUrlError("Invalid port in DATABASE_URL") from exc
    options = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if key not in _POSTGRES_OPTIONS:
            raise DatabaseUrlError(f"Unsupported DATABASE_URL option: {key}")
        options[key] = value
    config: dict[str, Any] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(port) if port else "",
        "CONN_MAX_AGE": conn_max_age,
        "CONN_HEALTH_CHECKS": conn_max_age > 0,
        "OPTIONS": options,
    }
    return config
