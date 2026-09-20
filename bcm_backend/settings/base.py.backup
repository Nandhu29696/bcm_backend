"""
Shared settings for the BCM application.

Every environment-specific value is read from `.env` via django-environ. No secret,
hostname or credential is ever literal in this file (AD-11). Required keys have no
default, so a missing one fails loudly at boot instead of silently degrading.
"""

import sys
from datetime import timedelta
from pathlib import Path

import environ

# bcm_backend/bcm_backend/settings/base.py -> repo root is four levels up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env()
environ.Env.read_env(REPO_ROOT / ".env")


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

ROOT_URLCONF = "bcm_backend.urls"
WSGI_APPLICATION = "bcm_backend.wsgi.application"
ASGI_APPLICATION = "bcm_backend.asgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------- #
# Applications
# --------------------------------------------------------------------------- #

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "social_django",
]

# Domain apps (ARCHITECTURE.md §5.2). Every one of the 50 tables has exactly one owner.
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.organization",
    "apps.lookups",
    "apps.questionnaire",
    "apps.plans",
    "apps.assessments",
    "apps.risk",
    "apps.exemptions",
    "apps.calltree",
    "apps.crisis",
    "apps.testing",
    "apps.documents",
    "apps.helpcenter",
    "apps.reporting",
    "apps.notifications",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    # First, so the request id covers everything below it and the timing is honest.
    "apps.core.observability.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "NAME": "default",
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
    {
        # Plain-text email. Autoescaping is OFF here on purpose: with it on, a
        # URL's "&" is rendered as "&amp;" in a text/plain body, which silently
        # corrupts every multi-parameter link — a password reset arrives with its
        # token parameter mangled and the reset fails for every user.
        # Notification templates must be rendered with using="text_email".
        "NAME": "text_email",
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"autoescape": False},
    },
]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
# MySQL only. Tests also run against MySQL — never SQLite (ARCHITECTURE.md §8):
# the schema uses JSON columns, CHECK constraints and utf8mb4 collation, so
# SQLite would pass tests that production fails.

DATABASES = {
    "default": {
        "ENGINE": env("DB_ENGINE", default="django.db.backends.mysql"),
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="3306"),
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
        # Django's own TIME_ZONE for this connection. Set explicitly so that
        # __date / TruncDate lookups do not depend on the server's timezone
        # tables being loaded.
        "TIME_ZONE": "UTC",
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        "TEST": {
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_0900_ai_ci",
        },
    }
}

_db_ssl_ca = env("DB_SSL_CA", default="")
if _db_ssl_ca:
    DATABASES["default"]["OPTIONS"]["ssl"] = {"ca": _db_ssl_ca}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
# AD-2: user_accounts is the user model, employees is a 1:1 profile. This cannot
# be changed after the first migration without rebuilding the database.

AUTH_USER_MODEL = "accounts.UserAccount"

AUTHENTICATION_BACKENDS = [
    "social_core.backends.google.GoogleOAuth2",
    "social_core.backends.azuread_tenant.AzureADTenantOAuth2",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_RESET_TIMEOUT = env.int("PASSWORD_RESET_TIMEOUT", default=900)
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=604800)

# Login throttling (Phase 1.4)
LOGIN_MAX_FAILED_ATTEMPTS = env.int("LOGIN_MAX_FAILED_ATTEMPTS", default=5)
LOGIN_LOCKOUT_SECONDS = env.int("LOGIN_LOCKOUT_SECONDS", default=900)

# OTP (Phase 1.3)
# Second factor on local login. Defaults ON: turning it off is a deliberate act,
# not something that happens because a variable was forgotten.
REQUIRE_OTP_FOR_LOGIN = env.bool("REQUIRE_OTP_FOR_LOGIN", default=True)

# --- Risk scoring (Phase 5.4) -------------------------------------------------
# Residual score thresholds. A residual score >= "high" is High, >= "moderate"
# is Moderate, else Low. Config rather than code because the business owns these
# numbers; see apps/risk/scoring.py for the formula and where it came from.
# Days a plan may sit in Pending BU Lead Review before its lead is reminded.
REVIEW_REMINDER_DAYS = env.int("REVIEW_REMINDER_DAYS", default=5)

RISK_LEVEL_THRESHOLDS = {
    "moderate": env.float("RISK_LEVEL_MODERATE_THRESHOLD", default=5.0),
    "high": env.float("RISK_LEVEL_HIGH_THRESHOLD", default=7.0),
}
OTP_EXPIRY_SECONDS = env.int("OTP_EXPIRY_SECONDS", default=300)
OTP_LENGTH = env.int("OTP_LENGTH", default=6)
OTP_MAX_ATTEMPTS = env.int("OTP_MAX_ATTEMPTS", default=5)
OTP_RESEND_COOLDOWN_SECONDS = env.int("OTP_RESEND_COOLDOWN_SECONDS", default=60)


# --------------------------------------------------------------------------- #
# SSO (Phase 1.6 / 1.7)
# --------------------------------------------------------------------------- #
# Note: MS_ENTRA_* is the *login* app registration. It is deliberately separate
# from MS_GRAPH_* below, which is the client-credentials registration used for
# Teams calling in Phase 8.

GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
GOOGLE_OAUTH_REDIRECT_URI = env(
    "GOOGLE_OAUTH_REDIRECT_URI",
    default="http://localhost:5173/auth/google/callback",
)

MS_ENTRA_CLIENT_ID = env("MS_ENTRA_CLIENT_ID", default="")
MS_ENTRA_CLIENT_SECRET = env("MS_ENTRA_CLIENT_SECRET", default="")
MS_ENTRA_TENANT_ID = env("MS_ENTRA_TENANT_ID", default="common")
MS_ENTRA_REDIRECT_URI = env(
    "MS_ENTRA_REDIRECT_URI",
    default="http://localhost:5173/auth/microsoft/callback",
)

# Mirrors of the above for social_django's models, which stay installed for the
# UserSocialAuth linkage table even though the authorization-code flow is
# implemented directly in apps/accounts/sso.py.
SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = GOOGLE_OAUTH_CLIENT_ID
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = GOOGLE_OAUTH_CLIENT_SECRET
SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_KEY = MS_ENTRA_CLIENT_ID
SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_SECRET = MS_ENTRA_CLIENT_SECRET
SOCIAL_AUTH_AZUREAD_TENANT_OAUTH2_TENANT_ID = MS_ENTRA_TENANT_ID
SOCIAL_AUTH_JSONFIELD_ENABLED = True
SOCIAL_AUTH_PROTECTED_USER_FIELDS = ["email"]

#: OAuth state lives in the cache for this long, bounding a CSRF replay window.
OAUTH_STATE_TTL_SECONDS = env.int("OAUTH_STATE_TTL_SECONDS", default=600)


# --------------------------------------------------------------------------- #
# REST framework
# --------------------------------------------------------------------------- #

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPageNumberPagination",
    "PAGE_SIZE": env.int("API_PAGE_SIZE", default=25),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_THROTTLE_ANON", default="30/min"),
        "user": env("API_THROTTLE_USER", default="1000/hour"),
        "login": env("API_THROTTLE_LOGIN", default="10/min"),
    },
}

API_BASE_PATH = env("API_BASE_PATH", default="/api/v1")
API_MAX_PAGE_SIZE = env.int("API_MAX_PAGE_SIZE", default=200)
ENABLE_API_DOCS = env.bool("ENABLE_API_DOCS", default=True)

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        seconds=env.int("SIMPLE_JWT_ACCESS_TOKEN_LIFETIME_SECONDS", default=900)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        seconds=env.int("SIMPLE_JWT_REFRESH_TOKEN_LIFETIME_SECONDS", default=86400)
    ),
    "ROTATE_REFRESH_TOKENS": env.bool("SIMPLE_JWT_ROTATE_REFRESH_TOKENS", default=True),
    "BLACKLIST_AFTER_ROTATION": env.bool("SIMPLE_JWT_BLACKLIST_AFTER_ROTATION", default=True),
    "AUTH_HEADER_TYPES": ("Bearer",),
    # UserAccount's primary key is `user_id`, not `id`. SimpleJWT defaults to
    # "id" and fails with AttributeError on token creation without this.
    "USER_ID_FIELD": "user_id",
    "USER_ID_CLAIM": "user_id",
    # Falls back to SECRET_KEY when unset, so dev needs no extra config, but
    # staging/production can rotate JWT signing independently.
    "SIGNING_KEY": env("JWT_SIGNING_KEY", default="") or SECRET_KEY,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "BCM Application API",
    "DESCRIPTION": "Business Continuity Management platform",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}


# --------------------------------------------------------------------------- #
# CORS / CSRF
# --------------------------------------------------------------------------- #
# No CORS_ALLOW_ALL_ORIGINS anywhere — it silently voids the allow-list.

FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173")
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[FRONTEND_URL])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[FRONTEND_URL])
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=True)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=20)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@example.com")
SERVER_EMAIL = env("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
ADMINS = [("BCM Admin", e) for e in env.list("ADMIN_EMAILS", default=[])]
# When set, EVERY notification is delivered to this one address instead of its
# real recipients (the original To/CC are named at the top of the body and in
# an X-BCM-Original-To header). For test and demo environments where the plan's
# BU leads and coordinators are real addresses that must not be mailed. The
# delivery log keeps the real recipients. Must be empty in production.
NOTIFICATION_REDIRECT_TO = env("NOTIFICATION_REDIRECT_TO", default="").strip()


# --------------------------------------------------------------------------- #
# Storage / media (AD-7)
# --------------------------------------------------------------------------- #

STORAGE_BACKEND = env("STORAGE_BACKEND", default="local")
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = env("MEDIA_URL", default="/media/")
MEDIA_ROOT = BASE_DIR / env("MEDIA_ROOT", default="media")

# The public origin of the *backend*, used to build absolute media/download URLs.
# It is NOT the frontend URL — pointing it at the frontend 404s every download.
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", default="http://localhost:8000")

MAX_UPLOAD_SIZE_MB = env.int("MAX_UPLOAD_SIZE_MB", default=25)
ALLOWED_UPLOAD_EXTENSIONS = env.list(
    "ALLOWED_UPLOAD_EXTENSIONS",
    default=["pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"],
)

AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="")
AWS_S3_SIGNED_URL_EXPIRY_SECONDS = env.int("AWS_S3_SIGNED_URL_EXPIRY_SECONDS", default=600)


# --------------------------------------------------------------------------- #
# Call tree providers (Phase 8)
# --------------------------------------------------------------------------- #
# Both default to False. A misconfigured environment must be incapable of
# calling real people.

TWILIO_ENABLED = env.bool("TWILIO_ENABLED", default=False)
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", default="")
TWILIO_PHONE_NUMBER_ID = env("TWILIO_PHONE_NUMBER_ID", default="")
TWILIO_STATUS_CALLBACK_URL = env("TWILIO_STATUS_CALLBACK_URL", default="")
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", default="v22.0")

TEAMS_ENABLED = env.bool("TEAMS_ENABLED", default=False)
MS_GRAPH_TENANT_ID = env("MS_GRAPH_TENANT_ID", default="")
MS_GRAPH_CLIENT_ID = env("MS_GRAPH_CLIENT_ID", default="")
MS_GRAPH_CLIENT_SECRET = env("MS_GRAPH_CLIENT_SECRET", default="")
MS_GRAPH_SCOPE = env("MS_GRAPH_SCOPE", default="https://graph.microsoft.com/.default")
# Shared secret echoed back in Graph call notifications, so the Teams webhook can
# reject payloads that did not originate from our own call requests.
TEAMS_WEBHOOK_CLIENT_STATE = env("TEAMS_WEBHOOK_CLIENT_STATE", default="")

# Call tree execution (Phase 8)
CALL_TREE_RETRY_SECONDS = env.int("CALL_TREE_RETRY_SECONDS", default=60)
CALL_TREE_ATTEMPT_TIMEOUT_SECONDS = env.int("CALL_TREE_ATTEMPT_TIMEOUT_SECONDS", default=180)
CALL_TREE_RING_SECONDS = env.int("CALL_TREE_RING_SECONDS", default=30)
# Upcoming tests are announced this many days ahead.
TEST_REMINDER_DAYS = env.int("TEST_REMINDER_DAYS", default=2)


# --------------------------------------------------------------------------- #
# Celery / cache (AD-9)
# --------------------------------------------------------------------------- #

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://127.0.0.1:6379/1")
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"


# --------------------------------------------------------------------------- #
# i18n
# --------------------------------------------------------------------------- #

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

LOG_LEVEL = env("LOG_LEVEL", default="INFO")
# "console" for people, "json" for a log shipper. Production defaults to json.
LOG_FORMAT = env("LOG_FORMAT", default="console")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "apps.core.observability.RequestIdFilter"}},
    "formatters": {
        "console": {
            "format": "{levelname} {asctime} {name} [{request_id}] {message}",
            "style": "{",
        },
        "json": {"()": "apps.core.observability.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": LOG_FORMAT,
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "apps": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # One line per request, at INFO; silence it with LOG_LEVEL=WARNING.
        "apps.request": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# drf-spectacular's schema-typing warnings (SerializerMethodField without an
# explicit type) are about the OpenAPI document, not the running service. They
# are silenced so `check --deploy` reports only what blocks a release.
SILENCED_SYSTEM_CHECKS = ["drf_spectacular.W001", "drf_spectacular.W002"]

# --------------------------------------------------------------------------- #
# Observability (Phase 10.4)
# --------------------------------------------------------------------------- #

APP_VERSION = env("APP_VERSION", default="")
SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default="development")
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0)
# Shared secret for the Prometheus scrape of /api/v1/metrics/. Unset = endpoint absent.
METRICS_TOKEN = env("METRICS_TOKEN", default="")

# Web Push (browser notifications). Generate a pair with `manage.py generate_vapid_keys`;
# unset = the bell still works, browsers are simply never pushed to.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_CLAIMS_EMAIL = env("VAPID_CLAIMS_EMAIL", default="bcm@example.com")
# Profile pictures: resized server-side to this many pixels square.
AVATAR_SIZE_PX = 192

# Data retention in days (PENDING #16). None = keep forever. The compliance
# record (plans, answers, risks, approved documents, call tree runs, crisis
# events, tests) is never swept automatically.
RETENTION_OTP_CHALLENGE_DAYS = env.int("RETENTION_OTP_CHALLENGE_DAYS", default=7)
RETENTION_NOTIFICATION_LOG_DAYS = env.int("RETENTION_NOTIFICATION_LOG_DAYS", default=None)
RETENTION_USER_NOTIFICATION_DAYS = env.int("RETENTION_USER_NOTIFICATION_DAYS", default=None)
RETENTION_AUDIT_LOG_DAYS = env.int("RETENTION_AUDIT_LOG_DAYS", default=None)
RETENTION_REPORT_OUTPUT_DAYS = env.int("RETENTION_REPORT_OUTPUT_DAYS", default=None)

# Request size caps: the largest legitimate body is a report upload.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024 + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000

from apps.core.observability import init_sentry  # noqa: E402

init_sentry(sys.modules[__name__])
