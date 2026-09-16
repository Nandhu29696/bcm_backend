"""
Data retention (PENDING #16).

The business has not yet set retention periods, so the defaults keep
everything except spent one-time codes. Each period is a setting
(`RETENTION_*_DAYS`; empty = keep forever), so the decision becomes a
configuration change with an audit trail, not a code change.

What is purgeable and what is not:

  purgeable   OTP challenges, notification delivery logs, bell notifications,
              audit log entries, report outputs (the files and their request rows)
  never here  plan versions, answers, risks, approved documents, call tree runs
              and attempts, crisis events, tests - these are the compliance
              record. Retiring them is a business decision taken per record,
              not a sweep.

`apply_retention(dry_run=True)` reports what would go; the beat job runs it
weekly for real.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _cutoff(days: int | None) -> dt.datetime | None:
    if not days or days <= 0:
        return None
    return timezone.now() - dt.timedelta(days=days)


def apply_retention(*, dry_run: bool = False, now: dt.datetime | None = None) -> dict[str, int]:
    """Delete (or count, when dry_run) everything past its configured period."""
    from apps.accounts.models import OtpChallenge
    from apps.core.models import AuditLog
    from apps.documents.models import Document, EntityDocument
    from apps.notifications.models import NotificationLog, UserNotification
    from apps.reporting.models import ReportRequest

    now = now or timezone.now()
    counts: dict[str, int] = {}

    def sweep(name: str, queryset) -> None:
        counts[name] = queryset.count()
        if not dry_run and counts[name]:
            queryset.delete()

    cutoff = _cutoff(settings.RETENTION_OTP_CHALLENGE_DAYS)
    if cutoff:
        sweep("otp_challenges", OtpChallenge.objects.filter(created_at__lt=cutoff))

    cutoff = _cutoff(settings.RETENTION_NOTIFICATION_LOG_DAYS)
    if cutoff:
        sweep("notification_log", NotificationLog.objects.filter(created_at__lt=cutoff))

    cutoff = _cutoff(settings.RETENTION_USER_NOTIFICATION_DAYS)
    if cutoff:
        sweep("user_notifications", UserNotification.objects.filter(created_at__lt=cutoff))

    cutoff = _cutoff(settings.RETENTION_AUDIT_LOG_DAYS)
    if cutoff:
        sweep("audit_log", AuditLog.objects.filter(created_at__lt=cutoff))

    cutoff = _cutoff(settings.RETENTION_REPORT_OUTPUT_DAYS)
    if cutoff:
        # Report outputs: the attachment rows, the request rows, and the files -
        # unless a file is also attached elsewhere (content addressing can share it).
        old_requests = ReportRequest.objects.filter(
            created_at__lt=cutoff, active_flag=False
        ) | ReportRequest.objects.filter(last_run_at__lt=cutoff, active_flag=False)
        old_requests = old_requests.distinct()
        request_ids = list(old_requests.values_list("pk", flat=True))
        attachments = EntityDocument.objects.filter(
            entity_type=EntityDocument.EntityType.REPORT_REQUEST, entity_id__in=request_ids
        )
        document_ids = set(attachments.values_list("document_id", flat=True))
        counts["report_requests"] = len(request_ids)
        counts["report_documents"] = 0
        if not dry_run:
            attachments.delete()
            old_requests.delete()
        for document in Document.objects.filter(pk__in=document_ids):
            if EntityDocument.objects.filter(document=document).exists():
                continue  # still attached to something that lives
            counts["report_documents"] += 1
            if not dry_run:
                path = Path(settings.MEDIA_ROOT) / document.storage_key
                document.delete()
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove report file %s", path)
        if dry_run:
            counts["report_documents"] = len(document_ids)

    logger.info("Retention %s: %s", "dry run" if dry_run else "applied", counts)
    return counts
