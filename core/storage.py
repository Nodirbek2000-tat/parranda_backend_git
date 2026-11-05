"""Private file storage (API_CONTRACT_V3 §4.5).

``FILE_STORAGE=local`` (default): ``MEDIA_ROOT`` on disk, no public URL — files are streamed only by
``GET /api/files/{id}``. ``FILE_STORAGE=s3``: ``storages.backends.s3.S3Storage`` (optional dependency
``django-storages`` + ``boto3``), private ACL, no query-string auth.
"""

from __future__ import annotations

from functools import cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage, Storage


@cache
def _build() -> Storage:
    kind = getattr(settings, "FILE_STORAGE", "local")
    if kind == "s3":
        try:
            from storages.backends.s3 import S3Storage  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise ImproperlyConfigured(
                "FILE_STORAGE=s3 uchun `pip install django-storages boto3` kerak (requirements.txt izohiga qarang)."
            ) from exc
        return S3Storage(
            bucket_name=settings.S3_BUCKET,
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            access_key=settings.S3_ACCESS_KEY_ID,
            secret_key=settings.S3_SECRET_ACCESS_KEY,
            region_name=settings.S3_REGION or None,
            default_acl="private",
            querystring_auth=False,
            file_overwrite=False,
        )
    if kind != "local":
        raise ImproperlyConfigured("FILE_STORAGE faqat 'local' yoki 's3' bo‘lishi mumkin.")
    # No explicit location: FileSystemStorage reads settings.MEDIA_ROOT lazily (override_settings works).
    # There is no MEDIA_URL route, so the storage never yields a reachable public URL.
    return FileSystemStorage()


def private_storage() -> Storage:
    """Callable used by ``Attachment.file`` (evaluated lazily, so tests can override ``MEDIA_ROOT``)."""
    return _build()


def reset_storage_cache() -> None:
    """Tests that override ``MEDIA_ROOT`` / ``FILE_STORAGE`` call this first."""
    _build.cache_clear()
