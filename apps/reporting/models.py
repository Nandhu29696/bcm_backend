"""
Report requests (Phase 9.3-9.4).

Mirrors the legacy download-status lists (`tran_BCM_Estates_DetailReport_status`,
`tran_BCM_Assign_Coordinator_Report`): who asked for which report, whether it
is ready, and where the file is. Adds a schedule so the same request can recur
and be emailed, which the legacy process did by hand.

The requester's data scope is applied when the report is built, never widened:
a scheduled report keeps producing what that person may see, and stops if their
account is deactivated.
"""

from django.conf import settings
from django.db import models


class ReportType(models.TextChoices):
    ESTATE_DETAIL = "ESTATE_DETAIL", "Estate detail report"
    COORDINATOR_ASSIGNMENTS = "COORDINATOR_ASSIGNMENTS", "Coordinator assignment report"
    CALL_TREE_RUNS = "CALL_TREE_RUNS", "Call tree run report"
    EXEMPTION_REGISTER = "EXEMPTION_REGISTER", "Exemption register"
    DASHBOARD_SUMMARY = "DASHBOARD_SUMMARY", "Dashboard summary"


class ReportFormat(models.TextChoices):
    XLSX = "xlsx", "Excel"
    CSV = "csv", "CSV"
    PDF = "pdf", "PDF"


class Schedule(models.TextChoices):
    ONCE = "ONCE", "Once"
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"


class RequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class ReportRequest(models.Model):
    report_request_id = models.BigAutoField(primary_key=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="requested_by",
        related_name="report_requests",
    )
    report_type = models.CharField(max_length=40, choices=ReportType.choices)
    report_format = models.CharField(
        max_length=10, choices=ReportFormat.choices, default=ReportFormat.XLSX
    )
    # Filters: {"estate_id": 3} and the like. Interpreted by the report builder.
    parameters = models.JSONField(default=dict, blank=True)
    schedule = models.CharField(max_length=10, choices=Schedule.choices, default=Schedule.ONCE)
    active_flag = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)

    status = models.CharField(
        max_length=20, choices=RequestStatus.choices, default=RequestStatus.PENDING
    )
    last_error = models.TextField(blank=True)
    run_count = models.PositiveIntegerField(default=0)
    # The most recent output. Earlier outputs stay attached as REPORT_REQUEST documents.
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="document_id",
        related_name="report_requests",
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "report_requests"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["requested_by", "created_at"], name="idx_reports_user_created")
        ]

    def __str__(self):
        return f"{self.report_type} ({self.report_format}) for {self.requested_by_id}"
