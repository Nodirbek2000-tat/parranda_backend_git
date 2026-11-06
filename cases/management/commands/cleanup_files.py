from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from cases.models import Document, Photo


class Command(BaseCommand):
    help = "Hech qaysi xonadonga biriktirilmagan, 24 soatdan eski fayllarni o‘chiradi."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, hours, **options):
        cutoff = timezone.now() - timedelta(hours=hours)
        removed = 0
        for model, field in ((Photo, "image"), (Document, "file")):
            for obj in model.objects.filter(case__isnull=True, created_at__lt=cutoff):
                getattr(obj, field).delete(save=False)
                obj.delete()
                removed += 1
        self.stdout.write(f"O‘chirilgan fayllar: {removed}")
