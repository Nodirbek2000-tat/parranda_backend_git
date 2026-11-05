"""Authentication of the Telegram bot process (``X-Bot-Token`` shared secret)."""

from __future__ import annotations

import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

BOT_TOKEN_HEADER = "HTTP_X_BOT_TOKEN"


class HasBotToken(BasePermission):
    """Allows the request only if ``X-Bot-Token`` equals ``BOT_API_TOKEN``.

    An empty ``BOT_API_TOKEN`` disables every bot endpoint.
    """

    message = "Bot tokeni noto‘g‘ri"

    def has_permission(self, request, view) -> bool:
        expected: str = settings.BOT_API_TOKEN
        received: str = request.META.get(BOT_TOKEN_HEADER, "")
        if not expected or not received:
            return False
        return hmac.compare_digest(expected.encode(), received.encode())


class TelegramIdLoginThrottle(SimpleRateThrottle):
    """``login`` rate (10/min) keyed by the Telegram id sent to ``/api/bot/link``."""

    scope = "login"

    def get_cache_key(self, request, view) -> str | None:
        data = request.data if isinstance(request.data, dict) else {}
        telegram_id = str(data.get("telegram_id", "")).strip()
        if not telegram_id:
            return None
        return self.cache_format % {"scope": f"{self.scope}-bot", "ident": telegram_id}
