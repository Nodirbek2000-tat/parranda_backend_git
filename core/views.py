"""Company settings, files and global search (API_CONTRACT_V3 §3, §4, §12)."""

from __future__ import annotations

from urllib.parse import quote

from django.http import FileResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.permissions import IsAuthenticatedRole, IsSuperAdmin
from common.schema import ERROR_400, ERROR_403, ERROR_404

from . import files, search, settings_service
from .models import Attachment, AttachmentPurpose
from .serializers import AttachmentSerializer, SystemSettingsSerializer, SystemSettingsUpdateSerializer

MSG_FILE_NOT_FOUND = "Fayl topilmadi."

ID_PATH = OpenApiParameter("pk", int, OpenApiParameter.PATH)


class SettingsView(APIView):
    """``GET`` any role, ``PATCH`` Super Admin only (plain ADMIN → 403 “Bu amal faqat Super Admin uchun.”)."""

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsAuthenticatedRole()]
        return [IsAuthenticatedRole(), IsSuperAdmin()]

    @extend_schema(responses=SystemSettingsSerializer)
    def get(self, request: Request) -> Response:
        return Response(SystemSettingsSerializer(settings_service.get_settings()).data)

    @extend_schema(
        request=SystemSettingsUpdateSerializer,
        responses={200: SystemSettingsSerializer, 400: ERROR_400, 403: ERROR_403},
    )
    def patch(self, request: Request) -> Response:
        serializer = SystemSettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = settings_service.update_settings(dict(serializer.validated_data), actor=request.user)
        return Response(SystemSettingsSerializer(row).data)


FileUploadRequest = inline_serializer(
    "FileUpload",
    fields={
        "file": serializers.FileField(),
        "purpose": serializers.ChoiceField(choices=AttachmentPurpose.choices),
    },
)


class FileUploadView(files.UploadSizeLimitMixin, APIView):
    permission_classes = [IsAuthenticatedRole]
    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "uploads"

    @extend_schema(
        request={"multipart/form-data": FileUploadRequest},
        responses={201: AttachmentSerializer, 400: ERROR_400, 403: ERROR_403},
    )
    def post(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        attachment = files.upload(actor=request.user, file=upload, purpose=request.data.get("purpose"))
        return Response(AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)


def _visible_attachment(request: Request, pk: int) -> Attachment:
    attachment = Attachment.objects.filter(pk=pk).first()
    if attachment is None or not files.can_view(request.user, attachment):
        raise NotFound(MSG_FILE_NOT_FOUND)
    return attachment


def content_disposition(name: str, *, attachment: bool = False) -> str:
    """``inline; filename="<ascii>"; filename*=UTF-8''<urlencoded original>``."""
    ascii_name = name.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "").strip() or "fayl"
    kind = "attachment" if attachment else "inline"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


class FileDownloadView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(
        parameters=[ID_PATH],
        responses={(200, "application/octet-stream"): OpenApiTypes.BINARY, 404: ERROR_404},
    )
    def get(self, request: Request, pk: int):
        attachment = _visible_attachment(request, pk)
        try:
            handle = attachment.file.open("rb")
        except (FileNotFoundError, OSError):
            raise NotFound(MSG_FILE_NOT_FOUND) from None
        response = FileResponse(handle, content_type=attachment.content_type)
        response["Content-Length"] = str(attachment.size)
        response["Content-Disposition"] = content_disposition(attachment.original_name)
        response["X-Content-Type-Options"] = "nosniff"
        # Not cached (deviation from V3 §4.4 "max-age=300"): the browser cache is keyed by URL only, so on a
        # shared device the next user would get the previous user's file without an access check.
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Authorization"
        response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
        return response


class FileMetaView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=[ID_PATH], responses={200: AttachmentSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return Response(AttachmentSerializer(_visible_attachment(request, pk)).data)


SearchItem = inline_serializer(
    "SearchItem",
    fields={
        "id": serializers.IntegerField(),
        "title": serializers.CharField(),
        "subtitle": serializers.CharField(),
        "path": serializers.CharField(),
    },
)
SearchResult = inline_serializer(
    "SearchResult",
    fields={
        "q": serializers.CharField(),
        "groups": inline_serializer(
            "SearchGroup",
            fields={"type": serializers.CharField(), "label": serializers.CharField(), "items": SearchItem},
            many=True,
        ),
    },
)


class SearchView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(
        parameters=[OpenApiParameter("q", str, OpenApiParameter.QUERY, required=True, description="2–100 belgi")],
        responses={200: SearchResult, 400: OpenApiResponse(description="q juda qisqa")},
    )
    def get(self, request: Request) -> Response:
        q = (request.query_params.get("q") or "").strip()
        if len(q) < search.MIN_Q:
            raise ValidationError({"q": ["Kamida 2 ta belgi kiriting."]})
        if len(q) > search.MAX_Q:
            raise ValidationError({"q": ["Ko‘pi bilan 100 ta belgi kiriting."]})
        return Response(search.search(request.user, q))
