"""
Exemption requests.

An approved exemption moves the plan version to `Exempted` (PlanStatus.EXEMPTED),
which is one of the six real statuses — exemption is an outcome of review, not a
side channel around it.

S4: `requested_by` points at user_accounts. S5: `comment_type` was a MySQL ENUM.
"""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class ExemptionStatus(models.TextChoices):
    PENDING = "Pending", "Pending"
    APPROVED = "Approved", "Approved"
    REJECTED = "Rejected", "Rejected"
    REWORK = "Rework", "Rework"


class CommentType(models.TextChoices):
    APPROVAL = "APPROVAL", "Approval"
    REWORK = "REWORK", "Rework"
    GENERAL = "GENERAL", "General"


class Exemption(BaseModel):
    exemption_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="exemptions",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="requested_by",
        related_name="exemptions_requested",
    )
    status = models.CharField(
        max_length=50, choices=ExemptionStatus.choices, default=ExemptionStatus.PENDING
    )
    reason = models.TextField(blank=True)
    answer_1 = models.CharField(max_length=500, blank=True)
    answer_2 = models.CharField(max_length=500, blank=True)
    answer_3 = models.CharField(max_length=500, blank=True)

    class Meta(BaseModel.Meta):
        db_table = "exemptions"
        ordering = ["-created_at"]

    def __str__(self):
        return f"exemption {self.exemption_id} [{self.status}]"


class ExemptionComment(models.Model):
    exemption_comment_id = models.BigAutoField(primary_key=True)
    exemption = models.ForeignKey(
        Exemption,
        on_delete=models.CASCADE,
        db_column="exemption_id",
        related_name="comments",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="author_user_id",
        related_name="exemption_comments",
    )
    comment_type = models.CharField(max_length=20, choices=CommentType.choices)
    comment = models.TextField()
    status = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "exemption_comments"
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(comment_type__in=CommentType.values),
                name="ck_exemption_comment_type",
            )
        ]

    def __str__(self):
        return f"{self.comment_type} on exemption {self.exemption_id}"
