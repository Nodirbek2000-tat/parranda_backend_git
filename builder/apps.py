from django.apps import AppConfig


class BuilderConfig(AppConfig):
    """Konstruktor (API_CONTRACT_V4): role profiles, forms, tasks, processes, document templates."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "builder"
    verbose_name = "Konstruktor"

    def ready(self) -> None:
        from core import files

        from . import audit_snapshots
        from .schemas import SUBMISSION_OBJECT_TYPE

        audit_snapshots.register()

        def submission_file_rule(user, attachment) -> bool:
            from .services import can_view_submission_file

            return can_view_submission_file(user, attachment)

        files.register_visibility(SUBMISSION_OBJECT_TYPE, submission_file_rule)
