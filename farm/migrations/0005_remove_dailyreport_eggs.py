# Client decision 2026-11-05: meat only — the egg count of the daily report is dropped.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("farm", "0004_meat_only_poultry_types"),
    ]

    operations = [
        # A default first, so that the removal can be reversed on a database that has reports.
        migrations.AlterField(
            model_name="dailyreport",
            name="eggs",
            field=models.PositiveIntegerField(default=0, verbose_name="Tuxum (dona)"),
        ),
        migrations.RemoveField(model_name="dailyreport", name="eggs"),
    ]
