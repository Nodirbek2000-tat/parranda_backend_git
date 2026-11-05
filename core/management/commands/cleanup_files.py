"""``manage.py cleanup_files`` — deletes unbound uploads older than 24 h (API_CONTRACT_V3 §4.5). Run daily."""

from django.core.management.base import BaseCommand

from core.files import cleanup_unbound


class Command(BaseCommand):
    help = "24 soatdan eski, hech narsaga biriktirilmagan fayllarni o‘chiradi."

    def handle(self, *args, **options) -> None:
        count = cleanup_unbound()
        self.stdout.write(f"O‘chirilgan fayllar: {count}")
