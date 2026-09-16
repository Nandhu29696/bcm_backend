"""
Call tree execution.

A run dials its members in escalation order across three channels, recording every
attempt. The `(member, channel, attempt_number)` unique key is what makes provider
webhook replay idempotent — without it a redelivered callback double-records.

S5: `channel` was a MySQL ENUM.
"""

from django.conf import settings
from django.db import models


class Channel(models.TextChoices):
    VOICE = "VOICE", "Voice"
    MS_TEAMS = "MS_TEAMS", "Microsoft Teams"
    EMAIL = "EMAIL", "Email"


class RunStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class MemberStage(models.IntegerChoices):
    """Escalation stages, in the order the legacy process dialled them."""

    VOICE = 1, "Voice calls"
    TEAMS = 2, "Microsoft Teams"
    EMAIL = 3, "Email"
    DONE = 4, "Done"


class CallTreeRun(models.Model):
    call_tree_run_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="plan_version_id",
        related_name="call_tree_runs",
    )
    process = models.ForeignKey(
        "organization.Process",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="process_id",
        related_name="call_tree_runs",
    )
    cost_code = models.ForeignKey(
        "organization.CostCode",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="cost_code_id",
        related_name="call_tree_runs",
    )
    center = models.ForeignKey(
        "organization.Center",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="center_id",
        related_name="call_tree_runs",
    )
    broadcast_id = models.CharField(max_length=100, blank=True)
    call_tree_type = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=RunStatus.choices, default=RunStatus.PENDING)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="initiated_by",
        related_name="call_tree_runs_initiated",
    )
    # Which live providers were switched on when the run started. Recorded on the
    # run itself so a report can say "this was a real call" without consulting
    # settings that may have changed since.
    providers_enabled = models.JSONField(default=dict, blank=True)
    # True when the run was executed against the fake provider rather than a live
    # one. Simulation runs must never be counted in compliance reporting.
    simulation_flag = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "call_tree_runs"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["cost_code", "started_at"], name="idx_ctr_cc_started"),
            models.Index(fields=["status"], name="idx_ctr_status"),
        ]

    def __str__(self):
        return f"run {self.call_tree_run_id} [{self.status}]"


class CallTreeMember(models.Model):
    """Contact details are snapshotted onto the run.

    Deliberately denormalised: a run is evidence of who was contacted at the time,
    so later edits to the employee record must not rewrite history.
    """

    call_tree_member_id = models.BigAutoField(primary_key=True)
    call_tree_run = models.ForeignKey(
        CallTreeRun,
        on_delete=models.CASCADE,
        db_column="call_tree_run_id",
        related_name="members",
    )
    employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_id",
        related_name="call_tree_memberships",
    )
    member_name = models.CharField(max_length=200, blank=True)
    member_email = models.EmailField(max_length=320, blank=True)
    phone_number = models.CharField(max_length=50, blank=True)
    cmsc_member = models.ForeignKey(
        "crisis.CmscMember",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="cmsc_member_id",
        related_name="call_tree_memberships",
    )
    sequence_number = models.IntegerField(null=True, blank=True)
    # The stage this member has escalated to (MemberStage). Level 1 is the first
    # voice call; a member reached at level 1 never escalates.
    escalation_level = models.IntegerField(null=True, blank=True)
    reached_flag = models.BooleanField(default=False)
    reached_channel = models.CharField(max_length=20, choices=Channel.choices, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "call_tree_members"
        ordering = ["call_tree_run", "escalation_level", "sequence_number"]
        indexes = [
            models.Index(fields=["call_tree_run"], name="idx_ctm_run"),
        ]

    def __str__(self):
        return self.member_name or f"member {self.call_tree_member_id}"

    @property
    def reporting_manager_email(self) -> str:
        return self.cmsc_member.reporting_manager_email if self.cmsc_member_id else ""


class CallAttempt(models.Model):
    call_attempt_id = models.BigAutoField(primary_key=True)
    call_tree_member = models.ForeignKey(
        CallTreeMember,
        on_delete=models.CASCADE,
        db_column="call_tree_member_id",
        related_name="attempts",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    attempt_number = models.IntegerField()
    attempt_status = models.CharField(max_length=50, blank=True)
    status_code = models.CharField(max_length=50, blank=True)
    # The provider's own id for this attempt (a Twilio CallSid, a Graph message
    # id). A webhook finds its attempt by this, never by guessing.
    provider_reference = models.CharField(max_length=120, blank=True, db_index=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    response_key = models.CharField(max_length=100, blank=True)
    call_duration_seconds = models.IntegerField(null=True, blank=True)
    comments = models.TextField(blank=True)

    class Meta:
        db_table = "call_attempts"
        ordering = ["call_tree_member", "attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["call_tree_member", "channel", "attempt_number"],
                name="uq_call_attempt",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=Channel.values), name="ck_call_channel"
            ),
        ]

    def __str__(self):
        return f"{self.channel} #{self.attempt_number}"
