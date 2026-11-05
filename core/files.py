"""Uploads, binding and visibility of private files (API_CONTRACT_V3 §4).

Module API (everything a V3 module needs)::

    from core import files
    from core.models import AttachmentPurpose

    # serializer: file_ids = files.FileIdsField()
    # service, inside transaction.atomic() after the business row is saved and locked:
    files.bind_files(file_ids, actor=actor, purpose=AttachmentPurpose.DELIVERY_PROOF, obj=task)
    # response: files.files_for(task) / files.files_by_object("LogisticsTask", [ids])  -> AttachmentSerializer

Visibility (§4.4): uploader; ADMIN; then by the bound object's type: ``OBJECT_VIEW_ROLES`` + the owner of the
copied ``household``. A module with special rules calls :func:`register_visibility`.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.db import models, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.permissions import MSG_NO_PERMISSION
from common.roles import Role, role_of

from .models import Attachment, AttachmentPurpose

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
#: Whole multipart body: the file plus form fields and boundaries. Larger bodies are refused from the
#: ``Content-Length`` header, before Django spools anything to disk.
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
MAX_FILES_PER_OBJECT = 10
BIND_WINDOW = timedelta(hours=24)
#: Unbound uploads one user may hold at a time (storage cannot be filled with never-bound files).
MAX_UNBOUND_FILES_PER_USER = 20
MAX_UNBOUND_BYTES_PER_USER = 100 * 1024 * 1024

MSG_NO_FILE = "Fayl tanlanmagan."
MSG_TOO_BIG = "Fayl hajmi 10 MB dan oshmasligi kerak."
MSG_BAD_TYPE = "Faqat JPG, PNG, WEBP yoki PDF fayl yuklash mumkin."
MSG_BIND = "Fayl topilmadi yoki allaqachon biriktirilgan."
MSG_TOO_MANY = f"Ko‘pi bilan {MAX_FILES_PER_OBJECT} ta fayl biriktirish mumkin."
MSG_IMAGE_ONLY = "Bu yerga faqat JPG, PNG yoki WEBP rasm yuklash mumkin."
MSG_UNBOUND_QUOTA = (
    "Biriktirilmagan fayllaringiz juda ko‘p. Yuklangan fayllar bilan amalni yakunlang yoki 24 soatdan keyin "
    "qayta urinib ko‘ring."
)

#: Allowed extensions → the content type their magic bytes must have.
EXTENSION_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

#: Who may upload a file with this purpose (§4.2).
PURPOSE_ROLES: dict[str, frozenset[str]] = {
    AttachmentPurpose.DELIVERY_PROOF: frozenset({Role.ADMIN, Role.LOGISTICS}),
    AttachmentPurpose.VET_VISIT: frozenset({Role.ADMIN, Role.VET}),
    AttachmentPurpose.VACCINATION: frozenset({Role.ADMIN, Role.VET}),
    AttachmentPurpose.DISEASE: frozenset({Role.ADMIN, Role.VET}),
    AttachmentPurpose.TREATMENT: frozenset({Role.ADMIN, Role.VET}),
    AttachmentPurpose.VET_REQUEST: frozenset({Role.OWNER}),
    AttachmentPurpose.STOCK_DOCUMENT: frozenset({Role.ADMIN, Role.WAREHOUSE}),
}

#: Staff roles that see files bound to an object of this type (§4.4); the household owner sees them too.
OBJECT_VIEW_ROLES: dict[str, frozenset[str]] = {
    "LogisticsTask": frozenset({Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS}),
    "VetVisit": frozenset({Role.ADMIN, Role.VET}),
    "Vaccination": frozenset({Role.ADMIN, Role.VET}),
    "DiseaseRecord": frozenset({Role.ADMIN, Role.VET}),
    "Treatment": frozenset({Role.ADMIN, Role.VET}),
    "VetRequest": frozenset({Role.ADMIN, Role.VET}),
    "StockTransaction": frozenset({Role.ADMIN, Role.WAREHOUSE}),
}

IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
#: Purposes limited to some content types. Owners (untrusted uploaders) send photos only — no PDF
#: (active content) reaches vets/admins from them; staff purposes keep PDF (signatures, vet documents).
PURPOSE_TYPES: dict[str, frozenset[str]] = {
    AttachmentPurpose.VET_REQUEST: IMAGE_TYPES,
}

VisibilityRule = Callable[[User, Attachment], bool]
_VISIBILITY: dict[str, VisibilityRule] = {}


def register_visibility(object_type: str, rule: VisibilityRule) -> None:
    """Replace the default role/household rule for files bound to ``object_type`` (call from ``AppConfig.ready``)."""
    _VISIBILITY[object_type] = rule


# --- Validation ------------------------------------------------------------------------------


def sniff_content_type(head: bytes) -> str | None:
    """Content type from magic bytes: JPEG, PNG, WEBP or PDF; ``None`` for anything else."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]+")


def sanitize_name(name: str) -> str:
    """Display name only: path separators and control characters removed, ≤ 200 chars."""
    name = unicodedata.normalize("NFC", name or "")
    name = _UNSAFE_NAME.sub("", name).strip().strip(".") or "fayl"
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem[: 200 - len(ext) - 1]}.{ext}" if dot and len(ext) <= 10 else name[:200]
    return name


def extension_of(name: str) -> str:
    return f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""


def validate_upload(file: UploadedFile | None) -> str:
    """Checks of §4.2 in contract order; returns the sniffed content type."""
    if file is None:
        raise ValidationError({"file": [MSG_NO_FILE]})
    if file.size is None or file.size > MAX_UPLOAD_BYTES:
        raise ValidationError({"file": [MSG_TOO_BIG]})
    expected = EXTENSION_TYPES.get(extension_of(file.name or ""))
    file.seek(0)
    head = file.read(16)
    file.seek(0)
    if expected is None or sniff_content_type(head) != expected:
        raise ValidationError({"file": [MSG_BAD_TYPE]})
    return expected


def check_purpose(purpose: str | None, actor: User) -> str:
    if not purpose:
        raise ValidationError({"purpose": ["Bu maydon to‘ldirilishi shart."]})
    if purpose not in AttachmentPurpose.values:
        raise ValidationError({"purpose": [f"“{purpose}” ruxsat etilgan qiymat emas."]})
    if role_of(actor) not in PURPOSE_ROLES[purpose]:
        raise PermissionDenied(MSG_NO_PERMISSION)
    return purpose


def reject_oversized_body(request: Any) -> None:
    """``400 {"file": [MSG_TOO_BIG]}`` from ``Content-Length`` alone, before the multipart body is parsed."""
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length > MAX_REQUEST_BYTES:
        raise ValidationError({"file": [MSG_TOO_BIG]})


class UploadSizeLimitMixin:
    """For upload views: refuse an oversized body before throttles/parsers touch ``request.data``."""

    def initial(self, request: Any, *args: Any, **kwargs: Any) -> None:
        reject_oversized_body(request)
        super().initial(request, *args, **kwargs)  # type: ignore[misc]


def _check_unbound_quota(actor: User, size: int) -> None:
    """Drop the actor's own expired unbound uploads, then enforce the per-user unbound quota."""
    cleanup_unbound(uploaded_by=actor)
    stats = Attachment.objects.filter(uploaded_by=actor, object_id__isnull=True).aggregate(
        count=models.Count("id"), total=models.Sum("size")
    )
    if stats["count"] >= MAX_UNBOUND_FILES_PER_USER or (stats["total"] or 0) + size > MAX_UNBOUND_BYTES_PER_USER:
        raise ValidationError({"file": [MSG_UNBOUND_QUOTA]})


# --- Upload ----------------------------------------------------------------------------------


def upload(*, actor: User, file: UploadedFile | None, purpose: str | None) -> Attachment:
    """Validate and store an unbound file (``files / upload`` audit row)."""
    content_type = validate_upload(file)
    purpose = check_purpose(purpose, actor)
    allowed = PURPOSE_TYPES.get(purpose)
    if allowed is not None and content_type not in allowed:
        raise ValidationError({"file": [MSG_IMAGE_ONLY]})
    _check_unbound_quota(actor, file.size)
    digest = hashlib.sha256()
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    original = sanitize_name(file.name)
    ext = extension_of(original) or ".bin"
    with transaction.atomic():
        attachment = Attachment(
            original_name=original,
            content_type=content_type,
            size=file.size,
            sha256=digest.hexdigest(),
            purpose=purpose,
            uploaded_by=actor,
        )
        attachment.file.save(f"upload{ext}", file, save=False)
        try:
            attachment.save()
            audit.record(
                module=AuditModule.FILES,
                action=AuditAction.UPLOAD,
                obj=attachment,
                actor=actor,
                after=audit.snapshot(attachment),
            )
        except Exception:
            attachment.file.delete(save=False)
            raise
    return attachment


# --- Binding ---------------------------------------------------------------------------------


class FileIdsField(serializers.ListField):
    """``"file_ids": [41, 42]`` (0–10 ids) for write serializers."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("child", serializers.IntegerField(min_value=1, max_value=2**63 - 1))
        kwargs.setdefault("required", False)
        kwargs.setdefault("default", list)
        kwargs.setdefault("max_length", MAX_FILES_PER_OBJECT)
        kwargs.setdefault("error_messages", {"max_length": MSG_TOO_MANY})
        super().__init__(**kwargs)


def object_type_of(obj: models.Model) -> str:
    return type(obj).__name__


def bind_files(
    file_ids: Iterable[int],
    *,
    actor: User,
    purpose: str | Iterable[str],
    obj: models.Model,
    household: Any = None,
) -> list[Attachment]:
    """Attach unbound uploads of ``actor`` to ``obj`` exactly once. Call inside the business transaction.

    ``household`` defaults to ``obj.household`` (owner visibility). Raises the §4.3 ``400`` otherwise.
    """
    ids = sorted({int(pk) for pk in file_ids})
    if not ids:
        return []
    if len(ids) > MAX_FILES_PER_OBJECT:
        raise ValidationError({"file_ids": [MSG_TOO_MANY]})
    purposes = {purpose} if isinstance(purpose, str) else set(purpose)
    if household is None:
        household = getattr(obj, "household", None)
    since = timezone.now() - BIND_WINDOW
    rows = list(Attachment.objects.select_for_update().filter(id__in=ids).order_by("id"))
    if len(rows) != len(ids) or any(
        row.uploaded_by_id != actor.pk
        or row.is_bound
        or row.purpose not in purposes
        or row.created_at < since
        for row in rows
    ):
        raise ValidationError({"file_ids": [MSG_BIND]})
    now = timezone.now()
    object_type = object_type_of(obj)
    Attachment.objects.filter(id__in=ids).update(
        object_type=object_type, object_id=obj.pk, household=household, bound_at=now
    )
    for row in rows:
        row.object_type, row.object_id, row.household, row.bound_at = object_type, obj.pk, household, now
    return rows


def files_by_object(object_type: str, object_ids: Iterable[int]) -> dict[int, list[Attachment]]:
    """One query: ``{object_id: [Attachment, ...]}`` for list/detail serializers."""
    result: dict[int, list[Attachment]] = defaultdict(list)
    ids = list(object_ids)
    if not ids:
        return result
    for row in Attachment.objects.filter(object_type=object_type, object_id__in=ids).order_by("id"):
        result[row.object_id].append(row)
    return result


def files_for(obj: models.Model) -> list[Attachment]:
    return files_by_object(object_type_of(obj), [obj.pk]).get(obj.pk, [])


# --- Visibility ------------------------------------------------------------------------------


def can_view(user: User, attachment: Attachment) -> bool:
    if attachment.uploaded_by_id == user.pk:
        return True
    role = role_of(user)
    if role == Role.ADMIN:
        return True
    if not attachment.is_bound:
        return False
    rule = _VISIBILITY.get(attachment.object_type or "")
    if rule is not None:
        return rule(user, attachment)
    if role in OBJECT_VIEW_ROLES.get(attachment.object_type or "", frozenset()):
        return True
    if role == Role.OWNER and attachment.household_id is not None:
        return attachment.household_id == user.household_id_or_none
    return False


def cleanup_unbound(older_than: timedelta = BIND_WINDOW, *, uploaded_by: User | None = None) -> int:
    """Delete unbound uploads older than ``older_than`` (row + stored file). Returns the count."""
    cutoff = timezone.now() - older_than
    count = 0
    rows = Attachment.objects.filter(object_id__isnull=True, created_at__lt=cutoff)
    if uploaded_by is not None:
        rows = rows.filter(uploaded_by=uploaded_by)
    for row in rows.order_by("id"):
        name = row.file.name
        storage = row.file.storage
        with transaction.atomic():
            row.delete()
        if name:
            storage.delete(name)
        count += 1
    return count
