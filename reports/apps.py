from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Report export (API_CONTRACT_V3 §11): registry, data builders, xlsx.py, pdf.py."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "reports"
    verbose_name = "Hisobotlar"

    def ready(self) -> None:
        # Register here (see docs/SCAFFOLD_NOTES.md):
        # - audit snapshots:      audit.services.register_snapshot(Model, AuditObjectType.X, builder, op_id_attr="code")
        # - file visibility:      core.files.register_visibility("Model", rule)   (only if the default rule is not enough)
        # - global search group:  core.search.register_group(...)
        # - reminder jobs:        notifications.reminders.register_job(...)
        pass
