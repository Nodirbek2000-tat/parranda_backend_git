from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .context import AuditContext, _current, client_ip
from .models import AuditSource

BOT_PREFIX = "/api/bot/"
ADMIN_PREFIX = "/admin/"


class AuditContextMiddleware:
    """Sets the audit source and client IP for the duration of each request.

    ``/api/bot/...`` → ``BOT`` (no IP: the bot process is the client), ``/admin/...`` → ``ADMIN``,
    everything else → ``API``.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info
        if path.startswith(BOT_PREFIX):
            context = AuditContext(source=AuditSource.BOT)
        elif path.startswith(ADMIN_PREFIX):
            context = AuditContext(source=AuditSource.ADMIN, ip=client_ip(request.META), user=request.user)
        else:
            context = AuditContext(source=AuditSource.API, ip=client_ip(request.META))
        token = _current.set(context)
        try:
            return self.get_response(request)
        finally:
            _current.reset(token)
