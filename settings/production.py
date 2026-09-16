"""
Production settings.

Anything that would be unsafe with DEBUG=False is made explicit here rather than
inherited. `manage.py check --deploy --settings=bcm_backend.settings.production`
must be clean before a release.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import env

DEBUG = False

# Re-read here rather than inherit: a weak or auto-generated key must stop the
# process, not pass a warning by, and the check must see the real environment.
SECRET_KEY = env("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be a long random value in production.")

# A redirect left over from a test environment would send every BU lead's
# review request to one test mailbox and nobody would notice until a plan
# stalled. Refuse to start rather than run that way.
if env("NOTIFICATION_REDIRECT_TO", default="").strip():
    raise ImproperlyConfigured("NOTIFICATION_REDIRECT_TO must be empty in production.")

# No default — production must declare its hosts.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# --- HTTPS / transport security ---
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# --- Cookies ---
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

X_FRAME_OPTIONS = "DENY"

# --- Real infrastructure ---
CELERY_TASK_ALWAYS_EAGER = False

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("REDIS_CACHE_URL"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

# Structured logs for the shipper; a console format can still be forced by env.
LOG_FORMAT = env("LOG_FORMAT", default="json")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default="production")

# Persistent connections with a health check, so a restarted database does not
# leak stale connections into the first requests after it.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # noqa: F405

SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# API docs are opt-in in production.
ENABLE_API_DOCS = env.bool("ENABLE_API_DOCS", default=False)

# The master switch for the second factor stays on here; per-account MFA is
# an administrator's decision in User administration, not an environment's. `base.py` reads this from the
# environment so end-to-end tests can turn it off; a stray REQUIRE_OTP_FOR_LOGIN
# in a production `.env` would then silently disable multi-factor login for
# everyone, and nothing about the running system would look wrong.
REQUIRE_OTP_FOR_LOGIN = True
