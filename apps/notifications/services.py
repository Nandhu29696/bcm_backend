"""
Email dispatch with a delivery log (AD-8, Phase 6.6).

`send_notification` writes a `NotificationLog` row and hands delivery to the
`deliver_notification` Celery task. In development and tests the task runs
inline (CELERY_TASK_ALWAYS_EAGER), so callers see the final status on return;
in production it is queued and retried with backoff. Either way the contract
is the same: the log row is the record, and its idempotency key is what stops
a retry, a redelivery or a double-click from mailing anyone twice.
"""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings

from apps.core.tasks import enqueue_after_commit
from apps.notifications.models import DeliveryStatus, NotificationLog

logger = logging.getLogger(__name__)


def build_idempotency_key(event_type: str, entity_id, recipient: str, salt: str = "") -> str:
    """Stable key for (event, entity, recipient).

    A retry that re-enters dispatch collides on the unique constraint and
    short-circuits, so a Celery redelivery cannot mail a BU lead twice. `salt`
    is for events that legitimately repeat — a daily reminder, an OTP resend.
    """
    raw = f"{event_type}:{entity_id}:{recipient.strip().lower()}:{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def send_notification(
    *,
    event_type: str,
    to_email: str,
    subject: str,
    template_name: str,
    context: dict,
    cc_emails: list[str] | None = None,
    recipient_user=None,
    entity_type: str = "",
    entity_id=None,
    idempotency_key: str | None = None,
) -> NotificationLog:
    """Log, then deliver (asynchronously where a broker is configured).

    Never raises on a send failure — the failure is recorded on the log row and
    retried by the task. An OTP email that fails to send must not 500 the login
    request; the user needs to see "we couldn't send your code", not a stack trace.
    """
    from apps.notifications.tasks import deliver_notification

    cc_emails = cc_emails or []
    key = idempotency_key or build_idempotency_key(event_type, entity_id, to_email)

    existing = NotificationLog.objects.filter(idempotency_key=key).first()
    if existing is not None and existing.status == DeliveryStatus.SENT:
        logger.info("Notification %s already sent, skipping", key[:12])
        return existing

    context = {**context, "frontend_url": settings.FRONTEND_URL}

    log = existing or NotificationLog(idempotency_key=key)
    log.event_type = event_type
    log.to_email = to_email
    log.cc_emails = ",".join(cc_emails)
    log.recipient_user = recipient_user
    log.subject = subject
    log.template_name = template_name
    log.entity_type = entity_type
    log.entity_id = entity_id
    # Secrets are redacted before persisting — the rendered email still receives
    # the real values, only the stored copy is scrubbed.
    log.context = _redact(context)
    log.status = DeliveryStatus.PENDING
    log.save()

    # The real (unredacted) context travels with the task, not the row.
    #
    # With a broker, the task is enqueued after commit so a rolled-back
    # transaction cannot leave a worker delivering a notification about
    # something that never happened. In eager mode (development, tests) the
    # task runs inline on this same connection, so "after commit" has no
    # meaning - and waiting for one would mean nothing is ever sent inside a
    # test transaction.
    def enqueue():
        deliver_notification.delay(log.pk, _jsonable(context))

    def guarded():
        # Inline execution propagates an exhausted retry as an exception. The
        # task has already recorded FAILED on the row; the caller must not 500.
        try:
            enqueue()
        except Exception:  # noqa: BLE001 - recorded on the log row by the task
            logger.exception("Inline delivery of notification %s failed", key[:12])

    enqueue_after_commit(guarded)

    # The bell mirrors the email for every recipient that has an account.
    if existing is None:
        from apps.notifications.inapp import notify_users

        notify_users(
            log=log,
            event_type=event_type,
            emails=[to_email, *cc_emails],
            subject=subject,
            context=context,
        )

    log.refresh_from_db()
    return log


#: Context keys whose values must never be persisted.
#:
#: The delivery log is a debugging and audit aid, not a secret store. Writing an
#: OTP here in plaintext would defeat hashing it in `otp_challenges` entirely —
#: anyone with read access to the database could bypass the second factor. The
#: same goes for a password-reset URL, which carries a usable token in its query
#: string.
SENSITIVE_CONTEXT_KEYS = frozenset(
    {"code", "otp", "token", "password", "reset_url", "secret", "access", "refresh"}
)

REDACTED = "[redacted]"


def _redact(context: dict) -> dict:
    """Strip secrets before the context is written to the log."""
    return {
        key: (REDACTED if key.lower() in SENSITIVE_CONTEXT_KEYS else value)
        for key, value in context.items()
        if _is_jsonable(value)
    }


def _jsonable(context: dict) -> dict:
    """Only JSON-serialisable values can travel through the task queue."""
    return {key: value for key, value in context.items() if _is_jsonable(value)}


def _is_jsonable(value) -> bool:
    return isinstance(value, str | int | float | bool | type(None) | list | dict)
