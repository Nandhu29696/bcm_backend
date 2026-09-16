"""Send today's upcoming-test reminders. Safe to run more than once a day."""

import datetime as dt

from django.core.management.base import BaseCommand

from apps.testing.reminders import send_test_reminders


class Command(BaseCommand):
    help = "Remind coordinators of tests scheduled TEST_REMINDER_DAYS from today."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Treat this date (YYYY-MM-DD) as today.")

    def handle(self, *args, **options):
        today = dt.date.fromisoformat(options["as_of"]) if options["as_of"] else None
        self.stdout.write(self.style.SUCCESS(f"{send_test_reminders(today)} reminder(s) sent."))
