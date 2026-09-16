"""Back up the database and media directory. Schedule it; then verify it with restore_database."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.backup import backup


class Command(BaseCommand):
    help = "Write a gzipped mysqldump, a media tarball and a manifest of row counts."

    def add_arguments(self, parser):
        parser.add_argument("--out-dir", default=str(Path(settings.BASE_DIR).parent / "backups"))
        parser.add_argument("--no-media", action="store_true", help="Database only.")

    def handle(self, *args, **options):
        manifest = backup(Path(options["out_dir"]), include_media=not options["no_media"])
        self.stdout.write(self.style.SUCCESS(f"Backup written to {options['out_dir']}"))
        self.stdout.write(
            f"  dump      {manifest['dump']} ({manifest['dump_bytes']:,} bytes, sha256 {manifest['dump_sha256'][:12]}...)"
        )
        if "media" in manifest:
            self.stdout.write(
                f"  media     {manifest['media']} ({manifest['media_bytes']:,} bytes)"
            )
        self.stdout.write(f"  manifest  {manifest['manifest']} ({len(manifest['tables'])} tables)")
