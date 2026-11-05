"""Account business logic: credentials, temporary passwords, Telegram links, tokens.

Every change writes its audit row (``audit.services.record``) inside the same transaction.
"""

from __future__ import annotations

import re
import secrets
import string

from django.db import transaction
from django.utils import timezone

from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.roles import CREATABLE_STAFF_ROLES

from . import tokens
from .models import User

TEMP_PASSWORD_LENGTH = 10
_ALPHABET = string.ascii_letters + string.digits
MSG_BAD_CREDENTIALS = "Login yoki parol noto‘g‘ri"
MSG_BLOCKED = "Hisobingiz bloklangan. Administratorga murojaat qiling."


class CredentialsInvalid(Exception):
    """Unknown username or wrong password. ``user`` is the matching account, if any (for the audit log)."""

    def __init__(self, user: User | None = None) -> None:
        super().__init__("bad credentials")
        self.user = user


class AccountBlocked(Exception):
    """Correct credentials but ``is_active`` is false."""

    def __init__(self, user: User | None = None) -> None:
        super().__init__("blocked")
        self.user = user


def generate_temp_password(length: int = TEMP_PASSWORD_LENGTH) -> str:
    """Random letters+digits password that always contains both a letter and a digit."""
    while True:
        password = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in password) and any(c.isalpha() for c in password):
            return password


def verify_credentials(username: str, password: str) -> User:
    user = User.objects.filter(username=username).first()
    if user is None:
        # Run the hasher anyway so response time does not reveal existing usernames.
        User().set_password(password)
        raise CredentialsInvalid(None)
    if not user.check_password(password):
        raise CredentialsInvalid(user)
    if not user.is_active:
        raise AccountBlocked(user)
    return user


def issue_tokens(user: User) -> dict[str, str]:
    return tokens.issue_tokens(user)


def create_user(
    *,
    username: str,
    first_name: str,
    last_name: str,
    phone: str,
    role: str,
    actor: User | None = None,
) -> tuple[User, str]:
    """Create a user with a temporary password (``must_change_password=True``).

    Writes a ``users``/``create`` audit row: the caller runs this inside ``transaction.atomic()``.
    """
    password = generate_temp_password()
    user = User(
        username=username,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        role=role,
        must_change_password=True,
    )
    user.set_password(password)
    user.save()
    audit.record_create(AuditModule.USERS, user, actor)
    return user, password


def create_staff_user(*, actor: User | None = None, **fields: str) -> tuple[User, str]:
    """ADMIN / WAREHOUSE / LOGISTICS / VET account (OWNER accounts come with a household)."""
    from notifications import events  # local import: notifications imports accounts

    if fields.get("role") not in CREATABLE_STAFF_ROLES:
        raise ValueError("Staff users must be ADMIN, WAREHOUSE, LOGISTICS or VET")
    with transaction.atomic():
        user, password = create_user(actor=actor, **fields)
        events.user_created(user)
    return user, password


USER_EDITABLE_FIELDS = frozenset({"first_name", "last_name", "phone", "is_active"})


def update_user(user: User, changes: dict, *, actor: User | None = None) -> User:
    """Profile fields and block/unblock; the audit row holds only the fields that really changed."""
    unknown = set(changes) - USER_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    with transaction.atomic():
        before = audit.snapshot(user)
        for field, value in changes.items():
            setattr(user, field, value)
        if changes:
            user.save(update_fields=list(changes))
        audit.record_update(AuditModule.USERS, user, actor, before)
    return user


def reset_password(user: User, *, actor: User | None = None) -> str:
    """New temporary password; every existing session of the user is revoked."""
    password = generate_temp_password()
    with transaction.atomic():
        user.set_password(password)
        user.must_change_password = True
        tokens.bump_token_version(user)
        user.save(update_fields=["password", "must_change_password", "token_version"])
        audit.record(
            module=AuditModule.USERS,
            action=AuditAction.PASSWORD_RESET,
            obj=user,
            actor=actor,
            after={"must_change_password": True},
        )
    return password


def change_password(user: User, new_password: str) -> None:
    """Set the user's own password; all tokens issued so far (this session too) stop working."""
    with transaction.atomic():
        user.set_password(new_password)
        user.must_change_password = False
        tokens.bump_token_version(user)
        user.save(update_fields=["password", "must_change_password", "token_version"])
        audit.record(
            module=AuditModule.AUTH,
            action=AuditAction.PASSWORD_CHANGE,
            obj=user,
            actor=user,
            after={"must_change_password": False},
        )


# --- Sessions (API_CONTRACT_V3 §2.3, §2.4) ------------------------------------------------

MSG_TOKEN_INVALID = "Token yaroqsiz."
MSG_OWN_SESSIONS = "O‘z sessiyalaringizni “Chiqish” orqali yakunlang."


class RefreshTokenInvalid(Exception):
    """The submitted refresh token does not decode, is expired or belongs to another user."""


def logout(user: User, *, refresh: str | None, all_sessions: bool = False) -> None:
    """Blacklist one refresh token, or (``all_sessions``) revoke every token of the user at once.

    Already blacklisted tokens are accepted (idempotent). The access token of the current session
    stays valid until it expires (≤ 60 min); the Web App drops it immediately (deviation 20.6).
    """
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.settings import api_settings as jwt_settings

    token = None
    if refresh:
        try:
            token = tokens.NNPRefreshToken(refresh)
        except TokenError:
            if not _is_blacklisted_for(refresh, user):
                if not all_sessions:
                    raise RefreshTokenInvalid() from None
        if token is not None and str(token.payload.get(jwt_settings.USER_ID_CLAIM)) != str(user.pk):
            if not all_sessions:
                raise RefreshTokenInvalid()
            token = None
    elif not all_sessions:
        raise RefreshTokenInvalid()
    with transaction.atomic():
        if token is not None:
            token.blacklist()
        if all_sessions:
            tokens.bump_token_version(user)
            user.sessions_revoked_at = timezone.now()
            user.save(update_fields=["token_version", "sessions_revoked_at"])
        audit.record(
            module=AuditModule.AUTH, action=AuditAction.LOGOUT, obj=user, actor=user, after={"all": all_sessions}
        )


def _is_blacklisted_for(raw: str, user: User) -> bool:
    """``True`` when ``raw`` is a correctly signed refresh token of ``user`` that is already blacklisted."""
    from rest_framework_simplejwt.settings import api_settings as jwt_settings
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    try:
        token = tokens.NNPRefreshToken(raw, verify=False)
        # Signature check without the blacklist / expiry checks.
        token.token_backend.decode(raw, verify=True)
    except Exception:  # noqa: BLE001 - any decoding problem means "not ours"
        return False
    if str(token.payload.get(jwt_settings.USER_ID_CLAIM)) != str(user.pk):
        return False
    return BlacklistedToken.objects.filter(token__jti=token.payload.get(jwt_settings.JTI_CLAIM)).exists()


def end_sessions(target: User, *, actor: User, unlink_telegram: bool = False) -> None:
    """Admin action: revoke every Web App session of ``target`` (and optionally the Telegram link)."""
    if target.pk == actor.pk:
        raise ValueError(MSG_OWN_SESSIONS)
    with transaction.atomic():
        target = User.objects.select_for_update().get(pk=target.pk)
        old_telegram = target.telegram_id
        tokens.bump_token_version(target)
        target.sessions_revoked_at = timezone.now()
        fields = ["token_version", "sessions_revoked_at"]
        if unlink_telegram and old_telegram is not None:
            target.telegram_id = None
            fields.append("telegram_id")
        target.save(update_fields=fields)
        if unlink_telegram and old_telegram is not None:
            _record_unlink(target, old_telegram, actor)
        audit.record(
            module=AuditModule.USERS,
            action=AuditAction.SESSIONS_REVOKED,
            obj=target,
            actor=actor,
            after={"unlink_telegram": bool(unlink_telegram)},
        )


def _record_unlink(user: User, old_telegram_id: int, actor: User | None, reason: str = "") -> None:
    after: dict = {"telegram_id": None}
    if reason:
        after["reason"] = reason
    audit.record(
        module=AuditModule.TELEGRAM,
        action=AuditAction.UNLINK,
        obj=user,
        actor=actor,
        before={"telegram_id": old_telegram_id},
        after=after,
    )


def unlink_telegram(user: User, *, actor: User | None = None) -> None:
    """Remove the Telegram link and revoke the user's sessions (incl. Web App logins via Telegram)."""
    with transaction.atomic():
        old = user.telegram_id
        if old is not None:
            user.telegram_id = None
            tokens.bump_token_version(user)
            user.save(update_fields=["telegram_id", "token_version"])
            _record_unlink(user, old, actor)


@transaction.atomic
def link_telegram(*, telegram_id: int, username: str, password: str) -> User:
    """Bind ``telegram_id`` to the account, removing it from any other account first."""
    user = verify_credentials(username, password)
    # The previous holder of this Telegram id loses the link and its sessions.
    previous = list(User.objects.select_for_update().filter(telegram_id=telegram_id).exclude(pk=user.pk))
    if previous:
        User.objects.filter(pk__in=[other.pk for other in previous]).update(
            telegram_id=None, token_version=tokens.NEXT_TOKEN_VERSION
        )
        for other in previous:
            _record_unlink(other, telegram_id, user, reason="relinked")
    if user.telegram_id != telegram_id:
        old = user.telegram_id
        user.telegram_id = telegram_id
        user.save(update_fields=["telegram_id"])
        audit.record(
            module=AuditModule.TELEGRAM,
            action=AuditAction.LINK,
            obj=user,
            actor=user,
            before={"telegram_id": old},
            after={"telegram_id": telegram_id},
        )
    return user


# --- Login audit (each attempt in its own small transaction) ------------------------------

LOGIN_PASSWORD = "password"
LOGIN_TELEGRAM = "telegram"


def record_login_success(user: User, method: str) -> None:
    with transaction.atomic():
        audit.record(
            module=AuditModule.AUTH,
            action=AuditAction.LOGIN_SUCCESS,
            obj=user,
            actor=user,
            after={"method": method},
        )


#: Username charset of Django's ``UnicodeUsernameValidator`` (ASCII digits/letters plus ``.@+-_``).
_USERNAME_RE = re.compile(r"^[\w.@+-]+$")
MASK_KEEP = 2
MASK_INVALID = "(noto‘g‘ri login)"


def mask_submitted_username(value: str) -> str:
    """What a failed login with an UNKNOWN account stores as ``actor_name`` (deviation from V2 §2.1).

    People regularly type their password into the login field; storing the raw value would keep that
    password forever in the immutable, admin-readable audit table. Only the first two characters are kept
    (``"no***"`` for ``"nobody_here"``): enough to see that one name is being attacked, never a secret.
    Values that cannot be a username (invalid charset or > 150 chars) become ``"(noto‘g‘ri login)"``.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > audit.MAX_SUBMITTED_USERNAME or not _USERNAME_RE.match(value):
        return MASK_INVALID
    return f"{value[:MASK_KEEP]}***"


def record_login_failure(*, method: str, reason: str, user: User | None = None, submitted_username: str = "") -> None:
    """Rejected login. Unknown account → ``actor`` is null and ``actor_name`` the MASKED submitted username."""
    with transaction.atomic():
        audit.record(
            module=AuditModule.AUTH,
            action=AuditAction.LOGIN_FAILURE,
            obj=user,
            actor=user,
            actor_name=None if user is not None else mask_submitted_username(submitted_username),
            after={"method": method, "reason": reason},
        )
