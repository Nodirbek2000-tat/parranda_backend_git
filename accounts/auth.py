"""JWT issuing/revocation, DRF authentication and role permissions.

Every token carries the user's `token_version` (claim `tv`). Bumping it (password change/reset,
delete, Telegram unlink) invalidates all tokens issued before.
"""

import hmac

from django.conf import settings
from django.contrib.auth.models import update_last_login
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Role, User

TV_CLAIM = "tv"
MSG_SESSION_REVOKED = "Sessiya muddati tugagan. Qaytadan kiring."
MSG_MUST_CHANGE_PASSWORD = "Avval vaqtinchalik parolingizni almashtiring."
#: Paths reachable while the user still holds a temporary password.
TEMP_PASSWORD_PATHS = {"/api/auth/me", "/api/auth/change-password", "/api/auth/logout"}


def issue_tokens(user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    refresh[TV_CLAIM] = user.token_version  # copied into the access token too
    update_last_login(None, user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def check_token_version(token, user: User) -> None:
    if token.get(TV_CLAIM) != user.token_version or user.deleted_at is not None:
        raise AuthenticationFailed(MSG_SESSION_REVOKED, "token_revoked")


class NNPJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)  # also rejects inactive users
        check_token_version(validated_token, user)
        return user

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None and result[0].must_change_password:
            if request.path.rstrip("/") not in TEMP_PASSWORD_PATHS:
                raise PermissionDenied(MSG_MUST_CHANGE_PASSWORD, "password_change_required")
        return result


def role_required(*roles: str):
    """Permission class: the user's role is one of `roles` (SUPERADMIN always passes)."""

    class _RolePermission(BasePermission):
        def has_permission(self, request, view):
            user = request.user
            return bool(user and user.is_authenticated and (user.role in roles or user.role == Role.SUPERADMIN))

    return _RolePermission


IsAdmin = role_required(Role.ADMIN)


class IsStaffRole(BasePermission):
    """Any logged-in role except OWNER."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role != Role.OWNER)


class HasBotToken(BasePermission):
    """`X-Bot-Token` equals BOT_API_TOKEN. An empty BOT_API_TOKEN disables the bot API."""

    message = "Bot tokeni noto‘g‘ri"

    def has_permission(self, request, view):
        expected = settings.BOT_API_TOKEN
        received = request.META.get("HTTP_X_BOT_TOKEN", "")
        return bool(expected and received) and hmac.compare_digest(expected.encode(), received.encode())
