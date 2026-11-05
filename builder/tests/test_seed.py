"""``seed_builder_examples`` is idempotent and creates valid drafts only."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command

from builder import schemas, services
from builder.models import Blueprint, BlueprintKind, BlueprintStatus

from .helpers import BuilderTestCase


class SeedTests(BuilderTestCase):
    def test_seed_is_idempotent_and_valid(self):
        call_command("seed_builder_examples", stdout=StringIO())
        count = Blueprint.objects.count()
        call_command("seed_builder_examples", stdout=StringIO())
        self.assertEqual(Blueprint.objects.count(), count)
        self.assertFalse(Blueprint.objects.exclude(status=BlueprintStatus.DRAFT).exists())
        for bp in Blueprint.objects.all():
            schemas.validate_data(bp.kind, bp.data)
        process = Blueprint.objects.get(kind=BlueprintKind.PROCESS, name="Xonadon to‘liq sikli")
        titles = [stage["title"] for stage in process.data["stages"]]
        self.assertEqual(titles, ["Xonadon tasdiqlandi", "Qurilish", "Ombor", "Inkubatsiya", "Logistika",
                                  "Veterinar", "Qushxona", "Buxgalteriya"])
        self.assertEqual(Blueprint.objects.filter(kind=BlueprintKind.ROLE).count(), 2)
        self.assertEqual(Blueprint.objects.filter(kind=BlueprintKind.DOCUMENT).count(), 7)
        # the examples can be activated bottom-up (roles/forms/documents → tasks → process)
        for kind in (BlueprintKind.ROLE, BlueprintKind.FORM, BlueprintKind.DOCUMENT, BlueprintKind.TASK,
                     BlueprintKind.PROCESS):
            for bp in Blueprint.objects.filter(kind=kind):
                services.activate_blueprint(actor=self.superuser, pk=bp.pk)
        self.assertFalse(Blueprint.objects.exclude(status=BlueprintStatus.ACTIVE).exists())
