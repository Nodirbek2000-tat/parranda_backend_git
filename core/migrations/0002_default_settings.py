"""Creates the SystemSettings singleton (pk=1) with the contract defaults (API_CONTRACT_V3 §3)."""

from django.db import migrations


def create_singleton(apps, schema_editor):
    SystemSettings = apps.get_model("core", "SystemSettings")
    SystemSettings.objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_singleton, migrations.RunPython.noop),
    ]
