"""Send today's overdue risk-action reminders. Safe to run more than once a day."""

import datetime as dt

from django.core.management.base import BaseCommand

from apps.risk.reminders import send_overdue_reminders


class Command(BaseCommand):
    help = "Email owners of overdue risk actions. Idempotent per action per day."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Treat this date (YYYY-MM-DD) as today.")

    def handle(self, *args, **options):
        today = dt.date.fromisoformat(options["as_of"]) if options["as_of"] else None
        sent = send_overdue_reminders(today)
        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent."))
