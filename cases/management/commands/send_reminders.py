from django.core.management.base import BaseCommand

from cases.growing import growing_alerts, plan_weekly_visits, visit_reminders
from cases.services import send_overdue_reminders


class Command(BaseCommand):
    help = ("Scheduler (har 15 daqiqada, idempotent): muddati o‘tgan bosqichlar, haftalik veterinar tashriflari, "
            "tashrif eslatmalari va 45 kunlik parvarish ogohlantirishi.")

    def handle(self, *args, **options):
        overdue = send_overdue_reminders()
        planned = plan_weekly_visits()
        visits = visit_reminders()
        alerts = growing_alerts()
        self.stdout.write(f"Muddati o‘tgan bosqichlar: {overdue}; yangi tashriflar: {planned}; "
                          f"tashrif eslatmalari: {visits}; parvarish ogohlantirishlari: {alerts}")
