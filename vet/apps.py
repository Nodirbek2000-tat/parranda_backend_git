from django.apps import AppConfig


class VetConfig(AppConfig):
    """Veterinary (API_CONTRACT_V3 §8): VetVisit, Vaccination, DiseaseRecord, Treatment, VetRequest."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "vet"
    verbose_name = "Veterinariya"

    def ready(self) -> None:
        from . import audit_snapshots, bot, reminders, search

        audit_snapshots.register()
        search.register()
        reminders.register()
        bot.register()
