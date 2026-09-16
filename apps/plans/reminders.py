"""
Escalation reminders for reviews left pending (Phase 6.7).

A version in Pending BU Lead Review for longer than REVIEW_REMINDER_DAYS gets
its BU lead a reminder once a day, with the coordinators in copy. Idempotent
per (version, recipient, day) through the notification log.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.utils import timezone

from apps.notifications.models import DeliveryStatus, NotificationEvent, NotificationLog
from apps.notifications.services import build_idempotency_key, send_notification
from apps.plans.models import PlanStatus, PlanVersion
from apps.plans.workflow import _plan_context, bu_lead_email, coordinator_emails

logger = logging.getLogger(__name__)


def overdue_reviews(today: dt.date | None = None):
    today = today or timezone.now().date()
    cutoff = timezone.make_aware(
        dt.datetime.combine(today - dt.timedelta(days=settings.REVIEW_REMINDER_DAYS), dt.time.min)
    )
    return (
        PlanVersion.objects.filter(status=PlanStatus.PENDING_BU_LEAD_REVIEW, updated_at__lte=cutoff)
        .select_related(
            "plan__cost_code__bu_lead", "plan__cost_code__process", "plan__cost_code__estate"
        )
        .order_by("updated_at")
    )


def send_review_reminders(today: dt.date | None = None) -> int:
    today = today or timezone.now().date()
    sent = 0
    for version in overdue_reviews(today):
        recipient = bu_lead_email(version)
        if not recipient:
            continue
        key = build_idempotency_key(
            NotificationEvent.REVIEW_REMINDER, version.pk, recipient, salt=today.isoformat()
        )
        if NotificationLog.objects.filter(idempotency_key=key, status=DeliveryStatus.SENT).exists():
            continue
        days_pending = (today - version.updated_at.date()).days
        log = send_notification(
            event_type=NotificationEvent.REVIEW_REMINDER,
            to_email=recipient,
            cc_emails=coordinator_emails(version),
            subject=f"Reminder: BCP plan for {version.plan.cost_code.cost_code} awaits your review",
            template_name="review_reminder",
            context={**_plan_context(version), "days_pending": days_pending},
            entity_type="PLAN_VERSION",
            entity_id=version.pk,
            idempotency_key=key,
        )
        if log.status == DeliveryStatus.SENT:
            sent += 1
    logger.info("Review reminders: %s sent for %s", sent, today)
    return sent
