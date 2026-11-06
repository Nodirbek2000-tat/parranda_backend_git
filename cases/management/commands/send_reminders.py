from django.core.management.base import BaseCommand

from cases.services import send_overdue_reminders


class Command(BaseCommand):
    help = "Muddati o‘tgan bosqichlar haqida kuniga bir marta eslatma (idempotent)."

    def handle(self, *args, **options):
        count = send_overdue_reminders()
        self.stdout.write(f"Muddati o‘tgan bosqichlar uchun eslatma: {count}")
