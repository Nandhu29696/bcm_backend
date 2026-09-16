"""
Notification delivery as a Celery task (Phase 6.6).

`send_notification` writes the log row and enqueues `deliver_notification`;
this task renders and sends. A failed send is retried with exponential
backoff, and because every attempt re-reads the row and stops if it is already
SENT, a retry — or a redelivered message — can never mail anyone twice.

Retries are done two ways on purpose. With a worker, `self.retry()` hands the
attempt back to Celery with a countdown. In eager mode (development, tests)
Celery does not re-execute a retried task — it raises `Retry` to the caller —
so the task loops over its attempts itself, without the countdown. Same
attempt count, same bookkeeping, same outcome; only the waiting differs.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.notifications.models import DeliveryStatus, NotificationLog

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (30, 120, 300, 900)


def _attempt(log: NotificationLog, render_context: dict) -> Exception | None:
    """One delivery attempt. Returns the exception on failure, None on success."""
    log.attempts = (log.attempts or 0) + 1
    log.save(update_fields=["attempts"])
    try:
        # using="text_email" selects the non-autoescaping engine. Rendering a
        # text/plain body through the default engine turns "&" into "&amp;" and
        # breaks every multi-parameter URL in it.
        body = render_to_string(
            f"notifications/{log.template_name}.txt", render_context, using="text_email"
        )
        to = [log.to_email]
        cc = [address for address in log.cc_emails.split(",") if address]
        headers = {}
        if settings.NOTIFICATION_REDIRECT_TO:
            # Test/demo: one mailbox receives everything. Say who it was for, so
            # the reader can tell a BU lead's copy from a coordinator's.
            original = f"To: {', '.join(to)}" + (f"; CC: {', '.join(cc)}" if cc else "")
            headers["X-BCM-Original-To"] = original
            body = f"[Redirected by NOTIFICATION_REDIRECT_TO. Originally {original}]\n\n{body}"
            to, cc = [settings.NOTIFICATION_REDIRECT_TO], []
        message = EmailMultiAlternatives(
            subject=log.subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
            cc=cc,
            headers=headers,
        )
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - recorded, then retried
        logger.warning(
            "Send of %s to %s failed on attempt %s: %s",
            log.event_type,
            log.to_email,
            log.attempts,
            exc,
        )
        log.status = DeliveryStatus.FAILED
        log.error_detail = str(exc)[:2000]
        log.save(update_fields=["status", "error_detail"])
        return exc

    log.status = DeliveryStatus.SENT
    log.sent_at = timezone.now()
    log.error_detail = ""
    log.save(update_fields=["status", "sent_at", "error_detail"])
    return None


@shared_task(
    bind=True, name="apps.notifications.tasks.deliver_notification", max_retries=MAX_ATTEMPTS - 1
)
def deliver_notification(self, log_id: int, render_context: dict) -> str:
    """Send one logged notification. Returns the final status."""
    log = NotificationLog.objects.filter(pk=log_id).first()
    if log is None:
        logger.warning("Notification %s vanished before delivery", log_id)
        return "missing"
    if log.status == DeliveryStatus.SENT:
        # A retry or a duplicate delivery of the message: nothing to do.
        return DeliveryStatus.SENT

    if self.request.is_eager:
        for _ in range(MAX_ATTEMPTS):
            if _attempt(log, render_context) is None:
                return DeliveryStatus.SENT
        logger.error("Notification %s failed after %s attempts", log_id, MAX_ATTEMPTS)
        return DeliveryStatus.FAILED

    exc = _attempt(log, render_context)
    if exc is None:
        return DeliveryStatus.SENT
    if self.request.retries >= self.max_retries:
        logger.error("Notification %s failed after %s attempts", log_id, MAX_ATTEMPTS)
        return DeliveryStatus.FAILED
    countdown = BACKOFF_SECONDS[min(self.request.retries, len(BACKOFF_SECONDS) - 1)]
    raise self.retry(exc=exc, countdown=countdown)
