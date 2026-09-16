"""
Upcoming test reminders (Phase 8).

Tests scheduled TEST_REMINDER_DAYS from today get their coordinators an email
with the BU lead in copy. Idempotent per (test, recipient, day).
"""

from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.utils import timezone

from apps.notifications.models import DeliveryStatus, NotificationEvent, NotificationLog
from apps.notifications.services import build_idempotency_key, send_notification
from apps.plans.workflow import _plan_context, bu_lead_email, coordinator_emails
from apps.testing.models import Test

logger = logging.getLogger(__name__)


def upcoming_tests(today: dt.date | None = None):
    today = today or timezone.now().date()
    target = today + dt.timedelta(days=settings.TEST_REMINDER_DAYS)
    return Test.objects.filter(status=Test.Status.SCHEDULED, scheduled_date=target).select_related(
        "plan_version__plan__cost_code__bu_lead",
        "plan_version__plan__cost_code__process",
        "plan_version__plan__cost_code__estate",
    )


def send_test_reminders(today: dt.date | None = None) -> int:
    today = today or timezone.now().date()
    sent = 0
    for test in upcoming_tests(today):
        version = test.plan_version
        recipients = coordinator_emails(version)
        lead = bu_lead_email(version)
        if not recipients and lead:
            recipients, lead = [lead], ""
        for recipient in recipients:
            key = build_idempotency_key(
                NotificationEvent.TEST_REMINDER, test.pk, recipient, salt=today.isoformat()
            )
            if NotificationLog.objects.filter(
                idempotency_key=key, status=DeliveryStatus.SENT
            ).exists():
                continue
            log = send_notification(
                event_type=NotificationEvent.TEST_REMINDER,
                to_email=recipient,
                cc_emails=[lead] if lead else [],
                subject=f"Reminder: {test.test_type} for {version.plan.cost_code.cost_code} on {test.scheduled_date:%d %b %Y}",
                template_name="test_reminder",
                context={
                    **_plan_context(version),
                    "test_id": test.pk,
                    "test_type": test.test_type,
                    "scheduled_date": test.scheduled_date.isoformat(),
                    "scheduled_time": (
                        test.scheduled_time.strftime("%H:%M") if test.scheduled_time else ""
                    ),
                    "days_ahead": settings.TEST_REMINDER_DAYS,
                },
                entity_type="TEST",
                entity_id=test.pk,
                idempotency_key=key,
            )
            if log.status == DeliveryStatus.SENT:
                sent += 1
    logger.info("Test reminders: %s sent for %s", sent, today)
    return sent
