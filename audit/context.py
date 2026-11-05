"""Per-request audit context: where an action came from (API / BOT / ADMIN) and the client IP.

The context is set by :class:`audit.middleware.AuditContextMiddleware` for every HTTP request.
Code running outside a request (management commands such as ``seed_demo``, migrations) has no
context, and :func:`audit.services.record` writes nothing there (API_CONTRACT_V2 §2.1).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from rest_framework.settings import api_settings

from .models import AuditSource


@dataclass(frozen=True)
class AuditContext:
    source: str
    ip: str | None = None
    #: Django session user (only for Django admin requests); API views pass their actor explicitly.
    user: Any = None


_current: ContextVar[AuditContext | None] = ContextVar("nnp_audit_context", default=None)


def current() -> AuditContext | None:
    return _current.get()


@contextmanager
def audit_context(source: str = AuditSource.API, ip: str | None = None, user: Any = None) -> Iterator[AuditContext]:
    """Run a block with an explicit audit context (tests, scripted maintenance)."""
    context = AuditContext(source=source, ip=ip, user=user)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def client_ip(meta: Mapping[str, Any]) -> str | None:
    """Client IP resolved exactly like the login throttle (DRF ``get_ident`` with ``NUM_PROXIES``).

    Returns ``None`` when the value is not a valid IPv4/IPv6 address.
    """
    xff = meta.get("HTTP_X_FORWARDED_FOR")
    remote_addr = meta.get("REMOTE_ADDR")
    num_proxies = api_settings.NUM_PROXIES
    if num_proxies is not None:
        if num_proxies == 0 or xff is None:
            candidate = remote_addr
        else:
            addrs = xff.split(",")
            candidate = addrs[-min(num_proxies, len(addrs))].strip()
    else:
        candidate = "".join(xff.split()) if xff else remote_addr
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
