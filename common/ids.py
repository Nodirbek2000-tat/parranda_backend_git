"""Database id bounds shared by URL converters, query parsing and serializer fields.

Every primary key and Telegram id is a signed 64-bit integer. Values outside that range
must be rejected before they reach the database driver (SQLite raises ``OverflowError``).
"""

MAX_DB_INT = 2**63 - 1
#: URL regex for ids: at most 18 digits always fits in a signed 64-bit integer.
ID_REGEX = r"[0-9]{1,18}"


def fits_db_int(value: int) -> bool:
    return -MAX_DB_INT - 1 <= value <= MAX_DB_INT


class IdConverter:
    """``<id:pk>`` path converter: like ``<int:...>`` but bounded to 64-bit ids."""

    regex = ID_REGEX

    def to_python(self, value: str) -> int:
        return int(value)

    def to_url(self, value: int) -> str:
        return str(value)


def register_id_converter() -> None:
    """Register ``<id:...>`` once (re-registering a converter is deprecated in Django 5.1+)."""
    from django.urls import register_converter
    from django.urls.converters import get_converters

    if "id" not in get_converters():
        register_converter(IdConverter, "id")
