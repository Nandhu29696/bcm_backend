"""
Celery application (AD-9).

Everything slow or retryable runs here: notification delivery, document
generation, scheduled reminders. Development and tests run with
CELERY_TASK_ALWAYS_EAGER=True so tasks execute inline and no broker is needed;
the code path is identical, only the transport changes.
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bcm_backend.settings.development")

app = Celery("bcm_backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Scheduled jobs. Each is idempotent per day, so a beat that fires twice - or a
# manual run after the scheduled one - sends nothing twice.
app.conf.beat_schedule = {
    "review-reminders-daily": {
        "task": "apps.plans.tasks.send_review_reminders",
        "schedule": crontab(hour=8, minute=0),
    },
    "risk-action-reminders-daily": {
        "task": "apps.risk.tasks.send_risk_action_reminders",
        "schedule": crontab(hour=8, minute=15),
    },
    "test-reminders-daily": {
        "task": "apps.testing.tasks.send_test_reminders",
        "schedule": crontab(hour=8, minute=30),
    },
    "scheduled-reports-hourly": {
        "task": "apps.reporting.tasks.run_scheduled_reports",
        "schedule": crontab(minute=5),
    },
    "retention-weekly": {
        "task": "apps.core.tasks.apply_retention",
        "schedule": crontab(day_of_week="sun", hour=2, minute=0),
    },
    # Expires call attempts a provider never reported on, so a run cannot stall.
    "call-tree-sweep": {
        "task": "apps.calltree.tasks.sweep_running_runs",
        # Read from the environment, not settings: this module is imported while
        # the settings package is still initialising.
        "schedule": float(os.environ.get("CALL_TREE_RETRY_SECONDS", "60")),
    },
}