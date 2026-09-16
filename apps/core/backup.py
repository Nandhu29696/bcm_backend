"""
Database and media backup, and a restore that proves itself (Phase 10.5).

    backup_database        mysqldump (single transaction) gzipped + media tarball + manifest
    restore_database       load a dump into a scratch database, compare row counts to the
                           manifest, drop the scratch database - or, explicitly, into the live one

The manifest records the row count of every table at dump time, so a restore
can be checked without a person eyeballing it. `restore_database --verify`
is what the runbook schedules weekly: a backup that has never been restored
is a hope, not a backup.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from django.conf import settings
from django.db import connection

KEY_TABLES = [
    "user_accounts",
    "estates",
    "cost_codes",
    "plans",
    "plan_versions",
    "plan_status_history",
    "question_answers",
    "risks",
    "risk_actions",
    "documents",
    "entity_documents",
    "exemptions",
    "tests",
    "test_outcomes",
    "crisis_events",
    "cmsc_members",
    "call_tree_runs",
    "call_attempts",
    "notification_log",
    "report_requests",
    "help_resources",
]

DEFAULT_WINDOWS_BIN = Path(r"C:\Program Files\MySQL\MySQL Server 8.0\bin")


def mysql_binary(name: str) -> str:
    """Find mysqldump / mysql: MYSQL_BIN_DIR, then PATH, then the Windows default install."""
    override = os.environ.get("MYSQL_BIN_DIR")
    for directory in [Path(override)] if override else []:
        for candidate in (directory / name, directory / f"{name}.exe"):
            if candidate.exists():
                return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    for candidate in (DEFAULT_WINDOWS_BIN / f"{name}.exe",):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        f"{name} not found: set MYSQL_BIN_DIR or add the MySQL bin directory to PATH."
    )


def db_settings() -> dict:
    return settings.DATABASES["default"]


def _conn_args(db: dict) -> list[str]:
    args = [
        f"--host={db.get('HOST') or '127.0.0.1'}",
        f"--port={db.get('PORT') or 3306}",
        f"--user={db['USER']}",
    ]
    return args


def _env(db: dict) -> dict:
    # The password travels in the environment, never on the command line.
    return {**os.environ, "MYSQL_PWD": db.get("PASSWORD") or ""}


def table_counts(database: str | None = None) -> dict[str, int]:
    counts = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            [database or db_settings()["NAME"]],
        )
        tables = [row[0] for row in cursor.fetchall()]
        prefix = f"`{database}`." if database else ""
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {prefix}`{table}`")
            counts[table] = cursor.fetchone()[0]
    return counts


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(out_dir: Path, *, include_media: bool = True) -> dict:
    """Dump the database and media into `out_dir`; return the manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    db = db_settings()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    dump_path = out_dir / f"bcm-{stamp}.sql.gz"

    command = [
        mysql_binary("mysqldump"),
        *_conn_args(db),
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        "--set-gtid-purged=OFF",
        "--default-character-set=utf8mb4",
        db["NAME"],
    ]
    with gzip.open(dump_path, "wb", compresslevel=6) as target:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(db)
        )
        assert process.stdout is not None
        shutil.copyfileobj(process.stdout, target)
        _, stderr = process.communicate()
    if process.returncode != 0:
        dump_path.unlink(missing_ok=True)
        raise RuntimeError(f"mysqldump failed: {stderr.decode(errors='replace')[:2000]}")

    manifest = {
        "created_at": stamp,
        "database": db["NAME"],
        "dump": dump_path.name,
        "dump_sha256": sha256_of(dump_path),
        "dump_bytes": dump_path.stat().st_size,
        "tables": table_counts(),
        "app_version": settings.APP_VERSION,
    }

    if include_media:
        media_root = Path(settings.MEDIA_ROOT)
        media_path = out_dir / f"media-{stamp}.tar.gz"
        with tarfile.open(media_path, "w:gz") as tar:
            if media_root.exists():
                tar.add(media_root, arcname="media")
        manifest["media"] = media_path.name
        manifest["media_sha256"] = sha256_of(media_path)
        manifest["media_bytes"] = media_path.stat().st_size

    manifest_path = out_dir / f"manifest-{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["manifest"] = manifest_path.name
    return manifest


def restore(dump_path: Path, *, database: str) -> None:
    """Load a gzipped dump into `database`, which is created (or emptied) first."""
    db = db_settings()
    mysql = mysql_binary("mysql")
    base = [mysql, *_conn_args(db), "--default-character-set=utf8mb4"]
    subprocess.run(
        [
            *base,
            "-e",
            f"DROP DATABASE IF EXISTS `{database}`; CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
        ],
        check=True,
        env=_env(db),
        capture_output=True,
    )
    with gzip.open(dump_path, "rb") as source:
        process = subprocess.Popen(
            [*base, database], stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(db)
        )
        assert process.stdin is not None
        shutil.copyfileobj(source, process.stdin)
        process.stdin.close()
        _, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"mysql restore failed: {stderr.decode(errors='replace')[:2000]}")


def verify(manifest: dict, *, database: str) -> dict:
    """Compare the restored database's row counts with the manifest. Returns the differences."""
    restored = table_counts(database)
    expected = manifest["tables"]
    differences = {
        table: {"expected": n, "restored": restored.get(table)}
        for table, n in expected.items()
        if restored.get(table) != n
    }
    missing = [t for t in expected if t not in restored]
    return {
        "tables_expected": len(expected),
        "tables_restored": len(restored),
        "differences": differences,
        "missing": missing,
    }


def drop_database(database: str) -> None:
    db = db_settings()
    subprocess.run(
        [mysql_binary("mysql"), *_conn_args(db), "-e", f"DROP DATABASE IF EXISTS `{database}`"],
        check=True,
        env=_env(db),
        capture_output=True,
    )
