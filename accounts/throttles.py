"""Login throttles keyed by the account being attacked, independent of the client IP.

The IP based ``login`` scope depends on ``X-Forwarded-For`` from the proxy chain; these
throttles cap password guesses per username however many IPs or Telegram accounts an
attacker uses. ``/api/auth/login`` and ``/api/bot/link`` share the same buckets.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from rest_framework.throttling import SimpleRateThrottle


class _UsernameThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view) -> str | None:
        data = request.data if isinstance(request.data, Mapping) else {}
        username = data.get("username")
        if not isinstance(username, str) or not username.strip():
            return None
        # Hashed: cache backends such as memcached reject arbitrary characters in keys.
        ident = hashlib.sha256(username.strip().lower().encode()).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": ident}


class UsernameLoginMinuteThrottle(_UsernameThrottle):
    scope = "login_username"


class UsernameLoginHourThrottle(_UsernameThrottle):
    scope = "login_username_hour"
