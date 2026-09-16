"""Retention: configured periods sweep, unset periods keep, the compliance record is untouched."""

import datetime as dt

import pytest
from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.retention import apply_retention
from apps.notifications.models import NotificationLog, UserNotification

pytestmark = pytest.mark.django_db


def age(instance, days):
    type(instance).objects.filter(pk=instance.pk).update(
        created_at=timezone.now() - dt.timedelta(days=days)
    )


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


class TestRetention:
    def test_unset_periods_keep_everything(self, settings, org, actor):
        settings.RETENTION_NOTIFICATION_LOG_DAYS = None
        settings.RETENTION_AUDIT_LOG_DAYS = None
        settings.RETENTION_USER_NOTIFICATION_DAYS = None
        settings.RETENTION_REPORT_OUTPUT_DAYS = None
        settings.RETENTION_OTP_CHALLENGE_DAYS = None
        log = NotificationLog.objects.create(
            event_type="PLAN_APPROVED",
            to_email="a@example.com",
            idempotency_key="k1",
            status="SENT",
        )
        age(log, 3000)
        assert apply_retention() == {}
        assert NotificationLog.objects.filter(pk=log.pk).exists()

    def test_configured_periods_sweep_only_older_rows(self, settings, org, actor):
        settings.RETENTION_NOTIFICATION_LOG_DAYS = 30
        settings.RETENTION_AUDIT_LOG_DAYS = 90
        settings.RETENTION_USER_NOTIFICATION_DAYS = 10
        old_log = NotificationLog.objects.create(
            event_type="PLAN_APPROVED",
            to_email="a@example.com",
            idempotency_key="old",
            status="SENT",
        )
        new_log = NotificationLog.objects.create(
            event_type="PLAN_APPROVED",
            to_email="a@example.com",
            idempotency_key="new",
            status="SENT",
        )
        age(old_log, 31)
        old_audit = AuditLog.objects.create(action="LOGIN_SUCCESS", actor=actor)
        age(old_audit, 91)
        recent_audit = AuditLog.objects.create(action="LOGIN_SUCCESS", actor=actor)
        bell = UserNotification.objects.create(user=actor, category="PLAN_APPROVED", title="x")
        age(bell, 11)

        preview = apply_retention(dry_run=True)
        assert (
            preview["notification_log"] == 1
            and preview["audit_log"] == 1
            and preview["user_notifications"] == 1
        )
        assert NotificationLog.objects.count() == 2  # dry run changed nothing

        applied = apply_retention()
        assert applied["notification_log"] == 1
        assert not NotificationLog.objects.filter(pk=old_log.pk).exists()
        assert NotificationLog.objects.filter(pk=new_log.pk).exists()
        assert not AuditLog.objects.filter(pk=old_audit.pk).exists()
        assert AuditLog.objects.filter(pk=recent_audit.pk).exists()
        assert not UserNotification.objects.filter(pk=bell.pk).exists()

    def test_report_outputs_and_their_files_go_but_shared_files_stay(
        self, settings, org, actor, approved_version
    ):
        from apps.documents.models import Document, EntityDocument
        from apps.reporting import services
        from apps.reporting.models import ReportRequest

        settings.RETENTION_REPORT_OUTPUT_DAYS = 30
        request = services.create_request(
            user=actor,
            report_type="EXEMPTION_REGISTER",
            report_format="csv",
            parameters={},
            schedule="ONCE",
        )
        request.refresh_from_db()
        document = request.document
        path = settings.MEDIA_ROOT / document.storage_key
        assert path.exists()
        ReportRequest.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - dt.timedelta(days=40),
            last_run_at=timezone.now() - dt.timedelta(days=40),
        )

        applied = apply_retention()
        assert applied["report_requests"] == 1 and applied["report_documents"] == 1
        assert not ReportRequest.objects.filter(pk=request.pk).exists()
        assert not Document.objects.filter(pk=document.pk).exists()
        assert not path.exists()

        # A file still attached elsewhere is kept.
        again = services.create_request(
            user=actor,
            report_type="EXEMPTION_REGISTER",
            report_format="csv",
            parameters={},
            schedule="ONCE",
        )
        again.refresh_from_db()
        EntityDocument.objects.create(
            document=again.document,
            entity_type="HELP_RESOURCE",
            entity_id=1,
            document_type="HELP_DOCUMENT",
        )
        ReportRequest.objects.filter(pk=again.pk).update(
            created_at=timezone.now() - dt.timedelta(days=40),
            last_run_at=timezone.now() - dt.timedelta(days=40),
        )
        applied = apply_retention()
        assert applied["report_requests"] == 1 and applied["report_documents"] == 0
        assert Document.objects.filter(pk=again.document_id).exists()

    def test_the_compliance_record_is_never_swept(self, settings, org, actor, approved_version):
        from apps.plans.models import PlanVersion

        settings.RETENTION_AUDIT_LOG_DAYS = 1
        settings.RETENTION_NOTIFICATION_LOG_DAYS = 1
        PlanVersion.objects.filter(pk=approved_version.pk).update(
            created_at=timezone.now() - dt.timedelta(days=5000)
        )
        apply_retention()
        assert PlanVersion.objects.filter(pk=approved_version.pk).exists()
