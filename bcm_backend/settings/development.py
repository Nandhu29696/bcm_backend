"""Local development settings."""

from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)

# Email is FORCED to the console backend here, ignoring EMAIL_BACKEND.
#
# Reading it from the environment was a mistake: a `.env` carrying real SMTP
# credentials — which is the normal state of a developer's machine — silently
# turned local logins into live outbound mail. That actually happened during
# Phase 1 verification: an OTP was delivered through Gmail from a dev run.
#
# Reaching a real mail server from development now requires deliberately setting
# DEV_ALLOW_REAL_EMAIL=True, which nobody does by accident.
if env.bool("DEV_ALLOW_REAL_EMAIL", default=False):
    EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Celery runs inline so contributors do not need Redis to start.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)

INTERNAL_IPS = ["127.0.0.1"]
