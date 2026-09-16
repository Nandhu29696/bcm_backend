"""Send today's review escalation reminders. Safe to run more than once a day."""

import datetime as dt

from django.core.management.base import BaseCommand

from apps.plans.reminders import send_review_reminders


class Command(BaseCommand):
    help = "Remind BU leads of plans pending review beyond REVIEW_REMINDER_DAYS."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Treat this date (YYYY-MM-DD) as today.")

    def handle(self, *args, **options):
        today = dt.date.fromisoformat(options["as_of"]) if options["as_of"] else None
        self.stdout.write(self.style.SUCCESS(f"{send_review_reminders(today)} reminder(s) sent."))
