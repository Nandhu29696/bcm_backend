"""
Restore a backup - by default into a scratch database that is verified and dropped.

    restore_database --dump backups/bcm-....sql.gz --verify
    restore_database --dump ... --into-live --yes          (the real thing; take a backup first)
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.core.backup import db_settings, drop_database, restore, verify


class Command(BaseCommand):
    help = "Restore a gzipped dump into a scratch database (verified against its manifest) or into the live one."

    def add_arguments(self, parser):
        parser.add_argument("--dump", required=True, help="Path to bcm-<stamp>.sql.gz")
        parser.add_argument(
            "--manifest", help="Path to manifest-<stamp>.json (default: alongside the dump)."
        )
        parser.add_argument(
            "--verify", action="store_true", help="Compare row counts with the manifest."
        )
        parser.add_argument(
            "--keep", action="store_true", help="Keep the scratch database after verifying."
        )
        parser.add_argument(
            "--into-live", action="store_true", help="Restore into the configured database."
        )
        parser.add_argument("--yes", action="store_true", help="Required with --into-live.")

    def handle(self, *args, **options):
        dump = Path(options["dump"])
        if not dump.exists():
            raise CommandError(f"No such dump: {dump}")
        manifest_path = (
            Path(options["manifest"])
            if options["manifest"]
            else dump.with_name(dump.name.replace("bcm-", "manifest-").replace(".sql.gz", ".json"))
        )
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else None
        )

        live = db_settings()["NAME"]
        if options["into_live"]:
            if not options["yes"]:
                raise CommandError(
                    "Restoring into the live database replaces it. Pass --yes to confirm."
                )
            target = live
        else:
            target = f"{live}_restore_check"

        self.stdout.write(f"Restoring {dump.name} into `{target}`...")
        restore(dump, database=target)
        self.stdout.write(self.style.SUCCESS("Restore completed."))

        if options["verify"] or not options["into_live"]:
            if manifest is None:
                raise CommandError(f"Cannot verify without a manifest ({manifest_path}).")
            result = verify(manifest, database=target)
            self.stdout.write(
                f"  tables: {result['tables_restored']} restored, {result['tables_expected']} expected"
            )
            if result["differences"] or result["missing"]:
                for table, counts in result["differences"].items():
                    self.stdout.write(
                        self.style.ERROR(
                            f"  {table}: expected {counts['expected']}, restored {counts['restored']}"
                        )
                    )
                for table in result["missing"]:
                    self.stdout.write(self.style.ERROR(f"  {table}: missing"))
                if not options["keep"] and not options["into_live"]:
                    drop_database(target)
                raise CommandError("Verification failed.")
            self.stdout.write(self.style.SUCCESS("  every table's row count matches the manifest"))

        if not options["into_live"] and not options["keep"]:
            drop_database(target)
            self.stdout.write(f"Scratch database `{target}` dropped.")
