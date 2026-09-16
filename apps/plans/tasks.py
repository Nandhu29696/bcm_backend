"""Scheduled plan jobs (Celery beat)."""

from celery import shared_task

from apps.plans.reminders import send_review_reminders as _send


@shared_task(name="apps.plans.tasks.send_review_reminders")
def send_review_reminders() -> int:
    return _send()
