"""
In-app notifications and Web Push.

`notify_users` is called by `send_notification` for every recipient (TO and
CC) that is a known account, so the bell shows exactly what the email said.
Transactional messages a person must not see twice - one-time codes, password
resets - are excluded: they are credentials, not news.

Web Push (browser notifications when the tab is closed) is best effort: a
subscription the browser has dropped (404/410 from the push service) is
deleted; any other failure is logged and the in-app row stands.
"""

from __future__ import annotations

import hashlib
import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.tasks import enqueue_after_commit
from apps.notifications.models import NotificationEvent, PushSubscription, UserNotification

logger = logging.getLogger(__name__)

#: Never surfaced in the bell or as a push.
SILENT_EVENTS = frozenset({NotificationEvent.OTP_CODE, NotificationEvent.PASSWORD_RESET})

#: Frontend path for a notification, from the entity it concerns.
ENTITY_LINKS = {
    "PLAN_VERSION": "/plan-versions/{id}",
    "TEST": "/tests/{id}",
    "CRISIS_EVENT": "/crisis/{id}",
    "CALL_TREE_RUN": "/crisis",
    "REPORT_REQUEST": "/reports",
    "EXEMPTION": "/reviews",
}


def link_for(entity_type: str, entity_id, context: dict) -> str:
    if entity_type == "EXEMPTION" and context.get("plan_version_id"):
        return f"/plan-versions/{context['plan_version_id']}"
    template = ENTITY_LINKS.get(entity_type or "")
    if not template:
        return ""
    return (
        template.format(id=entity_id)
        if "{id}" in template and entity_id
        else template.replace("{id}", "")
    )


def summary_for(event_type: str, subject: str, context: dict) -> tuple[str, str]:
    """Title and body for the bell: the email subject, and the one line that matters."""
    cost_code = context.get("cost_code", "")
    if event_type == NotificationEvent.PLAN_REWORK:
        body = context.get("comments") or "Sent back for rework."
    elif event_type == NotificationEvent.PLAN_SUBMITTED:
        body = f"Submitted by {context.get('submitted_by', 'the coordinator')} for your review."
    elif event_type == NotificationEvent.PLAN_APPROVED:
        body = "Approved. The plan document is being generated."
    elif event_type == NotificationEvent.CRISIS_INITIATED:
        body = context.get("comments") or f"{context.get('event_type', 'Crisis')} declared."
    elif event_type == NotificationEvent.REPORT_READY:
        body = f"{context.get('report_name', 'Report')} ({context.get('report_format', '')}) is ready to download."
    elif event_type == NotificationEvent.TEST_REMINDER:
        body = f"{context.get('test_type', 'Test')} on {context.get('scheduled_date', '')}."
    elif event_type == NotificationEvent.REVIEW_REMINDER:
        body = f"Waiting {context.get('days_pending', '')} days for your review."
    elif event_type == NotificationEvent.RISK_ACTION_OVERDUE:
        body = context.get("risk_name") or "A risk action is overdue."
    else:
        body = cost_code
    return subject[:300], (body or "")[:1000]


def notify_users(
    *, log, event_type: str, emails: list[str], subject: str, context: dict
) -> list[UserNotification]:
    """Create bell rows for every recipient that is an account; queue a push for each."""
    if event_type in SILENT_EVENTS or not emails:
        return []
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(email__in=[e for e in emails if e], is_active=True)
    if not users:
        return []
    title, body = summary_for(event_type, subject, context)
    link = link_for(log.entity_type, log.entity_id, context)
    rows = [
        UserNotification.objects.create(
            user=user, category=event_type, title=title, body=body, link=link, source_log=log
        )
        for user in users
    ]
    for row in rows:
        row_id = row.pk
        enqueue_after_commit(lambda row_id=row_id: send_web_push.delay(row_id))
    return rows


def push_configured() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def deliver_push(notification: UserNotification) -> int:
    """Send one notification to every subscription the user has. Returns the number sent."""
    if not push_configured():
        return 0
    from pywebpush import WebPushException, webpush

    payload = json.dumps(
        {
            "title": notification.title,
            "body": notification.body,
            "link": notification.link,
            "id": notification.pk,
            "category": notification.category,
        }
    )
    sent = 0
    for sub in PushSubscription.objects.filter(user=notification.user):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{settings.VAPID_CLAIMS_EMAIL}"},
                ttl=3600,
            )
            sub.last_used_at = timezone.now()
            sub.save(update_fields=["last_used_at"])
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                logger.info("Push subscription %s is gone; removing", sub.pk)
                sub.delete()
            else:
                logger.warning("Web push to subscription %s failed: %s", sub.pk, exc)
        except Exception:  # noqa: BLE001 - push is best effort; the bell row stands
            logger.exception("Web push to subscription %s failed", sub.pk)
    return sent


@transaction.atomic
def subscribe(
    user, *, endpoint: str, p256dh: str, auth: str, user_agent: str = ""
) -> PushSubscription:
    sub, _ = PushSubscription.objects.update_or_create(
        endpoint_hash=endpoint_hash(endpoint),
        defaults={
            "user": user,
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": user_agent[:300],
        },
    )
    return sub


def unsubscribe(user, *, endpoint: str) -> int:
    deleted, _ = PushSubscription.objects.filter(
        user=user, endpoint_hash=endpoint_hash(endpoint)
    ).delete()
    return deleted


from celery import shared_task  # noqa: E402


@shared_task(name="apps.notifications.inapp.send_web_push")
def send_web_push(notification_id: int) -> int:
    notification = (
        UserNotification.objects.select_related("user").filter(pk=notification_id).first()
    )
    if notification is None:
        return 0
    return deliver_push(notification)
