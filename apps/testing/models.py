"""Test scheduling and outcomes — journey step 8."""

from django.conf import settings
from django.db import models


class Test(models.Model):
    class TestType(models.TextChoices):
        CALL_TREE = "Call Tree Test", "Call Tree Test"
        TABLETOP = "Tabletop Exercise", "Tabletop Exercise"
        FULL_SIMULATION = "Full Simulation", "Full Simulation"
        WALKTHROUGH = "Walkthrough", "Walkthrough"

    class Status(models.TextChoices):
        SCHEDULED = "Scheduled", "Scheduled"
        IN_PROGRESS = "In Progress", "In Progress"
        COMPLETED = "Completed", "Completed"
        CANCELLED = "Cancelled", "Cancelled"

    test_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="tests",
    )
    test_type = models.CharField(max_length=100, choices=TestType.choices)
    scheduled_date = models.DateField(null=True, blank=True)
    scheduled_time = models.TimeField(null=True, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="initiated_by",
        related_name="tests_initiated",
    )
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.SCHEDULED)
    comments = models.TextField(blank=True)
    # Set when a Call Tree Test is executed through the call tree engine.
    call_tree_run = models.ForeignKey(
        "calltree.CallTreeRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="call_tree_run_id",
        related_name="tests",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tests"
        ordering = ["-scheduled_date"]
        indexes = [
            models.Index(fields=["scheduled_date", "status"], name="idx_tests_date_status"),
            models.Index(fields=["plan_version", "status"], name="idx_tests_version_status"),
        ]

    def __str__(self):
        return f"{self.test_type} {self.scheduled_date}"


class TestOutcome(models.Model):
    class FinalStatus(models.TextChoices):
        PASSED = "Passed", "Passed"
        PARTIAL = "Partial", "Partial"
        FAILED = "Failed", "Failed"
        PENDING = "Pending", "Pending"

    test_outcome_id = models.BigAutoField(primary_key=True)
    test = models.ForeignKey(
        Test, on_delete=models.CASCADE, db_column="test_id", related_name="outcomes"
    )
    conducted_date = models.DateField(null=True, blank=True)
    conducted_time = models.TimeField(null=True, blank=True)
    result = models.TextField(blank=True)
    final_status = models.CharField(max_length=50, choices=FinalStatus.choices, blank=True)
    final_report_document = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="final_report_document_id",
        related_name="test_outcomes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "test_outcomes"
        ordering = ["-conducted_date"]
        indexes = [models.Index(fields=["test", "conducted_date"], name="idx_outcomes_test_date")]

    def __str__(self):
        return f"outcome {self.test_outcome_id} [{self.final_status}]"
