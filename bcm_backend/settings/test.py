"""
Test settings.

Still MySQL — never SQLite (ARCHITECTURE.md §8). The schema uses JSON columns,
CHECK constraints and utf8mb4 collation, so SQLite would pass tests that
production fails. The test database is `test_<DB_NAME>`, which is why the app
user needs `GRANT ALL ON \`test_bcm%\`.*`.
"""

from .base import *  # noqa: F403

DEBUG = False

# Fast, deterministic hashing — tests do not need key stretching.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
# Tests assert on real recipients; a developer's .env redirect must not leak in.
NOTIFICATION_REDIRECT_TO = ""

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# External providers are hard-off in tests.
TWILIO_ENABLED = False
TEAMS_ENABLED = False

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
