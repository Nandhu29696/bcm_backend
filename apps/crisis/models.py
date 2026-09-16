"""
Crisis management — journey step 8.

S6: neither of these tables existed in the schema; they are reconstructed from the
legacy CSV exports `tran_BCM_Crisis_Management.csv` and
`BCM_Crisis_Mangmnt_CMSC_Members.csv`.

The legacy exports denormalised the whole org context onto every row (process,
region, centre, location, estate). Only `cost_code` is carried here — the rest is
derivable through it, and duplicating it invites the two copies to disagree.
"""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class CrisisEvent(models.Model):
    """A crisis or exercise initiated against a cost code.

    `csd_ticket_number` links to the external service-desk ticket; it is how the
    business correlates a crisis here with the incident record elsewhere.
    """

    class EventType(models.TextChoices):
        TABLE_TOP = "Table Top", "Table Top"
        CALL_TREE = "Call tree", "Call tree"
        FULL_SIMULATION = "Full Simulation", "Full Simulation"
        WALKTHROUGH = "Walkthrough", "Walkthrough"
        LIVE_INCIDENT = "Live Incident", "Live Incident"

    class Status(models.TextChoices):
        PLANNED = "Planned", "Planned"
        INITIATED = "Initiated", "Initiated"
        IN_PROGRESS = "In Progress", "In Progress"
        CLOSED = "Closed", "Closed"
        CANCELLED = "Cancelled", "Cancelled"

    crisis_event_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    cost_code = models.ForeignKey(
        "organization.CostCode",
        on_delete=models.PROTECT,
        db_column="cost_code_id",
        related_name="crisis_events",
    )
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="plan_version_id",
        related_name="crisis_events",
    )
    csd_ticket_number = models.CharField(max_length=80, blank=True, db_index=True)
    event_type = models.CharField(max_length=80, choices=EventType.choices)
    comments = models.TextField(blank=True)
    event_date = models.DateField(null=True, blank=True)
    event_time = models.TimeField(null=True, blank=True)
    initiated_flag = models.BooleanField(default=False)
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.PLANNED)
    call_tree_run = models.ForeignKey(
        "calltree.CallTreeRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="call_tree_run_id",
        related_name="crisis_events",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="crisis_events_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crisis_events"
        ordering = ["-event_date", "-event_time"]
        indexes = [
            models.Index(fields=["cost_code", "created_at"], name="idx_crisis_cc_created"),
            models.Index(fields=["status", "event_type"], name="idx_crisis_status_type"),
        ]

    def __str__(self):
        return f"{self.event_type} {self.event_date} ({self.cost_code_id})"


class CmscMember(BaseModel):
    """Crisis Management Steering Committee roster — who a crisis call tree dials.

    Contact details are stored on the row rather than only referenced through
    `employee`, because the legacy process maintained this list by bulk upload for
    people who may not have an employee record.
    """

    cmsc_member_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_id",
        related_name="cmsc_memberships",
    )
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="plan_version_id",
        related_name="cmsc_members",
    )
    cost_code = models.ForeignKey(
        "organization.CostCode",
        on_delete=models.CASCADE,
        db_column="cost_code_id",
        related_name="cmsc_members",
    )
    process = models.ForeignKey(
        "organization.Process",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="process_id",
        related_name="cmsc_members",
    )
    region = models.ForeignKey(
        "organization.Region",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="region_id",
        related_name="cmsc_members",
    )
    bu_lead = models.ForeignKey(
        "organization.BuLead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="bu_lead_id",
        related_name="cmsc_members",
    )

    member_name = models.CharField(max_length=200)
    member_email = models.EmailField(max_length=320, blank=True)
    center = models.CharField(max_length=150, blank=True)
    location = models.CharField(max_length=150, blank=True)
    country_code = models.CharField(max_length=10, blank=True)
    phone_number = models.CharField(max_length=50, blank=True)
    reporting_manager_name = models.CharField(max_length=200, blank=True)
    reporting_manager_email = models.EmailField(max_length=320, blank=True)

    action_required_flag = models.BooleanField(default=False)
    email_sent_status = models.CharField(max_length=50, blank=True)
    bulk_call_status = models.CharField(max_length=50, blank=True)
    bulk_call_status_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="cmsc_members_created",
    )

    class Meta(BaseModel.Meta):
        db_table = "cmsc_members"
        ordering = ["member_name"]
        indexes = [models.Index(fields=["cost_code"], name="idx_cmsc_cost_code")]

    def __str__(self):
        return self.member_name
