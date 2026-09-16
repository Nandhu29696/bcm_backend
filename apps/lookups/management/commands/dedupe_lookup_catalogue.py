"""Collapse duplicate catalogue rows (PENDING #12). Safe to run repeatedly."""

from django.core.management.base import BaseCommand

from apps.lookups.services import dedupe_catalogue, duplicate_groups


class Command(BaseCommand):
    help = "Retire duplicate lookup catalogue rows, moving child values and answer references to the survivor."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="List the groups; change nothing."
        )

    def handle(self, *args, **options):
        groups = duplicate_groups()
        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate catalogue rows."))
            return
        for rows in groups:
            first = rows[0]
            self.stdout.write(
                f"  {first.category_type} | {first.category_name}: "
                + ", ".join(
                    f"#{r.pk} ({'weighted ' + str(r.points) if r.points is not None else 'no points'})"
                    for r in rows
                )
            )
        if options["dry_run"]:
            self.stdout.write(f"{len(groups)} group(s); nothing changed (dry run).")
            return
        result = dedupe_catalogue()
        self.stdout.write(
            self.style.SUCCESS(
                f"Retired {result['retired']} row(s) in {result['groups']} group(s); "
                f"moved {result['values_moved']} value(s); rewrote {result['answers_rewritten']} answer(s)."
            )
        )
