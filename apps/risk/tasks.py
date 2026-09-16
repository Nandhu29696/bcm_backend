"""Scheduled risk jobs (Celery beat)."""

from celery import shared_task

from apps.risk.reminders import send_overdue_reminders


@shared_task(name="apps.risk.tasks.send_risk_action_reminders")
def send_risk_action_reminders() -> int:
    return send_overdue_reminders()
