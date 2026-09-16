"""Scheduled test jobs (Celery beat)."""

from celery import shared_task

from apps.testing.reminders import send_test_reminders as _send


@shared_task(name="apps.testing.tasks.send_test_reminders")
def send_test_reminders() -> int:
    return _send()
