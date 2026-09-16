"""Sweep data past its RETENTION_*_DAYS. `--dry-run` only counts."""

from django.core.management.base import BaseCommand

from apps.core.retention import apply_retention


class Command(BaseCommand):
    help = "Delete rows and report files older than the configured retention periods."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        counts = apply_retention(dry_run=options["dry_run"])
        verb = "would remove" if options["dry_run"] else "removed"
        if not counts:
            self.stdout.write("No retention periods are configured; nothing to do.")
            return
        for name, n in counts.items():
            self.stdout.write(f"  {name}: {verb} {n}")
