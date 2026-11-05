"""«Barcha ma’lumotlarni o‘chirish» — Super Admin wipes every business record except his own account.

Kept: the acting Super Admin, the audit journal (immutable by design; the wipe itself is recorded there),
company settings (``SystemSettings``) and notification rules (``NotificationSetting``).
Everything else — other users (other Super Admins too), households, batches, warehouse, orders, logistics,
vet, notifications, Konstruktor objects, acts, uploaded-file records — is deleted.

Before deleting, a full JSON snapshot is written to ``MEDIA_ROOT/system-backups/`` (private storage, persistent
Docker volume) so a mistaken wipe can be undone with::

    python manage.py loaddata /app/media/system-backups/<file>.json.gz
"""

from __future__ import annotations

import gzip
import io
import logging
from collections import Counter
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.management import call_command
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from accounts.models import User

logger = logging.getLogger("nnp.wipe")

#: Text the client must send back (third confirmation).
CONFIRM_TEXT = "HAMMASINI O‘CHIRISH"

#: Apps whose records are wiped (Django internals such as contenttypes/permissions/migrations are untouched).
WIPE_APPS = (
    "accounts", "farm", "warehouse", "notifications", "dashboard", "stats", "core", "orders", "logistics",
    "vet", "reports", "builder", "acts", "token_blacklist", "sessions", "admin",
)
#: ``app_label.ModelName`` kept as they are.
KEEP_MODELS = frozenset({"audit.AuditLog", "core.SystemSettings", "notifications.NotificationSetting"})
#: Apps in the safety snapshot (everything that is wiped + users).
SNAPSHOT_APPS = ("accounts", "farm", "warehouse", "notifications", "core", "orders", "logistics", "vet",
                 "reports", "builder", "acts")

MSG_PASSWORD = "Parol noto‘g‘ri."
MSG_CONFIRM = f"Tasdiqlash uchun «{CONFIRM_TEXT}» deb yozing."


class WipeError(Exception):
    pass


def backup_dir() -> Path:
    return Path(settings.MEDIA_ROOT) / "system-backups"


def snapshot() -> Path:
    """JSON snapshot of all data that is about to be wiped (gzip; restorable with ``loaddata``)."""
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"nnp-before-wipe-{timezone.localtime():%Y%m%d-%H%M%S}.json.gz"
    buffer = io.StringIO()
    call_command("dumpdata", *SNAPSHOT_APPS, stdout=buffer, indent=None, verbosity=0, use_base_manager=True)
    # Written by hand: always UTF-8 (``dumpdata --output`` uses the platform encoding).
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        handle.write(buffer.getvalue())
    return target


def _wipe_models():
    for app_label in WIPE_APPS:
        try:
            config = apps.get_app_config(app_label)
        except LookupError:
            continue
        for model in config.get_models():
            label = f"{model._meta.app_label}.{model.__name__}"
            if label in KEEP_MODELS or model is User:
                continue
            yield label, model


def wipe(*, actor: User, password: str, confirm: str) -> dict:
    """Delete everything except ``actor``; returns ``{"deleted": {label: n}, "backup": "<file name>"}``."""
    if not (actor.is_active and actor.is_superuser):
        raise PermissionError
    if not check_password(password or "", actor.password):
        raise WipeError(MSG_PASSWORD)
    if (confirm or "").strip() != CONFIRM_TEXT:
        raise WipeError(MSG_CONFIRM)

    backup_file = snapshot()
    deleted: Counter[str] = Counter()
    with transaction.atomic():
        pending = list(_wipe_models())
        # PROTECT foreign keys decide the order: retry until every table is empty (bounded passes).
        for _ in range(len(pending) + 1):
            blocked = []
            for label, model in pending:
                try:
                    # Base manager: soft-deleted rows (households, products…) are wiped as well.
                    _, per_model = model._base_manager.all().delete()
                except ProtectedError:
                    blocked.append((label, model))
                    continue
                deleted.update({key: value for key, value in per_model.items() if value})
            pending = blocked
            if not pending:
                break
        if pending:
            raise WipeError("Ba’zi ma’lumotlarni o‘chirib bo‘lmadi: " + ", ".join(label for label, _ in pending))

        # Kept configuration rows must not point at users that are about to disappear.
        from core.models import SystemSettings
        from notifications.models import NotificationSetting

        SystemSettings.objects.exclude(updated_by=None).update(updated_by=actor)
        NotificationSetting.objects.exclude(updated_by=None).update(updated_by=actor)
        NotificationSetting.objects.update(extra_user_ids=[])

        _, per_model = User._base_manager.exclude(pk=actor.pk).delete()
        deleted.update({key: value for key, value in per_model.items() if value})

        from audit.models import AuditAction, AuditModule
        from audit.services import record

        record(module=AuditModule.SETTINGS, action=AuditAction.DELETE, actor=actor, object_type="SystemSettings",
               after={"wipe": True, "backup": backup_file.name, "deleted": dict(deleted)})
    logger.warning("Barcha ma’lumotlar o‘chirildi: actor=%s backup=%s", actor.pk, backup_file.name)
    return {"deleted": dict(sorted(deleted.items())), "backup": backup_file.name}
