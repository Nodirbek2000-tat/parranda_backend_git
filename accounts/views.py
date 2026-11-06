from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from audit.models import log

from . import telegram
from .auth import TV_CLAIM, IsAdmin, issue_tokens
from .models import Role, User, make_temp_password
from .serializers import (ChangePasswordSerializer, LoginSerializer, UserCreateSerializer, UserUpdateSerializer,
                          user_json)

MSG_BAD_CREDENTIALS = "Login yoki parol noto‘g‘ri"
MSG_EXPIRED = "Sessiya muddati tugagan. Qaytadan kiring."


def login_response(user: User) -> dict:
    return {**issue_tokens(user), "user": user_json(user)}


def unauthorized(detail: str) -> Response:
    """Explicit 401: DRF turns AuthenticationFailed into 403 on views without authentication classes."""
    return Response({"detail": detail}, status=401)


def check_credentials(username: str, password: str) -> User | None:
    user = authenticate(username=username.strip(), password=password)
    if user is None or user.deleted_at is not None:
        return None
    return user


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = check_credentials(ser.validated_data["username"], ser.validated_data["password"])
        if user is None:
            return unauthorized(MSG_BAD_CREDENTIALS)
        log(user, "auth.login", user)
        return Response(login_response(user))


class RefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            token = RefreshToken(str(request.data.get("refresh", "")))
        except TokenError:
            return unauthorized(MSG_EXPIRED)
        user = User.objects.alive().filter(pk=token.get("user_id"), is_active=True).first()
        if user is None or token.get(TV_CLAIM) != user.token_version:
            return unauthorized(MSG_EXPIRED)
        return Response({"access": str(token.access_token)})


class LogoutView(APIView):
    def post(self, request):
        try:
            RefreshToken(str(request.data.get("refresh", ""))).blacklist()
        except TokenError:
            pass
        return Response({"detail": "Tizimdan chiqildi"})


class TelegramAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        try:
            tid = telegram.validate_init_data(str(request.data.get("init_data", "")), settings.BOT_TOKEN)
        except telegram.InitDataInvalid:
            return unauthorized("Telegram ma’lumotlari tasdiqlanmadi.")
        user = User.objects.alive().filter(telegram_id=tid).first()
        if user is None:
            return Response({"detail": "Telegram hisobingiz tizimga bog‘lanmagan. Botda /start bosing."}, status=404)
        if not user.is_active:
            raise PermissionDenied("Hisobingiz bloklangan. Administratorga murojaat qiling.")
        return Response(login_response(user))


class MeView(APIView):
    def get(self, request):
        return Response(user_json(request.user))


class ChangePasswordView(APIView):
    def post(self, request):
        user = request.user
        ser = ChangePasswordSerializer(data=request.data, context={"user": user})
        ser.is_valid(raise_exception=True)
        user.set_password(ser.validated_data["new_password"])
        user.must_change_password = False
        user.token_version += 1
        user.save()
        log(user, "auth.change_password", user)
        return Response(login_response(user))


# --- Users (ADMIN, SUPERADMIN) ------------------------------------------------------------------

PRIVILEGED = (Role.ADMIN, Role.SUPERADMIN)


def _guard_privileged(actor: User, *roles) -> None:
    """Only SUPERADMIN manages ADMIN / SUPERADMIN accounts."""
    if not actor.is_superadmin and any(role in PRIVILEGED for role in roles):
        raise PermissionDenied("Administrator hisoblarini faqat Super Admin boshqaradi.")


def _other_superadmins_exist(user: User) -> bool:
    return User.objects.active_with_role(Role.SUPERADMIN).exclude(pk=user.pk).exists()


class UserListView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        qs = User.objects.alive().order_by("role", "full_name")
        params = request.query_params
        if params.get("role"):
            qs = qs.filter(role=params["role"])
        if params.get("is_active") in ("true", "false", "1", "0"):
            qs = qs.filter(is_active=params["is_active"] in ("true", "1"))
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(Q(username__icontains=term) | Q(full_name__icontains=term) | Q(phone__icontains=term))
        return Response([user_json(u) for u in qs])

    def post(self, request):
        ser = UserCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        _guard_privileged(request.user, data["role"])
        password = data.pop("password", "") or make_temp_password()
        user = User(**data, must_change_password=True)
        user.set_password(password)
        user.save()
        log(request.user, "user.create", user, after={"username": user.username, "role": user.role})
        return Response({**user_json(user), "temp_password": password}, status=201)


class UserDetailView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request, pk):
        return Response(user_json(get_object_or_404(User.objects.alive(), pk=pk)))

    @transaction.atomic
    def patch(self, request, pk):
        user = get_object_or_404(User.objects.alive().select_for_update(), pk=pk)
        ser = UserUpdateSerializer(user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        new_role = ser.validated_data.get("role", user.role)
        _guard_privileged(request.user, user.role, new_role)
        demotes = user.role == Role.SUPERADMIN and (new_role != Role.SUPERADMIN
                                                     or ser.validated_data.get("is_active") is False)
        if demotes and not _other_superadmins_exist(user):
            raise ValidationError({"detail": "Oxirgi Super Adminni o‘zgartirib bo‘lmaydi."})
        if user.pk == request.user.pk and (new_role != user.role or ser.validated_data.get("is_active") is False):
            raise ValidationError({"detail": "O‘z rolingizni yoki holatingizni o‘zgartira olmaysiz."})
        before = user_json(user)
        if new_role != Role.SUPERADMIN and user.role == Role.SUPERADMIN:
            user.is_superuser = user.is_staff = False
        for key, value in ser.validated_data.items():
            setattr(user, key, value)
        user.save()
        log(request.user, "user.update", user, before=_jsonable(before), after=_jsonable(user_json(user)))
        return Response(user_json(user))

    @transaction.atomic
    def delete(self, request, pk):
        user = get_object_or_404(User.objects.alive().select_for_update(), pk=pk)
        if user.pk == request.user.pk:
            raise ValidationError({"detail": "O‘z hisobingizni o‘chira olmaysiz."})
        _guard_privileged(request.user, user.role)
        if user.role == Role.SUPERADMIN and not _other_superadmins_exist(user):
            raise ValidationError({"detail": "Oxirgi Super Adminni o‘chirib bo‘lmaydi."})
        user.deleted_at = timezone.now()
        user.is_active = False
        user.telegram_id = None
        user.token_version += 1
        user.username = f"{user.username}__deleted_{user.pk}"[:150]  # frees the login for a new account
        user.save()
        log(request.user, "user.delete", user)
        return Response(status=204)


class ResetPasswordView(APIView):
    permission_classes = [IsAdmin]

    @transaction.atomic
    def post(self, request, pk):
        user = get_object_or_404(User.objects.alive().select_for_update(), pk=pk)
        _guard_privileged(request.user, user.role)
        password = make_temp_password()
        user.set_password(password)
        user.must_change_password = True
        user.token_version += 1
        user.save()
        log(request.user, "user.reset_password", user)
        return Response({"temp_password": password})


def _jsonable(data: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in data.items()}
