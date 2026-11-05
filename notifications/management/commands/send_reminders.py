"""``manage.py send_reminders [--date YYYY-MM-DD] [--force]`` (API_CONTRACT_V3 §16.1).

Fallback for Task Scheduler / cron; the bot's 15-minute tick uses the same guard, so both may run.
"""

from __future__ import annotations

from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from notifications import reminders


class Command(BaseCommand):
    help = "Kunlik eslatmalarni navbatga qo‘yadi (kuniga bir marta, reminder_hour dan keyin)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--date", default=None, help="YYYY-MM-DD (standart: bugun)")
        parser.add_argument("--force", action="store_true", help="Kunlik cheklovni e’tiborsiz qoldiradi")

    def handle(self, *args, **options) -> None:
        now = timezone.localtime()
        move_guard = True
        if options["date"]:
            day = parse_date(options["date"])
            if day is None:
                raise CommandError("Sana noto‘g‘ri formatda (YYYY-MM-DD).")
            if day > now.date():
                # A future date would send overdue/expired texts too early and burn their event keys.
                raise CommandError("Kelajakdagi sana uchun eslatma yuborib bo‘lmaydi.")
            if day != now.date():
                # Manual run for a past day: the daily guard (today's real run) is not touched.
                move_guard = False
                now = timezone.make_aware(datetime.combine(day, time(23, 59)))
        result = reminders.run_if_due(now, force=options["force"], move_guard=move_guard)
        if result["ran"]:
            self.stdout.write(f"{result['date']}: navbatga qo‘yildi {result['queued']} ta eslatma.")
        else:
            self.stdout.write(f"{result['date']}: eslatmalar bugun allaqachon yuborilgan yoki vaqti kelmagan.")
