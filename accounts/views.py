from __future__ import annotations

from django.conf import settings
from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from common import query
from common.exceptions import UnauthorizedError
from common.ids import ID_REGEX
from common.permissions import IsAdminRole, IsAuthenticatedRole
from common.roles import Role
from common.schema import ERROR_400, ERROR_401, ERROR_403, DetailSerializer, query as query_param

from . import services
from .models import User
from .serializers import (
    ChangePasswordSerializer,
    EndSessionsSerializer,
    LoginSerializer,
    LogoutSerializer,
    RefreshSerializer,
    TelegramAuthSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
    user_queryset,
)
from .telegram import InitDataInvalid, parse_init_data
from .throttles import UsernameLoginHourThrottle, UsernameLoginMinuteThrottle

MSG_TELEGRAM_INVALID = "Telegram ma’lumotlari yaroqsiz yoki eskirgan. Ilovani qayta oching."
MSG_TELEGRAM_NOT_LINKED = "Telegram akkauntingiz tizimga bog‘lanmagan. Botda /start bosing."
MSG_TELEGRAM_REVOKED = "Sessiya muddati tugagan. Ilovani yopib, qayta oching."

AuthResponseSerializer = inline_serializer(
    "AuthTokens",
    fields={"access": serializers.CharField(), "refresh": serializers.CharField(), "user": UserSerializer()},
)
TempPasswordSerializer = inline_serializer("TempPassword", fields={"temp_password": serializers.CharField()})
UserCreatedSerializer = inline_serializer(
    "UserCreated", fields={"user": UserSerializer(), "temp_password": serializers.CharField()}
)


def _auth_response(user: User, method: str) -> Response:
    tokens = services.issue_tokens(user)
    services.record_login_success(user, method)
    return Response({**tokens, "user": UserSerializer(user).data})


class PublicAuthView(APIView):
    """Unauthenticated endpoint throttled with the ``login`` scope (10/min per client IP)."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class LoginView(PublicAuthView):
    # Per-IP limit plus per-username limits: a rotating or spoofed client IP cannot
    # multiply the number of guesses against one account.
    throttle_classes = [*PublicAuthView.throttle_classes, UsernameLoginMinuteThrottle, UsernameLoginHourThrottle]

    @extend_schema(
        auth=[],
        request=LoginSerializer,
        responses={200: AuthResponseSerializer, 400: ERROR_400, 401: ERROR_401, 403: ERROR_403},
    )
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = services.LOGIN_PASSWORD
        try:
            user = services.verify_credentials(**serializer.validated_data)
        except services.CredentialsInvalid as exc:
            services.record_login_failure(
                method=method,
                reason="bad_credentials",
                user=exc.user,
                submitted_username=serializer.validated_data["username"],
            )
            raise UnauthorizedError(services.MSG_BAD_CREDENTIALS) from None
        except services.AccountBlocked as exc:
            services.record_login_failure(method=method, reason="blocked", user=exc.user)
            raise PermissionDenied(services.MSG_BLOCKED) from None
        return _auth_response(user, method)


class TelegramAuthView(PublicAuthView):
    @extend_schema(
        auth=[],
        request=TelegramAuthSerializer,
        responses={200: AuthResponseSerializer, 400: ERROR_400, 401: ERROR_401, 403: ERROR_403},
    )
    def post(self, request: Request) -> Response:
        serializer = TelegramAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = services.LOGIN_TELEGRAM
        try:
            init_data = parse_init_data(serializer.validated_data["init_data"], settings.BOT_TOKEN)
        except InitDataInvalid:
            services.record_login_failure(method=method, reason="telegram_invalid")
            raise UnauthorizedError(MSG_TELEGRAM_INVALID) from None
        user = User.objects.select_related("household").filter(telegram_id=init_data.user_id).first()
        if user is None:
            services.record_login_failure(method=method, reason="telegram_not_linked")
            raise PermissionDenied(MSG_TELEGRAM_NOT_LINKED)
        if not user.is_active:
            services.record_login_failure(method=method, reason="blocked", user=user)
            raise PermissionDenied(services.MSG_BLOCKED)
        revoked = user.sessions_revoked_at
        if revoked is not None and init_data.auth_date <= int(revoked.timestamp()):
            # initData signed before "end sessions" / "logout everywhere": the cut-off Web App
            # (or a captured initData string) must not log straight back in.
            services.record_login_failure(method=method, reason="session_revoked", user=user)
            raise UnauthorizedError(MSG_TELEGRAM_REVOKED)
        return _auth_response(user, method)


class RefreshView(TokenRefreshView):
    """``{refresh}`` → ``{access}`` (refresh tokens are not rotated)."""

    serializer_class = RefreshSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses=UserSerializer)
    def get(self, request: Request) -> Response:
        user = user_queryset().get(pk=request.user.pk)
        return Response(UserSerializer(user).data)


class LogoutView(APIView):
    """``{refresh, all}``: blacklist one refresh token, or ``all=true`` ends every session (V3 §2.3)."""

    permission_classes = [IsAuthenticatedRole]

    @extend_schema(request=LogoutSerializer, responses={200: DetailSerializer, 400: ERROR_400})
    def post(self, request: Request) -> Response:
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            services.logout(request.user, refresh=data["refresh"] or None, all_sessions=data["all"])
        except services.RefreshTokenInvalid:
            raise ValidationError({"refresh": [services.MSG_TOKEN_INVALID]}) from None
        return Response({"detail": "Tizimdan chiqildi"})


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(request=ChangePasswordSerializer, responses={200: DetailSerializer, 400: ERROR_400})
    def post(self, request: Request) -> Response:
        serializer = ChangePasswordSerializer(data=request.data, context={"user": request.user})
        serializer.is_valid(raise_exception=True)
        services.change_password(request.user, serializer.validated_data["new_password"])
        return Response({"detail": "Parol yangilandi"})


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("search", description="login, ism, familiya, telefon"),
            query_param("role", description="ADMIN, WAREHOUSE, LOGISTICS, VET, OWNER yoki vergul bilan bir nechtasi"),
            query_param("is_active", bool),
        ]
    ),
    create=extend_schema(request=UserCreateSerializer, responses={201: UserCreatedSerializer, 400: ERROR_400}),
    partial_update=extend_schema(request=UserUpdateSerializer, responses={200: UserSerializer, 400: ERROR_400}),
    reset_password=extend_schema(request=None, responses=TempPasswordSerializer),
    unlink_telegram=extend_schema(request=None, responses=DetailSerializer),
    end_sessions=extend_schema(request=EndSessionsSerializer, responses={200: DetailSerializer, 400: ERROR_400}),
)
class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Admin-only user management (OWNER accounts are created together with households).

    Django superusers are invisible (404) to non-superuser admins: otherwise an API admin
    could reset a superuser's password and take over the Django admin site.
    """

    permission_classes = [IsAdminRole]
    serializer_class = UserSerializer
    lookup_value_regex = ID_REGEX

    def get_queryset(self) -> QuerySet[User]:
        qs = user_queryset().order_by("id")
        if not self.request.user.is_superuser:
            qs = qs.exclude(is_superuser=True)
        if self.action != "list":
            return qs
        search = query.get_str(self.request, "search")
        if search:
            qs = qs.filter(
                Q(username__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(phone__icontains=search)
            )
        roles = self._role_filter()
        if roles:
            # A superuser always counts as ADMIN, whatever its stored role.
            role_q = Q(role__in=roles, is_superuser=False)
            if Role.ADMIN in roles:
                role_q |= Q(is_superuser=True)
            qs = qs.filter(role_q)
        is_active = query.get_bool(self.request, "is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def _role_filter(self) -> set[str]:
        """``?role=ADMIN`` or a comma-separated list (``?role=ADMIN,WAREHOUSE`` = staff only)."""
        raw = query.get_str(self.request, "role")
        roles = {item.strip() for item in raw.split(",") if item.strip()}
        unknown = sorted(roles - set(Role.values))
        if unknown:
            raise ValidationError({"role": [f"“{unknown[0]}” ruxsat etilgan qiymat emas."]})
        return roles

    def create(self, request: Request) -> Response:
        serializer = UserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, password = services.create_staff_user(actor=request.user, **serializer.validated_data)
        return Response(
            {"user": UserSerializer(user).data, "temp_password": password},
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        user = self.get_object()
        serializer = UserUpdateSerializer(user, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        services.update_user(user, dict(serializer.validated_data), actor=request.user)
        return Response(UserSerializer(user).data)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request: Request, pk: str | None = None) -> Response:
        password = services.reset_password(self.get_object(), actor=request.user)
        return Response({"temp_password": password})

    @action(detail=True, methods=["post"], url_path="unlink-telegram")
    def unlink_telegram(self, request: Request, pk: str | None = None) -> Response:
        services.unlink_telegram(self.get_object(), actor=request.user)
        return Response({"detail": "Telegram uzildi"})

    @action(detail=True, methods=["post"], url_path="end-sessions")
    def end_sessions(self, request: Request, pk: str | None = None) -> Response:
        """Bumps ``token_version`` (every Web App session ends); optionally unlinks Telegram (V3 §2.4)."""
        target = self.get_object()
        serializer = EndSessionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.end_sessions(target, actor=request.user, **serializer.validated_data)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)}) from None
        return Response({"detail": "Barcha sessiyalar yakunlandi"})
