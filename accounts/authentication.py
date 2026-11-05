"""DRF authentication: SimpleJWT plus per-user token revocation (see ``tokens.py``).

A user who still holds a temporary password (``must_change_password``) may only call the
endpoints needed to change it (TZ §3: "Birinchi kirishda parolni almashtirish talab qilinadi").
Everything else answers ``403`` with :data:`MSG_MUST_CHANGE_PASSWORD` — the Web App guard is
no longer the only enforcement.
"""

from __future__ import annotations

from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import Token

from .models import User
from .tokens import ensure_token_current

MSG_MUST_CHANGE_PASSWORD = "Avval vaqtinchalik parolingizni almashtiring."

#: (method or ``None`` for any method, path) reachable with a temporary password.
TEMP_PASSWORD_ALLOWED: frozenset[tuple[str | None, str]] = frozenset(
    {
        (None, "/api/auth/me"),
        (None, "/api/auth/change-password"),
        (None, "/api/auth/logout"),
        ("GET", "/api/settings"),  # company name for the change-password screen
    }
)


def temp_password_allows(method: str, path: str) -> bool:
    path = path.rstrip("/") or "/"
    return (None, path) in TEMP_PASSWORD_ALLOWED or (method.upper(), path) in TEMP_PASSWORD_ALLOWED


class MustChangePassword(PermissionDenied):
    default_code = "password_change_required"


class RevocableJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user = result[0]
            if user.must_change_password and not temp_password_allows(request.method, request.path):
                raise MustChangePassword(MSG_MUST_CHANGE_PASSWORD)
        return result

    def get_user(self, validated_token: Token) -> User:
        user = super().get_user(validated_token)
        ensure_token_current(validated_token, user)
        return user
