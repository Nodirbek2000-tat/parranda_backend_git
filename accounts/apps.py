from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Foydalanuvchilar"

    def ready(self) -> None:
        from . import schema  # noqa: F401  (registers the OpenAPI authentication extension)
