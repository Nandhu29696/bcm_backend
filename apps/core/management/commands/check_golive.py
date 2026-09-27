"""Validate the environment is really ready for production, in one pass.

`manage.py check --deploy` covers Django's own deploy checklist and the
hard fail-fast checks (DJANGO_SECRET_KEY, NOTIFICATION_REDIRECT_TO,
DJANGO_ALLOWED_HOSTS) already stop the process on their own. This adds
everything else in docs/GO_LIVE_CHECKLIST.md that would otherwise only
surface as a confusing error partway through the first real request:
unset infrastructure vars, and live reachability of the database, cache
and broker.

    manage.py check_golive --settings=bcm_backend.settings.production
"""

from django.conf import settings
from django.core.management.base import BaseCommand

#: (env var, human label). Presence only - format is whatever env() already
#: enforces (env.list, env.int, ...) when settings loads at all.
REQUIRED_VARS = [
    ("DB_NAME", "database name"),
    ("DB_USER", "database user"),
    ("DB_PASSWORD", "database password"),
    ("DB_HOST", "database host"),
    ("REDIS_CACHE_URL", "cache"),
    ("CELERY_BROKER_URL", "task broker"),
    ("CELERY_RESULT_BACKEND", "task result backend"),
    ("EMAIL_HOST", "SMTP host"),
    ("EMAIL_HOST_USER", "SMTP user"),
    ("EMAIL_HOST_PASSWORD", "SMTP password"),
    ("FRONTEND_URL", "frontend origin"),
    ("PUBLIC_BASE_URL", "backend public origin"),
    ("CORS_ALLOWED_ORIGINS", "CORS origins"),
    ("CSRF_TRUSTED_ORIGINS", "CSRF origins"),
]

#: Unset is a valid, deliberate choice for these - flagged as advisory only.
RECOMMENDED_VARS = [
    ("SENTRY_DSN", "error tracking is off until this is set"),
    ("METRICS_TOKEN", "GET /api/v1/metrics/ has no endpoint until this is set"),
    ("VAPID_PUBLIC_KEY", "browser push is off until this and VAPID_PRIVATE_KEY are set"),
    ("APP_VERSION", "Sentry events will not be tagged with a release"),
]


class Command(BaseCommand):
    help = "Check every go-live env var is set, and that the database/cache/broker are reachable."

    def handle(self, *args, **options):
        import environ

        env = environ.Env()
        problems = []
        warnings = []

        for key, label in REQUIRED_VARS:
            if not env(key, default=""):
                problems.append(f"{key} is not set ({label})")

        for key, note in RECOMMENDED_VARS:
            if not env(key, default=""):
                warnings.append(f"{key} is not set - {note}")

        problems += self._check_database()
        problems += self._check_cache()
        problems += self._check_broker()

        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"  ! {warning}"))

        if problems:
            for problem in problems:
                self.stdout.write(self.style.ERROR(f"  x {problem}"))
            self.stdout.write(self.style.ERROR(f"\n{len(problems)} problem(s) - not ready."))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("Go-live checks passed."))

    def _check_database(self) -> list[str]:
        from django.db import connection

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            return [f"database unreachable: {exc}"]
        return []

    def _check_cache(self) -> list[str]:
        from django.core.cache import cache

        try:
            cache.set("check_golive", "1", timeout=5)
            if cache.get("check_golive") != "1":
                return ["cache set/get round-trip did not return the value written"]
        except Exception as exc:  # noqa: BLE001
            return [f"cache (REDIS_CACHE_URL) unreachable: {exc}"]
        return []

    def _check_broker(self) -> list[str]:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return ["CELERY_TASK_ALWAYS_EAGER is True - tasks would run inline, not on a worker"]
        try:
            from bcm_backend.celery import app

            with app.connection_or_acquire() as conn:
                conn.ensure_connection(max_retries=1, timeout=5)
        except Exception as exc:  # noqa: BLE001
            return [f"task broker (CELERY_BROKER_URL) unreachable: {exc}"]
        return []
