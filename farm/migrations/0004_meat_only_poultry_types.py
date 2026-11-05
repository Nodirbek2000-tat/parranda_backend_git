# Client decision 2026-11-05: the company works only with meat poultry.

from django.db import migrations, models


def layer_batches_to_broiler(apps, schema_editor):
    """Existing egg-type ("LAYER") batches become meat batches (BROILER) before the choice is removed."""
    PoultryBatch = apps.get_model("farm", "PoultryBatch")
    PoultryBatch.objects.filter(poultry_type="LAYER").update(poultry_type="BROILER")


class Migration(migrations.Migration):

    dependencies = [
        ("farm", "0003_household_is_demo"),
    ]

    operations = [
        migrations.RunPython(layer_batches_to_broiler, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="poultrybatch",
            name="poultry_type",
            field=models.CharField(
                choices=[("BROILER", "Go‘sht yo‘nalishi"), ("BREEDER", "Naslli")],
                max_length=16,
                verbose_name="Parranda turi",
            ),
        ),
    ]
