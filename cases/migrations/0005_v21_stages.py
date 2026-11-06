"""v2.1 data migration: the chain grows from 10 to 13 stages.

For every existing case that still has the old numbering (stage 10 = "accounting"):
- the old stage 10 row becomes stage 13 (data, status, dates, events, photos and documents keep pointing at it);
- new rows 10 (growing), 11 (pickup) and 12 (slaughter) are added;
- a case that already passed stage 9 (status DONE, or current_stage 10 = it was at accounting, in progress or
  rejected there) gets 10-12 as DONE with a StageEvent "migratsiya" each, and current_stage 10 -> 13;
- any other case gets 10-12 as WAITING (it will reach them normally).
Idempotent: cases that already have a stage 13 row are skipped.

Also marks the existing case-linked stock moves (only the stage-5 package could create them) as purpose "package".
"""

from django.db import migrations
from django.utils import timezone

NEW_STAGES = ((10, "growing"), (11, "pickup"), (12, "slaughter"))
NOTE = "migratsiya"


def forwards(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    CaseStage = apps.get_model("cases", "CaseStage")
    StageEvent = apps.get_model("cases", "StageEvent")
    Photo = apps.get_model("cases", "Photo")
    Document = apps.get_model("cases", "Document")
    StockMove = apps.get_model("stock", "StockMove")

    StockMove.objects.filter(case__isnull=False, purpose="").update(purpose="package")

    now = timezone.now()
    for case in Case.objects.all().order_by("pk"):  # historical manager: deleted cases too
        stages = {st.number: st for st in CaseStage.objects.filter(case=case)}
        old = stages.get(10)
        if 13 in stages or old is None or old.key != "accounting":
            continue
        old.number = 13
        old.save(update_fields=["number"])
        StageEvent.objects.filter(case=case, stage_number=10).update(stage_number=13)
        Photo.objects.filter(case=case, stage=10).update(stage=13)
        Document.objects.filter(case=case, stage=10).update(stage=13)

        passed = case.status == "DONE" or case.current_stage == 10
        when = old.activated_at or case.done_at or now
        for number, key in NEW_STAGES:
            if passed:
                CaseStage.objects.create(case=case, number=number, key=key, status="DONE", note=NOTE,
                                         activated_at=when, submitted_at=when)
                StageEvent.objects.create(case=case, stage_number=number, action="submit", note=NOTE)
            else:
                CaseStage.objects.create(case=case, number=number, key=key, status="WAITING")
        if case.current_stage == 10:
            case.current_stage = 13
            case.save(update_fields=["current_stage"])


def backwards(apps, schema_editor):
    """Best effort: back to 10 stages (rows 10-12 are dropped, 13 becomes 10 again)."""
    Case = apps.get_model("cases", "Case")
    CaseStage = apps.get_model("cases", "CaseStage")
    StageEvent = apps.get_model("cases", "StageEvent")
    Photo = apps.get_model("cases", "Photo")
    Document = apps.get_model("cases", "Document")

    for case in Case.objects.all().order_by("pk"):
        last = CaseStage.objects.filter(case=case, number=13).first()
        if last is None:
            continue
        CaseStage.objects.filter(case=case, number__in=(10, 11, 12)).delete()
        StageEvent.objects.filter(case=case, stage_number__in=(10, 11, 12)).delete()
        Photo.objects.filter(case=case, stage__in=(10, 11, 12)).update(stage=None)
        Document.objects.filter(case=case, stage__in=(10, 11, 12)).update(stage=None)
        last.number = 10
        if case.current_stage in (10, 11, 12) and last.status == "WAITING":
            last.status, last.activated_at = "ACTIVE", timezone.now()
        last.save()
        StageEvent.objects.filter(case=case, stage_number=13).update(stage_number=10)
        Photo.objects.filter(case=case, stage=13).update(stage=10)
        Document.objects.filter(case=case, stage=13).update(stage=10)
        if case.current_stage > 10:
            case.current_stage = 10
            case.save(update_fields=["current_stage"])


class Migration(migrations.Migration):

    dependencies = [
        ("cases", "0004_v21"),
        ("stock", "0003_v21"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
