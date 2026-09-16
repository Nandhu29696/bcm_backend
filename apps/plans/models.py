"""
Plan lifecycle and versioning — the spine of the application.

A plan belongs to a (process, cost_code) pair. Plan versions are immutable once
Approved; changing anything deep-copies the version (AD-4), which is what gives
journey step 4 its "current / previous versions" list.

S4: audit stamps (`created_by`, `updated_by`, `changed_by`, `approved_by`) point at
user_accounts, not employees. An SSO user with no HR record must still be able to
approve a plan — pointing these at `employees` would make that impossible.
Domain roles (plan owner, assigned coordinator) still point at `employees`, because
those describe a person in the organisation rather than who clicked the button.
"""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel, TimeStampedModel


class PlanStatus(models.TextChoices):
    """S10 — the six real statuses from the legacy system.

    Permitted transitions (enforced in the service layer, never by PATCH):
        Not Started      -> Work in Progress
        Work in Progress -> Pending BU Lead Review   (submit)
        Work in Progress -> Exempted                 (exemption approved)
        Pending Review   -> Approved                 (approve -> generate document)
        Pending Review   -> Rework                   (reject)
        Rework           -> Work in Progress         (coordinator resumes)
        Approved         -> terminal; copy to a new version to change anything
    """

    NOT_STARTED = "Not Started", "Not Started"
    WORK_IN_PROGRESS = "Work in Progress", "Work in Progress"
    PENDING_BU_LEAD_REVIEW = "Pending BU Lead Review", "Pending BU Lead Review"
    APPROVED = "Approved", "Approved"
    REWORK = "Rework", "Rework"
    EXEMPTED = "Exempted", "Exempted"


#: Single source of truth for the workflow. Phase 6 reads this; nothing else
#: should hard-code a transition.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    PlanStatus.NOT_STARTED: (PlanStatus.WORK_IN_PROGRESS,),
    PlanStatus.WORK_IN_PROGRESS: (
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.EXEMPTED,
    ),
    PlanStatus.PENDING_BU_LEAD_REVIEW: (PlanStatus.APPROVED, PlanStatus.REWORK),
    PlanStatus.REWORK: (PlanStatus.WORK_IN_PROGRESS,),
    PlanStatus.APPROVED: (),
    PlanStatus.EXEMPTED: (),
}


class Plan(BaseModel):
    plan_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    process = models.ForeignKey(
        "organization.Process",
        on_delete=models.PROTECT,
        db_column="process_id",
        related_name="plans",
    )
    cost_code = models.ForeignKey(
        "organization.CostCode",
        on_delete=models.PROTECT,
        db_column="cost_code_id",
        related_name="plans",
    )
    owner_employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="owner_employee_id",
        related_name="owned_plans",
    )

    class Meta(BaseModel.Meta):
        db_table = "plans"
        constraints = [
            models.UniqueConstraint(
                fields=["process", "cost_code"], name="uq_plan_process_cost_code"
            )
        ]

    def __str__(self):
        return f"Plan {self.plan_id} ({self.cost_code})"

    @property
    def current_version(self):
        return self.versions.order_by("-version_number").first()


class PlanVersion(TimeStampedModel):
    plan_version_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    plan = models.ForeignKey(
        Plan, on_delete=models.CASCADE, db_column="plan_id", related_name="versions"
    )
    version_number = models.IntegerField()
    plan_mode = models.CharField(max_length=50, blank=True)
    review_mode = models.CharField(max_length=50, blank=True)
    status = models.CharField(
        max_length=50, choices=PlanStatus.choices, default=PlanStatus.NOT_STARTED
    )
    published_flag = models.BooleanField(default=False)
    copied_flag = models.BooleanField(default=False)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="approved_by",
        related_name="plan_versions_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="plan_versions_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="updated_by",
        related_name="plan_versions_updated",
    )

    class Meta:
        db_table = "plan_versions"
        ordering = ["plan", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "version_number"], name="uq_plan_version_number"
            )
        ]
        indexes = [
            models.Index(fields=["status", "published_flag"], name="idx_pv_status_pub"),
            # Supports the "latest version per plan" subquery behind the cost code
            # list's BCP status column (Phase 2.2).
            models.Index(fields=["plan", "-version_number"], name="idx_pv_plan_version"),
        ]

    def __str__(self):
        return f"{self.plan_id} v{self.version_number} [{self.status}]"

    @property
    def is_editable(self) -> bool:
        """Approved and Exempted versions are read-only (AD-4)."""
        return self.status in {
            PlanStatus.NOT_STARTED,
            PlanStatus.WORK_IN_PROGRESS,
            PlanStatus.REWORK,
        }

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in ALLOWED_TRANSITIONS.get(self.status, ())


class PlanStatusHistory(models.Model):
    """Append-only status trail — the History action in journey step 4."""

    plan_status_history_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        PlanVersion,
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="status_history",
    )
    status = models.CharField(max_length=50, choices=PlanStatus.choices)
    comments = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="changed_by",
        related_name="plan_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "plan_status_history"
        ordering = ["plan_version", "changed_at"]
        indexes = [models.Index(fields=["plan_version", "changed_at"], name="idx_psh_version")]

    def __str__(self):
        return f"{self.plan_version_id} -> {self.status}"


class CoordinatorAssignment(BaseModel):
    coordinator_assignment_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        PlanVersion,
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="coordinator_assignments",
    )
    employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.PROTECT,
        db_column="employee_id",
        related_name="coordinator_assignments",
    )
    coordinator_type = models.CharField(max_length=80, blank=True)
    estate = models.ForeignKey(
        "organization.Estate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="estate_id",
        related_name="coordinator_assignments",
    )
    additional_user_flag = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        db_table = "coordinator_assignments"
        constraints = [
            models.UniqueConstraint(
                fields=["plan_version", "employee", "coordinator_type"],
                name="uq_coordinator_assignment",
            )
        ]

    def __str__(self):
        return f"{self.employee} on {self.plan_version_id}"
