"""Notification dispatch, logging and secret redaction."""

import pytest
from django.core import mail

from apps.notifications.models import DeliveryStatus, NotificationEvent, NotificationLog
from apps.notifications.services import REDACTED, build_idempotency_key, send_notification

pytestmark = pytest.mark.django_db


def send(**overrides):
    payload = {
        "event_type": NotificationEvent.OTP_CODE,
        "to_email": "person@example.com",
        "subject": "Your code",
        "template_name": "otp_code",
        "context": {"display_name": "Person", "code": "123456", "expiry_minutes": 5},
    }
    payload.update(overrides)
    return send_notification(**payload)


def test_send_delivers_and_logs():
    mail.outbox.clear()
    log = send()

    assert log.status == DeliveryStatus.SENT
    assert log.sent_at is not None
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["person@example.com"]


def test_redirect_sends_everything_to_one_mailbox(settings):
    """NOTIFICATION_REDIRECT_TO: the test mailbox gets the mail, the log keeps
    the real recipients, and the body says who it was for."""
    settings.NOTIFICATION_REDIRECT_TO = "inbox@example.com"
    mail.outbox.clear()
    log = send(cc_emails=["copy@example.com"])

    assert log.status == DeliveryStatus.SENT
    assert log.to_email == "person@example.com"
    assert log.cc_emails == "copy@example.com"
    sent = mail.outbox[0]
    assert sent.to == ["inbox@example.com"]
    assert sent.cc == []
    assert sent.extra_headers["X-BCM-Original-To"] == (
        "To: person@example.com; CC: copy@example.com"
    )
    assert sent.body.startswith(
        "[Redirected by NOTIFICATION_REDIRECT_TO. Originally To: person@example.com"
    )


def test_no_redirect_by_default(settings):
    settings.NOTIFICATION_REDIRECT_TO = ""
    mail.outbox.clear()
    send(cc_emails=["copy@example.com"])
    assert mail.outbox[0].to == ["person@example.com"]
    assert mail.outbox[0].cc == ["copy@example.com"]
    assert "X-BCM-Original-To" not in mail.outbox[0].extra_headers


def test_otp_code_is_redacted_in_the_log_but_present_in_the_email():
    """Regression: the delivery log must not store live secrets.

    Persisting the OTP in plaintext would defeat hashing it in `otp_challenges`
    — read access to the database would be enough to bypass the second factor.
    """
    mail.outbox.clear()
    log = send()

    assert log.context["code"] == REDACTED
    assert "123456" not in str(log.context)
    # The recipient still gets the real code.
    assert "123456" in mail.outbox[0].body


def test_reset_url_is_redacted():
    """A reset URL carries a usable token in its query string."""
    log = send(
        event_type=NotificationEvent.PASSWORD_RESET,
        template_name="password_reset",
        context={
            "display_name": "Person",
            "reset_url": "http://localhost:5173/reset-password?uid=abc&token=secret",
            "expiry_minutes": 15,
        },
    )
    assert log.context["reset_url"] == REDACTED
    assert "secret" not in str(log.context)


def test_non_sensitive_context_is_kept():
    log = send()
    assert log.context["display_name"] == "Person"
    assert log.context["expiry_minutes"] == 5


def test_repeat_send_with_the_same_key_does_not_resend():
    """Idempotency: a Celery retry must not mail a BU lead twice."""
    mail.outbox.clear()
    key = build_idempotency_key("TEST_EVENT", 1, "person@example.com")

    first = send(idempotency_key=key)
    second = send(idempotency_key=key)

    assert first.pk == second.pk
    assert len(mail.outbox) == 1
    assert NotificationLog.objects.count() == 1


def test_send_failure_is_recorded_not_raised(monkeypatch):
    """An OTP email that fails must not 500 the login request."""

    def explode(*args, **kwargs):
        raise OSError("smtp is down")

    monkeypatch.setattr("django.core.mail.EmailMultiAlternatives.send", explode, raising=True)

    log = send()
    assert log.status == DeliveryStatus.FAILED
    assert "smtp is down" in log.error_detail
    # Every attempt was made and recorded before giving up.
    from apps.notifications.tasks import MAX_ATTEMPTS

    assert log.attempts == MAX_ATTEMPTS


def test_idempotency_key_is_stable_and_case_insensitive_on_email():
    a = build_idempotency_key("EVENT", 7, "Person@Example.com")
    b = build_idempotency_key("EVENT", 7, "person@example.com")
    c = build_idempotency_key("EVENT", 8, "person@example.com")

    assert a == b
    assert a != c


def test_a_transient_failure_is_retried_without_a_double_send(monkeypatch):
    """Phase 6 exit criterion: a failed SMTP send retries and never sends twice.

    The first attempt raises, the retry succeeds. Exactly one email leaves, the
    log shows both attempts, and a further call with the same key is a no-op.
    """
    from django.core.mail import EmailMultiAlternatives

    mail.outbox.clear()
    calls = {"n": 0}
    real_send = EmailMultiAlternatives.send

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("smtp hiccup")
        return real_send(self, *args, **kwargs)

    monkeypatch.setattr(EmailMultiAlternatives, "send", flaky)

    log = send(idempotency_key="retry-once")
    assert log.status == DeliveryStatus.SENT
    assert log.attempts == 2
    assert len(mail.outbox) == 1

    again = send(idempotency_key="retry-once")
    assert again.pk == log.pk
    assert len(mail.outbox) == 1


def test_delivery_runs_through_the_task(monkeypatch):
    """The service must not send directly; the task is the single delivery path."""
    from apps.notifications import tasks

    seen = []
    original = tasks.deliver_notification.run

    def spy(self, log_id, ctx):
        seen.append(log_id)
        return original(log_id, ctx)

    monkeypatch.setattr(tasks.deliver_notification, "run", spy.__get__(tasks.deliver_notification))
    log = send(idempotency_key="via-task")
    assert seen == [log.pk]
