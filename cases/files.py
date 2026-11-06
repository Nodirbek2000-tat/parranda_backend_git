"""Private files: upload a photo/document (unattached), then stream it only to users who may see the case."""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated, PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Document, Photo
from .serializers import FILE_SIGNER, FILE_URL_MAX_AGE, file_url
from .services import visible_cases

DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".txt"}
MODELS = {"photo": Photo, "document": Document}


def _check_size(upload):
    if upload.size > settings.FILE_MAX_BYTES:
        raise serializers.ValidationError("Fayl hajmi 10 MB dan oshmasligi kerak.")
    return upload


class PhotoUploadSerializer(serializers.Serializer):
    image = serializers.ImageField(validators=[_check_size])
    taken_at = serializers.DateTimeField(required=False)
    lat = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    lng = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    live = serializers.BooleanField(required=False, default=False)


class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField(validators=[_check_size])

    def validate_file(self, upload):
        if Path(upload.name).suffix.lower() not in DOCUMENT_EXTENSIONS:
            raise serializers.ValidationError("Bu turdagi faylni yuklab bo‘lmaydi.")
        return upload


def _r6(value):
    return None if value is None else round(value, 6)


class PhotoUploadView(APIView):
    def post(self, request):
        ser = PhotoUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        photo = Photo.objects.create(
            image=d["image"], taken_at=d.get("taken_at") or timezone.now(), lat=_r6(d.get("lat")),
            lng=_r6(d.get("lng")), live=d["live"], uploaded_by=request.user)
        return Response({"id": photo.pk, "url": file_url("photo", photo.pk)}, status=201)


class DocumentUploadView(APIView):
    def post(self, request):
        ser = DocumentUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        upload = ser.validated_data["file"]
        doc = Document.objects.create(file=upload, original_name=Path(upload.name).name[:255],
                                      uploaded_by=request.user)
        return Response({"id": doc.pk, "url": file_url("document", doc.pk), "original_name": doc.original_name},
                        status=201)


def can_read_file(user, obj) -> bool:
    if obj.case_id is None:
        return obj.uploaded_by_id == user.pk or user.is_admin
    return visible_cases(user).filter(pk=obj.case_id).exists()


class FileView(APIView):
    """GET /api/files/{kind}/{id}: Bearer token with access to the case, or a valid `sig` from the API."""

    permission_classes = [AllowAny]

    def get(self, request, kind, pk):
        model = MODELS.get(kind)
        if model is None:
            raise Http404
        obj = model.objects.filter(pk=pk).first()
        if obj is None:
            raise Http404
        if not self._signed(request, kind, pk):
            user = request.user
            if not user.is_authenticated:
                raise NotAuthenticated()
            if not can_read_file(user, obj):
                raise PermissionDenied()
        field = obj.image if kind == "photo" else obj.file
        try:
            handle = field.open("rb")
        except FileNotFoundError:
            raise Http404 from None
        content_type = mimetypes.guess_type(field.name)[0] or "application/octet-stream"
        if kind == "document":
            return FileResponse(handle, content_type=content_type, as_attachment=True, filename=obj.original_name)
        response = FileResponse(handle, content_type=content_type)
        response["Cache-Control"] = "private, max-age=3600"
        return response

    def _signed(self, request, kind, pk) -> bool:
        sig = request.query_params.get("sig")
        if not sig:
            return False
        try:
            return FILE_SIGNER.unsign(sig, max_age=FILE_URL_MAX_AGE) == f"{kind}-{pk}"
        except signing.BadSignature:
            raise AuthenticationFailed("Havola muddati tugagan. Sahifani yangilang.") from None
