from __future__ import annotations

import re

from django.contrib.auth import password_validation
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from common.roles import STAFF_ROLE_CHOICES, role_display

from .models import User
from .tokens import MSG_SESSION_REVOKED, NNPRefreshToken, ensure_token_current

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{3,150}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{6,19}$")
MIN_PASSWORD_LENGTH = 8

# Uzbek texts for Django's AUTH_PASSWORD_VALIDATORS, keyed by error code.
PASSWORD_VALIDATOR_MESSAGES: dict[str, str] = {
    "password_too_short": f"Parol kamida {MIN_PASSWORD_LENGTH} ta belgidan iborat bo‘lishi kerak",
    "password_too_common": "Bu parol juda oddiy. Harflar va raqamlardan iborat murakkabroq parol tanlang",
    "password_entirely_numeric": "Parol faqat raqamlardan iborat bo‘lmasligi kerak",
    "password_too_similar": "Parol login yoki ism-familiyangizga juda o‘xshash",
}
PASSWORD_INVALID_FALLBACK = "Parol talablarga javob bermaydi. Murakkabroq parol tanlang"


def validate_new_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.fullmatch(value):
        raise serializers.ValidationError(
            "Login 3–150 belgidan iborat bo‘lsin: lotin harflari, raqamlar va . _ @ + - belgilari."
        )
    if User.objects.filter(username__iexact=value).exists():
        raise serializers.ValidationError("Bu login band")
    return value


def validate_phone(value: str) -> str:
    value = value.strip()
    if value and not PHONE_RE.fullmatch(value):
        raise serializers.ValidationError("Telefon raqami noto‘g‘ri. Masalan: +998901234567")
    return value


class UserRefSerializer(serializers.ModelSerializer):
    """``{"id", "full_name"}`` used for ``created_by``."""

    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "full_name")


def mask_telegram_id(value: int | None) -> str | None:
    """``•••4567``: enough to tell accounts apart (TZ §5 "Telegram ID"), not the full id."""
    if value is None:
        return None
    digits = str(value)
    return f"•••{digits[-4:]}" if len(digits) > 4 else "•••"


class OwnerShortSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    telegram_linked = serializers.BooleanField(read_only=True)
    telegram_id_masked = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "username", "full_name", "phone", "telegram_linked", "telegram_id_masked", "is_active")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_telegram_id_masked(self, user: User) -> str | None:
        return mask_telegram_id(user.telegram_id)


def has_driver_relation() -> bool:
    """``True`` once ``logistics.Driver`` (``user = OneToOneField(User, related_name="driver")``) exists."""
    try:
        User._meta.get_field("driver")
    except FieldDoesNotExist:
        return False
    return True


def user_queryset() -> QuerySet[User]:
    """Users with everything :class:`UserSerializer` reads (no per-row queries)."""
    qs = User.objects.select_related("household")
    if has_driver_relation():
        qs = qs.select_related("driver")
    return qs


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    role = serializers.CharField(source="effective_role", read_only=True)
    role_display = serializers.SerializerMethodField()
    is_super_admin = serializers.SerializerMethodField()
    telegram_linked = serializers.BooleanField(read_only=True)
    household_id = serializers.IntegerField(source="household_id_or_none", read_only=True, allow_null=True)
    driver_id = serializers.SerializerMethodField()

    def get_role_display(self, user: User) -> str:
        return role_display(user)

    def get_is_super_admin(self, user: User) -> bool:
        return bool(user.is_superuser)

    def get_driver_id(self, user: User) -> int | None:
        """Id of the ``logistics.Driver`` linked to this user (LOGISTICS only), else ``None``."""
        try:
            driver = getattr(user, "driver", None)
        except ObjectDoesNotExist:
            return None
        return getattr(driver, "pk", None)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "first_name",
            "last_name",
            "full_name",
            "phone",
            "role",
            "role_display",
            "is_super_admin",
            "is_active",
            "must_change_password",
            "telegram_linked",
            "household_id",
            "driver_id",
            "date_joined",
        )
        read_only_fields = fields


class AccessSerializer(serializers.Serializer):
    """API_CONTRACT_V4 §2: role profile + scope of the current user (``permissions: null`` = defaults)."""

    profile_id = serializers.IntegerField(allow_null=True)
    profile_name = serializers.CharField(allow_null=True)
    permissions = serializers.DictField(child=serializers.ListField(child=serializers.CharField()), allow_null=True)
    districts = serializers.ListField(child=serializers.CharField())
    household_ids = serializers.ListField(child=serializers.IntegerField())


class MeSerializer(UserSerializer):
    """``GET /api/auth/me``: :class:`UserSerializer` + ``access`` (V4 §2)."""

    access = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = (*UserSerializer.Meta.fields, "access")
        read_only_fields = fields

    @extend_schema_field(AccessSerializer)
    def get_access(self, user: User) -> dict:
        from builder.access import access_payload

        return access_payload(user)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class RefreshSerializer(TokenRefreshSerializer):
    """SimpleJWT refresh that rejects revoked tokens and answers 401 (not 500) for a deleted user."""

    token_class = NNPRefreshToken

    def validate(self, attrs: dict) -> dict:
        try:
            refresh = self.token_class(attrs["refresh"])  # TokenError -> 401 in the view
        except TokenError as exc:
            if "blacklist" in str(exc.args[0] if exc.args else "").lower():
                # Logged out (V3 §2.3): Uzbek text + SimpleJWT's code instead of "Token is blacklisted".
                raise AuthenticationFailed({"detail": MSG_SESSION_REVOKED, "code": "token_not_valid"}) from None
            raise
        user = User.objects.filter(
            **{jwt_settings.USER_ID_FIELD: refresh.payload.get(jwt_settings.USER_ID_CLAIM)}
        ).first()
        if user is None:
            raise AuthenticationFailed(MSG_SESSION_REVOKED, "user_not_found")
        if user.is_active:  # a blocked user gets the "blocked" message from SimpleJWT below
            ensure_token_current(refresh, user)
        return super().validate(attrs)


class TelegramAuthSerializer(serializers.Serializer):
    init_data = serializers.CharField(max_length=4096, trim_whitespace=True)


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)

    def validate_old_password(self, value: str) -> str:
        if not self.context["user"].check_password(value):
            raise serializers.ValidationError("Joriy parol noto‘g‘ri")
        return value

    def validate_new_password(self, value: str) -> str:
        if len(value) < MIN_PASSWORD_LENGTH:
            raise serializers.ValidationError(PASSWORD_VALIDATOR_MESSAGES["password_too_short"])
        try:
            password_validation.validate_password(value, user=self.context["user"])
        except DjangoValidationError as exc:
            messages = {PASSWORD_VALIDATOR_MESSAGES.get(error.code, PASSWORD_INVALID_FALLBACK) for error in exc.error_list}
            raise serializers.ValidationError(sorted(messages)) from None
        return value

    def validate(self, attrs: dict) -> dict:
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": ["Yangi parol joriy paroldan farq qilishi kerak"]}
            )
        return attrs


class UserCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, validators=[validate_new_username])
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, allow_blank=True, default="")
    phone = serializers.CharField(max_length=32, allow_blank=True, default="", validators=[validate_phone])
    role = serializers.ChoiceField(choices=STAFF_ROLE_CHOICES)


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(max_length=4096, required=False, allow_blank=True, default="")
    all = serializers.BooleanField(required=False, default=False)


class EndSessionsSerializer(serializers.Serializer):
    unlink_telegram = serializers.BooleanField(required=False, default=False)


class UserUpdateSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(max_length=32, allow_blank=True, required=False, validators=[validate_phone])

    class Meta:
        model = User
        fields = ("first_name", "last_name", "phone", "is_active")
        extra_kwargs = {
            "first_name": {"required": False},
            "last_name": {"required": False},
            "is_active": {"required": False},
        }

    def validate_is_active(self, value: bool) -> bool:
        request = self.context["request"]
        if value is False and self.instance is not None and self.instance.pk == request.user.pk:
            raise serializers.ValidationError("O‘zingizni bloklay olmaysiz")
        return value
