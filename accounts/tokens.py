"""JWT issuing and revocation.

Every token carries the user's ``token_version`` (claim ``tv``). Bumping the version
(password reset/change, Telegram unlink) invalidates every access and refresh token issued
before, so a stolen token stops working as soon as the account is secured.
"""

from __future__ import annotations

import logging

from django.contrib.auth.models import update_last_login
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import BlacklistMixin, RefreshToken, Token

from .models import User

TOKEN_VERSION_CLAIM = "tv"
#: ``QuerySet.update(token_version=NEXT_TOKEN_VERSION)`` revokes the sessions of many users.
NEXT_TOKEN_VERSION = F("token_version") + 1
MSG_SESSION_REVOKED = "Sessiya muddati tugagan. Qaytadan kiring."

logger = logging.getLogger("nnp.auth")
_BLACKLIST_TABLE = "token_blacklist_outstandingtoken"
_blacklist_ready = False


def blacklist_tables_ready() -> bool:
    """``True`` once the ``token_blacklist`` migrations are applied (cached after the first positive answer)."""
    global _blacklist_ready
    if not _blacklist_ready:
        _blacklist_ready = _BLACKLIST_TABLE in connection.introspection.table_names()
    return _blacklist_ready


class NNPRefreshToken(RefreshToken):
    """SimpleJWT refresh token with logout blacklisting (V3 §2.3).

    Tolerates a database where the ``token_blacklist`` migrations are not applied yet (a server that
    picked up the new code before ``manage.py migrate``): login and refresh keep working without the
    outstanding/blacklist rows instead of failing with 500. Once the tables exist this is plain SimpleJWT.
    """

    @classmethod
    def for_user(cls, user: User) -> NNPRefreshToken:
        try:
            with transaction.atomic():  # savepoint: a missing table never poisons an outer transaction
                return super().for_user(user)
        except DatabaseError:
            if blacklist_tables_ready():
                raise
            logger.warning("token_blacklist jadvallari yo‘q (migrate qilinmagan): token ro‘yxatga olinmadi")
            return super(BlacklistMixin, cls).for_user(user)  # Token.for_user without the outstanding row

    def check_blacklist(self) -> None:
        try:
            with transaction.atomic():
                super().check_blacklist()
        except DatabaseError:
            if blacklist_tables_ready():
                raise


def issue_tokens(user: User) -> dict[str, str]:
    refresh = NNPRefreshToken.for_user(user)
    refresh[TOKEN_VERSION_CLAIM] = user.token_version  # copied to the access token
    update_last_login(None, user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def ensure_token_current(token: Token, user: User) -> None:
    """Reject a token issued before the user's sessions were revoked."""
    if token.get(TOKEN_VERSION_CLAIM) != user.token_version:
        raise AuthenticationFailed(MSG_SESSION_REVOKED, "token_revoked")


def bump_token_version(user: User) -> None:
    """Revoke all of the user's tokens. The caller must save ``token_version``."""
    user.token_version += 1
