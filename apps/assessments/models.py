"""
Plan authoring and Business Impact Analysis — journey step 5.

S3: the respondent is NOT NULL and points at `user_accounts`. In the original
schema it was a nullable FK to `employees`, which broke the unique key
(plan_version, question, respondent, answer_context): MySQL treats NULLs as
distinct in a unique index, so autosave would have silently written duplicate
answer rows. It also has to be a user rather than an employee — the respondent is
whoever is logged in, and an SSO user may have no HR record.

S5: `answer_context` and `requirement_type` were MySQL ENUMs. They are now
VARCHAR + TextChoices + CheckConstraint, because Django cannot model ENUM natively
and every value change would otherwise be a DDL migration.
"""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel, TimeStampedModel


class AnswerContext(models.TextChoices):
    BCP = "BCP", "BCP"
    BIA = "BIA", "BIA"
    INTERNAL_DEPENDENCY = "INTERNAL_DEPENDENCY", "Internal dependency"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY", "External dependency"
    SUBCONTRACTOR = "SUBCONTRACTOR", "Subcontractor"


class RequirementType(models.TextChoices):
    BIA_PROJECT = "BIA_PROJECT", "BIA project"
    BCP_PLAN = "BCP_PLAN", "BCP plan"


class SectionStatusValue(models.TextChoices):
    NOT_STARTED = "Not Started", "Not Started"
    IN_PROGRESS = "In Progress", "In Progress"
    COMPLETED = "Completed", "Completed"


class QuestionAnswer(BaseModel):
    """An answer to one question, for one plan version, in one context.

    `answer_json` shape is validated against the question's `answer_type` (AD-5).
    Choice answers store the option CODE, never a FK to question_options, so that
    deactivating or rewording an option cannot change what a historical answer meant.
    """

    answer_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="answers",
    )
    question = models.ForeignKey(
        "questionnaire.Question",
        on_delete=models.PROTECT,
        db_column="question_id",
        related_name="answers",
    )
    respondent_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        db_column="respondent_user_id",
        related_name="answers_given",
    )
    answer_context = models.CharField(max_length=30, choices=AnswerContext.choices)
    answer_json = models.JSONField(null=True, blank=True)
    comments = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="answers_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="updated_by",
        related_name="answers_updated",
    )

    class Meta(BaseModel.Meta):
        db_table = "question_answers"
        constraints = [
            models.UniqueConstraint(
                fields=["plan_version", "question", "respondent_user", "answer_context"],
                name="uq_question_answer",
            ),
            models.CheckConstraint(
                condition=models.Q(answer_context__in=AnswerContext.values),
                name="ck_answer_context",
            ),
        ]
        indexes = [models.Index(fields=["question"], name="idx_answer_question")]

    def __str__(self):
        return f"answer {self.answer_id} q{self.question_id}"


class QuestionComment(models.Model):
    question_comment_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="question_comments",
    )
    question = models.ForeignKey(
        "questionnaire.Question",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_column="question_id",
        related_name="comments",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="author_user_id",
        related_name="question_comments",
    )
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "question_comments"
        ordering = ["created_at"]

    def __str__(self):
        return f"comment {self.question_comment_id}"


class SectionStatus(models.Model):
    """Per-section completion.

    Derived, never hand-set: a section is Completed when every *visible* required
    question has a valid answer. Hidden conditional branches must not block
    completion (Phase 4.6).
    """

    section_status_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="section_statuses",
    )
    section = models.ForeignKey(
        "questionnaire.Section",
        on_delete=models.CASCADE,
        db_column="section_id",
        related_name="statuses",
    )
    status = models.CharField(
        max_length=50,
        choices=SectionStatusValue.choices,
        default=SectionStatusValue.NOT_STARTED,
    )
    comments = models.TextField(blank=True)
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="changed_by",
        related_name="section_status_changes",
    )

    class Meta:
        db_table = "section_statuses"
        constraints = [
            models.UniqueConstraint(fields=["plan_version", "section"], name="uq_section_status")
        ]
        indexes = [models.Index(fields=["plan_version", "section"], name="idx_secstat_version")]

    def __str__(self):
        return f"{self.section} [{self.status}]"


class BiaServiceDescription(TimeStampedModel):
    """Recovery targets: MAO, MBCO, RTO, RPO."""

    service_description_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="service_descriptions",
    )
    process = models.ForeignKey(
        "organization.Process",
        on_delete=models.PROTECT,
        db_column="process_id",
        related_name="service_descriptions",
    )
    subprocess = models.ForeignKey(
        "organization.Subprocess",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="subprocess_id",
        related_name="service_descriptions",
    )
    cost_code = models.ForeignKey(
        "organization.CostCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="cost_code_id",
        related_name="service_descriptions",
    )
    owner_employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="owner_employee_id",
        related_name="service_descriptions",
    )
    process_description = models.TextField(blank=True)
    mao = models.CharField(max_length=100, blank=True, verbose_name="MAO")
    mbco = models.CharField(max_length=100, blank=True, verbose_name="MBCO")
    rto = models.CharField(max_length=100, blank=True, verbose_name="RTO")
    rpo = models.CharField(max_length=100, blank=True, verbose_name="RPO")

    class Meta:
        db_table = "bia_service_descriptions"

    def __str__(self):
        return f"service description {self.service_description_id}"


class BiaCriticalContact(models.Model):
    critical_contact_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="critical_contacts",
    )
    employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_id",
        related_name="critical_contact_entries",
    )
    contact_type = models.CharField(max_length=80, blank=True)
    shift_timings = models.CharField(max_length=100, blank=True)
    primary_phone = models.CharField(max_length=50, blank=True)
    alternate_phone = models.CharField(max_length=50, blank=True)
    seat_count = models.IntegerField(null=True, blank=True)
    voice_non_voice = models.CharField(max_length=30, blank=True)
    asset_id = models.CharField(max_length=100, blank=True)
    asset_make = models.CharField(max_length=150, blank=True)
    hardware_software = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bia_critical_contacts"

    def __str__(self):
        return f"critical contact {self.critical_contact_id}"


class NetworkRequirement(models.Model):
    network_requirement_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="network_requirements",
    )
    employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_id",
        related_name="network_requirements",
    )
    requirement_type = models.CharField(max_length=20, choices=RequirementType.choices)
    source_ip = models.CharField(max_length=100, blank=True)
    destination_ip = models.CharField(max_length=100, blank=True)
    port_number = models.CharField(max_length=50, blank=True)
    connectivity_type = models.CharField(max_length=100, blank=True)
    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "network_requirements"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(requirement_type__in=RequirementType.values),
                name="ck_requirement_type",
            )
        ]

    def __str__(self):
        return f"network requirement {self.network_requirement_id}"
