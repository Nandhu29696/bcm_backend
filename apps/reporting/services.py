"""
Report requests: create, run, schedule, deliver (Phase 9.3).

A request is run by a Celery task. The output is stored like an upload
(content-addressed, under MEDIA_ROOT), attached to the request, and the
requester is emailed a signed download link. A scheduled request advances its
`next_run_at` after each run; the beat job picks up whatever is due.

The requester's scope is resolved when the report is built, so a schedule can
never outlive their access: a deactivated account produces nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path

from django.conf import settings
from django.core.signing import TimestampSigner
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import record_audit
from apps.core.models import AuditLog
from apps.core.tasks import enqueue_after_commit
from apps.documents.generation import media_root
from apps.documents.models import Document, EntityDocument
from apps.documents.uploads import attach
from apps.reporting import exports, reports
from apps.reporting.models import ReportFormat, ReportRequest, ReportType, RequestStatus, Schedule

logger = logging.getLogger(__name__)

DOCUMENT_TYPE = "REPORT_OUTPUT"

#: Which formats each report supports. Tabular reports do not render well as
#: PDF beyond a handful of columns; the summary is the PDF-shaped one.
FORMATS = {
    ReportType.ESTATE_DETAIL: {ReportFormat.XLSX, ReportFormat.CSV, ReportFormat.PDF},
    ReportType.COORDINATOR_ASSIGNMENTS: {ReportFormat.XLSX, ReportFormat.CSV, ReportFormat.PDF},
    ReportType.CALL_TREE_RUNS: {ReportFormat.XLSX, ReportFormat.CSV, ReportFormat.PDF},
    ReportType.EXEMPTION_REGISTER: {ReportFormat.XLSX, ReportFormat.CSV, ReportFormat.PDF},
    ReportType.DASHBOARD_SUMMARY: {ReportFormat.XLSX, ReportFormat.CSV, ReportFormat.PDF},
}


def next_run_after(schedule: str, moment: dt.datetime) -> dt.datetime | None:
    if schedule == Schedule.DAILY:
        return moment + dt.timedelta(days=1)
    if schedule == Schedule.WEEKLY:
        return moment + dt.timedelta(weeks=1)
    if schedule == Schedule.MONTHLY:
        # Same day next month, clamped to that month's length.
        year, month = (
            (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
        )
        import calendar

        day = min(moment.day, calendar.monthrange(year, month)[1])
        return moment.replace(year=year, month=month, day=day)
    return None


@transaction.atomic
def create_request(
    *, user, report_type: str, report_format: str, parameters: dict, schedule: str
) -> ReportRequest:
    request = ReportRequest.objects.create(
        requested_by=user,
        report_type=report_type,
        report_format=report_format,
        parameters=parameters or {},
        schedule=schedule,
        next_run_at=timezone.now(),
    )
    record_audit(
        action=AuditLog.Action.RECORD_CREATED,
        actor=user,
        entity_type="ReportRequest",
        entity_id=request.pk,
        detail={
            "report_type": report_type,
            "format": report_format,
            "schedule": schedule,
            "parameters": parameters,
        },
    )
    from apps.reporting.tasks import run_report_request

    enqueue_after_commit(lambda: run_report_request.delay(request.pk))
    return request


def _store(content: bytes, *, file_name: str, mime_type: str, actor) -> Document:
    digest = hashlib.sha256(content).hexdigest()
    storage_key = f"reports/{digest[:2]}/{digest}{Path(file_name).suffix}"
    existing = Document.objects.filter(storage_key=storage_key).first()
    if existing is not None:
        return existing
    path = media_root() / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return Document.objects.create(
        file_name=file_name,
        storage_key=storage_key,
        mime_type=mime_type,
        file_size_bytes=len(content),
        checksum_sha256=digest,
        uploaded_by=actor,
    )


def download_url(attachment: EntityDocument) -> str:
    token = TimestampSigner(salt="bcm.document.download").sign(str(attachment.pk))
    return f"{settings.PUBLIC_BASE_URL}{settings.API_BASE_PATH}/documents/{attachment.pk}/download/?token={token}"


def run_request(request_id: int) -> ReportRequest:
    """Build, store, attach, email. Failures are recorded on the row, never raised."""
    request = ReportRequest.objects.select_related("requested_by").get(pk=request_id)
    user = request.requested_by
    now = timezone.now()
    try:
        if not user.is_active:
            raise PermissionError("The requesting account is no longer active.")
        table = reports.build(request.report_type, user, request.parameters)
        content, mime = exports.render(table, request.report_format)
        stamp = now.strftime("%Y%m%d-%H%M")
        file_name = f"{request.report_type.lower()}-{stamp}.{request.report_format}"
        document = _store(content, file_name=file_name, mime_type=mime, actor=user)
        attachment = attach(
            document,
            entity_type=EntityDocument.EntityType.REPORT_REQUEST,
            entity_id=request.pk,
            document_type=DOCUMENT_TYPE,
        )

        request.document = document
        request.status = RequestStatus.COMPLETED
        request.last_error = ""
        _deliver(request, attachment, rows=len(table.rows))
    except Exception as exc:  # noqa: BLE001 - recorded on the row, the schedule carries on
        logger.exception("Report request %s failed", request_id)
        request.status = RequestStatus.FAILED
        request.last_error = str(exc)[:2000]

    request.run_count += 1
    request.last_run_at = now
    request.next_run_at = next_run_after(request.schedule, now) if request.active_flag else None
    if request.schedule == Schedule.ONCE:
        request.active_flag = False
    request.save()
    return request


def _deliver(request: ReportRequest, attachment: EntityDocument, *, rows: int) -> None:
    from apps.notifications.models import NotificationEvent
    from apps.notifications.services import send_notification

    email = (request.requested_by.email or "").strip()
    if not email:
        return
    send_notification(
        event_type=NotificationEvent.REPORT_READY,
        to_email=email,
        subject=f"Your {ReportType(request.report_type).label.lower()} is ready",
        template_name="report_ready",
        context={
            "report_name": ReportType(request.report_type).label,
            "report_format": request.report_format.upper(),
            "rows": rows,
            "schedule": Schedule(request.schedule).label,
            "download_url": download_url(attachment),
            "expires_hours": settings.AWS_S3_SIGNED_URL_EXPIRY_SECONDS // 3600,
            "report_request_id": request.pk,
        },
        entity_type="REPORT_REQUEST",
        entity_id=request.pk,
        idempotency_key=f"report:{request.pk}:run:{request.run_count + 1}",
    )


def due_requests(now: dt.datetime | None = None):
    now = now or timezone.now()
    return ReportRequest.objects.filter(active_flag=True, next_run_at__lte=now).exclude(
        schedule=Schedule.ONCE
    )


def latest_attachment(request: ReportRequest) -> EntityDocument | None:
    if not request.document_id:
        return None
    return EntityDocument.objects.filter(
        document_id=request.document_id,
        entity_type=EntityDocument.EntityType.REPORT_REQUEST,
        entity_id=request.pk,
    ).first()
