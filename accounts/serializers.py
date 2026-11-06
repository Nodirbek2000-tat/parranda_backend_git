from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Role, User


def user_json(user: User) -> dict:
    """The one user shape of the API (login, /me, /users, bot)."""
    return {
        "id": user.pk,
        "username": user.username,
        "full_name": user.full_name,
        "phone": user.phone,
        "role": user.role,
        "role_display": user.get_role_display(),
        "is_superadmin": user.is_superadmin,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "telegram_linked": user.telegram_id is not None,
        "date_joined": user.date_joined,
    }


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False, min_length=8, max_length=128)

    def validate(self, attrs):
        user = self.context["user"]
        if not user.check_password(attrs["old_password"]):
            raise serializers.ValidationError({"old_password": ["Joriy parol noto‘g‘ri."]})
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError({"new_password": ["Yangi parol eskisidan farq qilishi kerak."]})
        try:
            validate_password(attrs["new_password"], user)
        except Exception as exc:  # django ValidationError
            raise serializers.ValidationError({"new_password": list(getattr(exc, "messages", [str(exc)]))}) from None
        return attrs


class UserCreateSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=Role.choices)
    password = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, min_length=8,
                                     write_only=True)

    class Meta:
        model = User
        fields = ("username", "full_name", "phone", "role", "password")
        extra_kwargs = {"full_name": {"required": True, "allow_blank": False}}

    def validate_username(self, value):
        value = value.strip()
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("Bu login band.")
        return value


class UserUpdateSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=Role.choices)

    class Meta:
        model = User
        fields = ("full_name", "phone", "role", "is_active")
