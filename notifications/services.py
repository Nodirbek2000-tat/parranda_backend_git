"""Notification engine V3 (API_CONTRACT_V3 §9): rows are created only after the business transaction commits.

Public API for every module::

    from notifications import registry
    from notifications.services import notify_on_commit

    notify_on_commit(
        "order_approved",
        key=order.pk,                         # event_key = f"{event}:{key}:{user_id}"
        text=compose("order_approved", rows, "Tasdiqlandi", order.code),
        staff_path=f"/orders/{order.pk}",     # path for ADMIN/WAREHOUSE/LOGISTICS/VET recipients
        owner_path=f"/my/orders/{order.pk}",  # path for OWNER recipients
        related_op=order.code,
        contextual={registry.HOUSEHOLD_OWNER: order.household.owner},
        object_type="Order", object_id=order.pk,
    )

Recipients are resolved at commit time: role users (registry default roles or the Super Admin override)
∪ contextual users (when enabled for the event) − inactive users, de-duplicated. One row per recipient:
``PENDING`` (with ``chat_id``) when the user has Telegram and Telegram is on for the event, otherwise
``SKIPPED`` (Web App bell only). ``enabled=false`` queues nothing.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from accounts.models import User
from common.roles import NOTIFIABLE_ROLES, Role

from . import registry
from .models import Notification, NotificationSetting, NotificationStatus

logger = logging.getLogger("nnp.notifications")

PENDING_LIMIT_DEFAULT = 20
PENDING_LIMIT_MAX = 100
OWNER_PATH = "/my"

ContextualValue = User | Iterable[User] | None


# --- Recipients ------------------------------------------------------------------------------


def role_users_q(roles: Iterable[str]) -> Q:
    """``Q`` for users whose *effective* role is in ``roles`` (superusers count as ADMIN)."""
    roles = set(roles)
    condition = Q(pk__in=[])
    plain = roles - {Role.ADMIN}
    if plain:
        condition |= Q(role__in=plain, is_superuser=False)
    if Role.ADMIN in roles:
        condition |= Q(role=Role.ADMIN) | Q(is_superuser=True)
    return condition


def role_users(*roles: str) -> QuerySet[User]:
    """Active users of the given effective roles (e.g. all VETs for an unassigned visit)."""
    return User.objects.filter(role_users_q(roles), is_active=True)


def _reachable() -> QuerySet[User]:
    return User.objects.filter(is_active=True, telegram_id__isnull=False)


def admins() -> QuerySet[User]:
    """Active ADMIN users (superusers included) with a linked Telegram account (Phase 1 helper)."""
    return _reachable().filter(Q(role=Role.ADMIN) | Q(is_superuser=True))


def warehouse_users() -> QuerySet[User]:
    return _reachable().filter(role=Role.WAREHOUSE, is_superuser=False)


def _user_ids(value: ContextualValue) -> list[int]:
    if value is None:
        return []
    if isinstance(value, User):
        return [value.pk]
    return [user.pk for user in value if user is not None]


@dataclass
class EventSettings:
    """Effective switches of one event (registry defaults + ``NotificationSetting`` + system settings)."""

    enabled: bool = True
    telegram_enabled: bool = True
    roles: tuple[str, ...] = ()
    contextual_enabled: bool = True
    overridden: bool = field(default=False)
    # V4 §4
    custom_text: str = ""
    delay_minutes: int = 0
    extra_user_ids: tuple[int, ...] = ()


def effective_settings(event: str) -> EventSettings:
    definition = registry.get(event)
    row = NotificationSetting.objects.filter(event=event).first()
    result = EventSettings(roles=tuple(definition.default_roles))
    if row is not None:
        result.enabled = row.enabled
        result.telegram_enabled = row.telegram_enabled
        result.contextual_enabled = row.contextual_enabled
        result.custom_text = row.custom_text or ""
        result.delay_minutes = row.delay_minutes or 0
        result.extra_user_ids = tuple(pk for pk in (row.extra_user_ids or []) if isinstance(pk, int))
        if row.roles is not None:
            result.roles = tuple(role for role in row.roles if role in NOTIFIABLE_ROLES)
            result.overridden = True
    if definition.low_stock_gate and result.enabled:
        from core.settings_service import get_settings  # local import: core depends on accounts only

        result.enabled = get_settings().low_stock_alert_enabled
    return result


# --- Text helpers ----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def plain_body(text: str) -> str:
    """Telegram HTML → plain text without the title line (``Notification.body``)."""
    plain = html.unescape(_TAG_RE.sub("", text))
    lines = plain.split("\n")
    if lines:
        lines = lines[1:]  # the title line
    return "\n".join(lines).strip()


_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
PLACEHOLDER_NAMES = ("title", "code", "household", "due", "role", "text")


def apply_custom_text(definition: registry.EventDef, text: str, custom: str, values: Mapping[str, object]) -> str:
    """V4 §4: ``custom_text`` replaces the default body (title line kept). Unknown placeholders stay literal.

    Values are HTML-escaped; ``{text}`` is the default body exactly as composed (already Telegram HTML).
    """
    head, _, default_body = text.partition("\n")
    default_body = default_body.strip("\n")

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name == "text":
            return default_body
        if name in values and name in PLACEHOLDER_NAMES:
            return html.escape(str(values[name] if values[name] is not None else ""), quote=False)
        return match.group(0)

    escaped = html.escape(custom, quote=False)
    return f"{head or definition.title_html}\n\n{_PLACEHOLDER_RE.sub(substitute, escaped)}"


# --- Queueing --------------------------------------------------------------------------------


def preferences_for(event: str, user_ids: list[int]) -> dict[int, tuple[bool, bool]]:
    """``{user_id: (enabled, telegram_enabled)}`` of the users that changed this event (one query)."""
    from .models import NotificationPreference

    rows = NotificationPreference.objects.filter(event=event, user_id__in=user_ids).values_list(
        "user_id", "enabled", "telegram_enabled"
    )
    return {user_id: (enabled, telegram) for user_id, enabled, telegram in rows}


#: Contextual recipient → the role such a user has (to list the events a user can receive at all).
CONTEXTUAL_ROLES = {
    registry.HOUSEHOLD_OWNER: {Role.OWNER},
    registry.ASSIGNED_VET: {Role.VET},
    registry.DRIVER_USER: {Role.LOGISTICS},
    registry.RESPONSIBLE: {Role.LOGISTICS},
    registry.OTHER_PARTY: {Role.OWNER, Role.ADMIN},
}


#: Konstruktor events (V4 §3): recipients are configured role references, i.e. any role.
BUILDER_EVENTS = frozenset(
    {"task_assigned", "task_submitted", "task_approved", "task_returned", "process_stage_started"}
)


def events_for_role(role: str) -> list[registry.EventDef]:
    """Events a user of ``role`` may receive (default / Super Admin roles, fallback or contextual)."""
    result = []
    for definition in registry.all_events():
        config = effective_settings(definition.key)
        roles = set(config.roles) | set(definition.fallback_roles)
        for name in definition.contextual:
            roles |= CONTEXTUAL_ROLES.get(name, set())
        if definition.key in BUILDER_EVENTS:  # configurable executors/approvers: any role may receive them
            roles |= set(Role.values)
        if role in roles:
            result.append(definition)
    return result


def update_preference(user: User, event: str, changes: dict) -> "NotificationPreference":  # noqa: F821
    """Create / change the user's switch for ``event`` (audited)."""
    from audit import services as audit
    from audit.models import AuditModule

    from .models import NotificationPreference

    with transaction.atomic():
        row, created = NotificationPreference.objects.select_for_update().get_or_create(user=user, event=event)
        before = audit.snapshot(row)
        for field in ("enabled", "telegram_enabled"):
            if field in changes:
                setattr(row, field, bool(changes[field]))
        row.save()
        if created:
            audit.record_create(AuditModule.NOTIFICATIONS, row, user)
        else:
            audit.record_update(AuditModule.NOTIFICATIONS, row, user, before)
    return row


def notify(
    event: str,
    *,
    key: str | int,
    text: str,
    staff_path: str = "",
    owner_path: str = OWNER_PATH,
    related_op: str = "",
    contextual: Mapping[str, ContextualValue] | None = None,
    contextual_paths: Mapping[str, str] | None = None,
    include_roles: bool = True,
    object_type: str = "",
    object_id: int | None = None,
    placeholders: Mapping[str, object] | None = None,
) -> list[Notification]:
    """Create the rows of one event now (normally called through :func:`notify_on_commit`).

    Idempotent: ``event_key = f"{event}:{key}:{user_id}"`` is unique, a replay creates nothing.
    ``include_roles=False`` sends only to the contextual users (e.g. ``user_created`` for a new owner).
    V4 §4: the Super Admin's ``custom_text`` (``placeholders`` fill ``{title} {code} {household} {due} {role}``),
    ``delay_minutes`` (→ ``send_after``) and ``extra_user_ids`` are applied here.
    """
    definition = registry.get(event)
    config = effective_settings(event)
    if not config.enabled:
        return []

    contextual = contextual or {}
    contextual_ids: dict[int, str] = {}
    if config.contextual_enabled:
        for name in definition.contextual:
            for user_id in _user_ids(contextual.get(name)):
                contextual_ids.setdefault(user_id, name)

    if contextual_ids:
        # Inactive contextual users receive nothing: drop them BEFORE deciding on the fallback, otherwise
        # e.g. driver_assigned with a deactivated driver account would reach nobody (no LOGISTICS fallback).
        active = set(User.objects.filter(pk__in=list(contextual_ids), is_active=True).values_list("pk", flat=True))
        contextual_ids = {pk: name for pk, name in contextual_ids.items() if pk in active}

    roles = list(config.roles) if include_roles else []
    if not contextual_ids and definition.fallback_roles:
        roles.extend(definition.fallback_roles)

    condition = Q(pk__in=list(contextual_ids)) | Q(pk__in=list(config.extra_user_ids), deleted_at__isnull=True)
    if roles:
        condition |= role_users_q(roles)
    recipients = list(User.objects.filter(condition, is_active=True).order_by("id"))
    if not recipients:
        return []
    # Personal preferences (TZ §11.3): muted → no row; Telegram off → inbox only.
    prefs = preferences_for(event, [user.pk for user in recipients])
    recipients = [user for user in recipients if prefs.get(user.pk, (True, True))[0]]
    if not recipients:
        return []

    if config.custom_text.strip():
        values = {"title": definition.display, "code": related_op, "household": "", "due": "", "role": ""}
        values.update(placeholders or {})
        text = apply_custom_text(definition, text, config.custom_text, values)
    send_after = timezone.now() + timedelta(minutes=config.delay_minutes) if config.delay_minutes else None

    keys = {user.pk: f"{event}:{key}:{user.pk}" for user in recipients}
    existing = set(Notification.objects.filter(event_key__in=keys.values()).values_list("event_key", flat=True))
    body = plain_body(text)
    paths = contextual_paths or {}
    rows: list[Notification] = []
    for user in recipients:
        event_key = keys[user.pk]
        if event_key in existing:
            continue
        name = contextual_ids.get(user.pk)
        if name is not None and name in paths:
            path = paths[name]
        elif user.effective_role == Role.OWNER:
            path = owner_path
        else:
            path = staff_path
        telegram = config.telegram_enabled and user.telegram_id is not None and prefs.get(user.pk, (True, True))[1]
        rows.append(
            Notification(
                recipient=user,
                chat_id=user.telegram_id if telegram else None,
                event=event,
                event_key=event_key,
                text=text,
                title=definition.title,
                body=body,
                button_path=path,
                related_op=related_op[:40],
                object_type=object_type,
                object_id=object_id,
                status=NotificationStatus.PENDING if telegram else NotificationStatus.SKIPPED,
                send_after=send_after,
            )
        )
    # ignore_conflicts: a concurrent replay of the same event cannot fail the commit hook.
    Notification.objects.bulk_create(rows, ignore_conflicts=True)
    return rows


def notify_on_commit(event: str, **kwargs) -> None:
    """Schedule :func:`notify` after the current transaction commits (nothing on rollback).

    ``robust=True``: a failure while queuing is logged and never breaks the business operation.
    The event key and the contextual names are validated *now*, so programming errors fail loudly.
    """
    definition = registry.get(event)
    unknown = set(kwargs.get("contextual") or {}) - set(definition.contextual)
    if unknown:
        raise ValueError(f"{event}: unknown contextual recipients {sorted(unknown)}")
    contextual = kwargs.get("contextual")
    if contextual:
        # Freeze lazy querysets/generators at call time (the caller's objects, not later state).
        kwargs["contextual"] = {
            name: [value] if isinstance(value, User) else (list(value) if value is not None else [])
            for name, value in contextual.items()
        }
    transaction.on_commit(partial(notify, event, **kwargs), robust=True)


# --- Bot queue -------------------------------------------------------------------------------


def pending(limit: int = PENDING_LIMIT_DEFAULT) -> QuerySet[Notification]:
    limit = max(1, min(limit, PENDING_LIMIT_MAX))
    return Notification.objects.filter(
        Q(send_after__isnull=True) | Q(send_after__lte=timezone.now()),
        status=NotificationStatus.PENDING,
        attempts__lt=Notification.MAX_ATTEMPTS,
    ).order_by("id")[:limit]


@transaction.atomic
def record_delivery(notification_id: int, *, ok: bool, error: str = "") -> Notification:
    """Store the bot's delivery result. ``Notification.DoesNotExist`` propagates (404)."""
    # ``only()``: the bot keeps working on a database where the V3 columns are not migrated yet.
    notification = (
        Notification.objects.select_for_update()
        .only("id", "status", "attempts", "last_error", "sent_at")
        .get(pk=notification_id)
    )
    if notification.status != NotificationStatus.PENDING:
        return notification
    if ok:
        notification.status = NotificationStatus.SENT
        notification.sent_at = timezone.now()
        notification.last_error = ""
    else:
        notification.attempts += 1
        notification.last_error = error[:2000]
        if notification.attempts >= Notification.MAX_ATTEMPTS:
            notification.status = NotificationStatus.FAILED
            logger.warning("Notification %s failed permanently: %s", notification.pk, error)
    notification.save(update_fields=["status", "sent_at", "attempts", "last_error"])
    return notification


# --- Monitor: retry (API_CONTRACT_V3 §9.5) -----------------------------------------------------

MSG_RETRY_ONLY_FAILED = "Faqat yuborilmagan (FAILED) xabarni qayta yuborish mumkin."
MSG_RETRY_NOT_LINKED = "Foydalanuvchi Telegramga bog‘lanmagan."
RETRY_FAILED_LIMIT = 500


class RetryConflict(Exception):
    """The row is not FAILED (→ 409)."""


class RetryNotLinked(Exception):
    """The recipient is inactive or has no Telegram link (→ 400)."""


def _requeue(row: Notification, telegram_id: int) -> None:
    row.status = NotificationStatus.PENDING
    row.attempts = 0
    row.chat_id = telegram_id
    row.save(update_fields=["status", "attempts", "chat_id"])  # last_error is kept


def retry(notification_id: int, *, actor: User) -> Notification:
    """FAILED → PENDING with the recipient's CURRENT Telegram chat. ``DoesNotExist`` → 404."""
    from audit import services as audit
    from audit.models import AuditAction, AuditModule

    with transaction.atomic():
        row = Notification.objects.select_for_update(of=("self",)).select_related("recipient").get(pk=notification_id)
        if row.status != NotificationStatus.FAILED:
            raise RetryConflict(MSG_RETRY_ONLY_FAILED)
        recipient = row.recipient
        if not recipient.is_active or recipient.telegram_id is None:
            raise RetryNotLinked(MSG_RETRY_NOT_LINKED)
        _requeue(row, recipient.telegram_id)
        audit.record(
            module=AuditModule.NOTIFICATIONS,
            action=AuditAction.RETRY,
            obj=row,
            actor=actor,
            before={"status": NotificationStatus.FAILED},
            after={"status": NotificationStatus.PENDING},
        )
    return row


def retry_failed(*, actor: User, event: str = "", date_from=None, date_to=None) -> dict[str, int]:
    """Retry up to 500 matching FAILED rows of active, linked users → ``{"retried", "skipped"}``."""
    from audit import services as audit
    from audit.models import AuditAction, AuditModule
    from common.periods import day_range

    retried = skipped = 0
    with transaction.atomic():
        qs = Notification.objects.select_for_update(of=("self",)).select_related("recipient").filter(
            status=NotificationStatus.FAILED
        )
        if event:
            qs = qs.filter(event=event)
        start, end = day_range(date_from, date_to)
        if start is not None:
            qs = qs.filter(created_at__gte=start)
        if end is not None:
            qs = qs.filter(created_at__lt=end)
        for row in qs.order_by("id")[:RETRY_FAILED_LIMIT]:
            recipient = row.recipient
            if not recipient.is_active or recipient.telegram_id is None:
                skipped += 1
                continue
            _requeue(row, recipient.telegram_id)
            audit.record(
                module=AuditModule.NOTIFICATIONS,
                action=AuditAction.RETRY,
                obj=row,
                actor=actor,
                before={"status": NotificationStatus.FAILED},
                after={"status": NotificationStatus.PENDING},
            )
            retried += 1
    return {"retried": retried, "skipped": skipped}


# --- Notification settings (API_CONTRACT_V3 §9.3) --------------------------------------------

SETTING_FIELDS = (
    "enabled", "telegram_enabled", "roles", "contextual_enabled", "custom_text", "delay_minutes", "extra_user_ids",
)
#: ``ConfigVersion.object_type`` of notification settings (V4 §4).
CONFIG_OBJECT_TYPE = "notification_setting"


def setting_state(row: NotificationSetting) -> dict:
    """Full editable state of a setting (the ``ConfigVersion.data`` snapshot)."""
    return {name: getattr(row, name) for name in SETTING_FIELDS}


def settings_rows() -> list[tuple[registry.EventDef, NotificationSetting | None]]:
    """Every registry event (registry order) with its stored row (one query)."""
    rows = {row.event: row for row in NotificationSetting.objects.select_related("updated_by")}
    return [(definition, rows.get(key)) for key, definition in registry.EVENTS.items()]


def update_setting(event: str, changes: dict, *, actor: User, comment: str = "") -> NotificationSetting:
    """Super Admin switch change (``KeyError`` for an unknown event → 404). Audit ``notifications / update``.

    Every call writes a ``builder.ConfigVersion`` (V4 §4) with the full state after the change.
    """
    from builder.services import write_config_version
    from audit import services as audit
    from audit.models import AuditAction, AuditModule

    registry.get(event)
    unknown = set(changes) - set(SETTING_FIELDS)
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    with transaction.atomic():
        row, _ = NotificationSetting.objects.select_for_update().get_or_create(event=event)
        before = audit.snapshot(row)
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_by = actor
        row.save()
        old, new = audit.diff(before, audit.snapshot(row))
        if new:
            audit.record(
                module=AuditModule.NOTIFICATIONS,
                action=AuditAction.UPDATE,
                obj=row,
                actor=actor,
                before=old,
                after=new,
            )
        write_config_version(
            CONFIG_OBJECT_TYPE, row.pk, name=event, data=setting_state(row), actor=actor, comment=comment
        )
    return row


def setting_versions(event: str):
    """``ConfigVersion`` rows of ``event`` newest first (empty when the setting was never changed)."""
    from builder.models import ConfigVersion

    row = NotificationSetting.objects.filter(event=event).first()
    if row is None:
        return ConfigVersion.objects.none()
    return ConfigVersion.objects.filter(object_type=CONFIG_OBJECT_TYPE, object_id=row.pk).select_related(
        "created_by"
    ).order_by("-version")


def revert_setting(event: str, version: int, *, actor: User) -> NotificationSetting:
    """Apply the state of ``version`` again (a new version is written). ``LookupError`` → 400."""
    snapshot = setting_versions(event).filter(version=version).first()
    if snapshot is None:
        raise LookupError(version)
    changes = {name: snapshot.data[name] for name in SETTING_FIELDS if name in snapshot.data}
    return update_setting(event, changes, actor=actor, comment=f"{version}-versiyaga qaytarildi")


# --- My notifications / bell (API_CONTRACT_V3 §9.6) ------------------------------------------


def inbox(user: User) -> QuerySet[Notification]:
    return Notification.objects.filter(recipient=user).order_by("-id")


def unread_count(user: User) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_read(user: User, notification_id: int) -> Notification:
    """Own row only (``DoesNotExist`` → 404). Reading never changes the Telegram delivery status."""
    row = Notification.objects.get(pk=notification_id, recipient=user)
    if row.read_at is None:
        row.read_at = timezone.now()
        Notification.objects.filter(pk=row.pk, read_at__isnull=True).update(read_at=row.read_at)
    return row


def mark_all_read(user: User) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())
