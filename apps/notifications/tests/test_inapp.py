"""The bell and browser push: rows mirror emails, reads are per user, push is best effort."""

from unittest import mock

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.notifications import inapp
from apps.notifications.models import NotificationEvent, PushSubscription, UserNotification
from apps.notifications.services import send_notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(user_factory):
    return user_factory(email="lead@example.com", roles=["BCM_BU_LEAD"])


@pytest.fixture
def coordinator(user_factory):
    return user_factory(email="coord@example.com", roles=["BCM_COORDINATOR"])


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class TestBell:
    def test_email_recipients_with_accounts_get_a_bell_row(self, lead, coordinator):
        send_notification(
            event_type=NotificationEvent.PLAN_SUBMITTED,
            to_email="lead@example.com",
            cc_emails=["coord@example.com", "nobody@example.com"],
            subject="BCP plan CC-1 submitted for review",
            template_name="plan_submitted",
            context={"cost_code": "CC-1", "submitted_by": "Arun", "plan_version_id": 42},
            entity_type="PLAN_VERSION",
            entity_id=42,
        )
        rows = UserNotification.objects.order_by("user__email")
        assert [r.user.email for r in rows] == ["coord@example.com", "lead@example.com"]
        row = rows.get(user=lead)
        assert row.title == "BCP plan CC-1 submitted for review"
        assert row.body == "Submitted by Arun for your review." and row.link == "/plan-versions/42"
        assert row.read_at is None and row.category == "PLAN_SUBMITTED"

    def test_credentials_never_reach_the_bell(self, lead):
        send_notification(
            event_type=NotificationEvent.OTP_CODE,
            to_email="lead@example.com",
            subject="Your code",
            template_name="otp_code",
            context={"code": "123456", "display_name": "Lead", "expires_minutes": 5},
        )
        assert not UserNotification.objects.exists()

    def test_a_resent_notification_does_not_duplicate_the_row(self, lead):
        for _ in range(2):
            send_notification(
                event_type=NotificationEvent.REPORT_READY,
                to_email="lead@example.com",
                subject="Your report is ready",
                template_name="report_ready",
                context={
                    "report_name": "Estate detail",
                    "report_format": "XLSX",
                    "rows": 3,
                    "schedule": "Once",
                    "download_url": "x",
                    "expires_hours": 1,
                    "report_request_id": 1,
                },
                entity_type="REPORT_REQUEST",
                entity_id=1,
                idempotency_key="report:1:run:1",
            )
        assert UserNotification.objects.count() == 1
        assert UserNotification.objects.get().link == "/reports"

    def test_list_read_and_read_all_are_per_user(self, lead, coordinator):
        for user in (lead, coordinator):
            UserNotification.objects.create(
                user=user, category="PLAN_APPROVED", title=f"for {user.email}"
            )
        UserNotification.objects.create(user=lead, category="PLAN_REWORK", title="second")
        mine = client_for(lead).get(reverse("notifications:list")).data
        assert mine["unread"] == 2 and [r["title"] for r in mine["results"]] == [
            "second",
            "for lead@example.com",
        ]
        assert client_for(coordinator).get(reverse("notifications:unread-count")).data == {
            "unread": 1
        }

        first = mine["results"][0]["user_notification_id"]
        assert (
            client_for(coordinator).post(reverse("notifications:read", args=[first])).status_code
            == 404
        )
        marked = client_for(lead).post(reverse("notifications:read", args=[first]))
        assert marked.status_code == 200 and marked.data["read_at"] is not None
        assert (
            client_for(lead).get(reverse("notifications:list"), {"unread": "1"}).data["unread"] == 1
        )
        assert client_for(lead).post(reverse("notifications:read-all")).data == {"marked": 1}
        assert client_for(coordinator).get(reverse("notifications:unread-count")).data == {
            "unread": 1
        }


class TestPush:
    def test_public_key_reports_configuration(self, lead, settings):
        settings.VAPID_PUBLIC_KEY = ""
        settings.VAPID_PRIVATE_KEY = ""
        assert (
            client_for(lead).get(reverse("notifications:push-public-key")).data["configured"]
            is False
        )
        refused = client_for(lead).post(
            reverse("notifications:push-subscribe"),
            {"endpoint": "https://push.example.com/a", "keys": {"p256dh": "p", "auth": "a"}},
            format="json",
        )
        assert refused.status_code == 400 and refused.data["code"] == "push_not_configured"

    def test_subscribe_is_idempotent_per_endpoint_and_unsubscribe_removes_it(self, lead, settings):
        settings.VAPID_PUBLIC_KEY = "pub"
        settings.VAPID_PRIVATE_KEY = "priv"
        body = {
            "endpoint": "https://push.example.com/" + "x" * 700,
            "keys": {"p256dh": "p", "auth": "a"},
        }
        for _ in range(2):
            response = client_for(lead).post(
                reverse("notifications:push-subscribe"), body, format="json"
            )
            assert response.status_code == 201 and response.data["devices"] == 1
        assert PushSubscription.objects.count() == 1
        removed = client_for(lead).post(
            reverse("notifications:push-unsubscribe"), {"endpoint": body["endpoint"]}, format="json"
        )
        assert removed.data == {"removed": 1, "devices": 0}

    def test_push_is_sent_per_device_and_dead_subscriptions_are_dropped(self, lead, settings):
        settings.VAPID_PUBLIC_KEY = "pub"
        settings.VAPID_PRIVATE_KEY = "priv"
        inapp.subscribe(lead, endpoint="https://push.example.com/alive", p256dh="p", auth="a")
        inapp.subscribe(lead, endpoint="https://push.example.com/gone", p256dh="p", auth="a")
        row = UserNotification.objects.create(
            user=lead, category="PLAN_APPROVED", title="Approved", link="/plan-versions/1"
        )

        from pywebpush import WebPushException

        def fake_webpush(subscription_info, **kwargs):
            if subscription_info["endpoint"].endswith("gone"):
                raise WebPushException("gone", response=mock.Mock(status_code=410))
            return mock.Mock(status_code=201)

        with mock.patch("pywebpush.webpush", side_effect=fake_webpush) as sender:
            assert inapp.deliver_push(row) == 1
        assert sender.call_count == 2
        assert [s.endpoint.rsplit("/", 1)[1] for s in PushSubscription.objects.all()] == ["alive"]
        payload = sender.call_args_list[0].kwargs["data"]
        assert '"title": "Approved"' in payload and '"link": "/plan-versions/1"' in payload

    def test_push_failure_leaves_the_bell_row(self, lead, settings):
        settings.VAPID_PUBLIC_KEY = "pub"
        settings.VAPID_PRIVATE_KEY = "priv"
        inapp.subscribe(lead, endpoint="https://push.example.com/flaky", p256dh="p", auth="a")
        with mock.patch("pywebpush.webpush", side_effect=RuntimeError("network")):
            send_notification(
                event_type=NotificationEvent.PLAN_APPROVED,
                to_email="lead@example.com",
                subject="Approved",
                template_name="plan_approved",
                context={"cost_code": "CC-1", "plan_version_id": 1},
                entity_type="PLAN_VERSION",
                entity_id=1,
            )
        assert UserNotification.objects.filter(user=lead).count() == 1
        assert PushSubscription.objects.count() == 1


class TestAdminResend:
    @pytest.fixture
    def admin_client(self, user_factory):
        return client_for(user_factory(email="admin-n@example.com", roles=["BCM_ADMIN"]))

    def test_failed_notification_is_listed_and_resent(self, admin_client, lead, monkeypatch):
        from django.core import mail

        from apps.notifications import tasks
        from apps.notifications.models import NotificationLog

        # The first delivery fails every attempt; the resend succeeds.
        original = tasks._attempt
        calls = {"n": 0}

        def flaky(log, ctx):
            calls["n"] += 1
            if calls["n"] <= tasks.MAX_ATTEMPTS:
                log.status = "FAILED"
                log.error_detail = "SMTP down"
                log.attempts += 1
                log.save()
                return RuntimeError("SMTP down")
            return original(log, ctx)

        monkeypatch.setattr(tasks, "_attempt", flaky)
        send_notification(
            event_type=NotificationEvent.PLAN_APPROVED,
            to_email="lead@example.com",
            subject="Approved",
            template_name="plan_approved",
            context={"cost_code": "CC-1", "plan_version_id": 1, "bu_lead_name": "Lead"},
            entity_type="PLAN_VERSION",
            entity_id=1,
        )
        log = NotificationLog.objects.get(event_type="PLAN_APPROVED")
        assert log.status == "FAILED"

        listing = admin_client.get(reverse("notifications:admin-log"), {"status": "FAILED"})
        assert listing.status_code == 200
        assert [r["notification_log_id"] for r in listing.data["results"]] == [log.pk]
        assert listing.data["results"][0]["resendable"] is True
        assert listing.data["counts"]["FAILED"] == 1

        resent = admin_client.post(reverse("notifications:admin-resend", args=[log.pk]))
        assert resent.status_code == 200, resent.data
        assert resent.data["status"] == "SENT"
        assert mail.outbox[-1].to == ["lead@example.com"]
        again = admin_client.post(reverse("notifications:admin-resend", args=[log.pk]))
        assert again.status_code == 409

    def test_credentials_cannot_be_resent_and_non_admins_are_refused(self, admin_client, lead):
        from apps.notifications.models import NotificationLog

        otp = NotificationLog.objects.create(
            event_type="OTP_CODE",
            to_email="lead@example.com",
            idempotency_key="otp-1",
            status="FAILED",
        )
        refused = admin_client.post(reverse("notifications:admin-resend", args=[otp.pk]))
        assert refused.status_code == 400 and refused.data["code"] == "not_resendable"
        assert client_for(lead).get(reverse("notifications:admin-log")).status_code == 403
        assert (
            client_for(lead).post(reverse("notifications:admin-resend", args=[otp.pk])).status_code
            == 403
        )
