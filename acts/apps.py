from django.apps import AppConfig


class ActsConfig(AppConfig):
    """Dalolatnomalar (API_CONTRACT_V4 §6): frozen acts of completed work, ``DL-000001``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "acts"
    verbose_name = "Dalolatnomalar"

    def ready(self) -> None:
        from audit import services as audit
        from audit.models import AuditObjectType

        from .models import Act

        audit.register_snapshot(
            Act,
            AuditObjectType.ACT,
            lambda act: {
                "code": act.code,
                "kind": act.kind,
                "role": act.role,
                "user_id": act.user_id,
                "household_id": act.household_id,
                "task_id": act.task_id,
                "date_from": act.date_from,
                "date_to": act.date_to,
                "template_id": act.template_id,
                "template_version": act.template_version,
            },
            op_id_attr="code",
        )
