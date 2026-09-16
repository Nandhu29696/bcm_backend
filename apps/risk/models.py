"""
Risk register and recovery strategy.

Scores are DERIVED SERVER-SIDE from the lookup catalogue weights (Phase 5.4) —
`inherent_risk_score`, `residual_risk_score` and `risk_level` are never accepted
from the client. The formula lives in one service function so that API, UI and
document export cannot disagree.

S5: `action_type` was a MySQL ENUM; it is now TextChoices + CheckConstraint.
"""

from django.db import models


class ActionType(models.TextChoices):
    MITIGATION = "MITIGATION", "Mitigation"
    CONTINGENCY = "CONTINGENCY", "Contingency"


class Risk(models.Model):
    risk_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="risks",
    )
    owner_employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="owner_employee_id",
        related_name="owned_risks",
    )
    resource_type = models.CharField(max_length=100, blank=True)
    risk_name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    impact_area = models.CharField(max_length=150, blank=True)

    likelihood_rating = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    severity_rating = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    control_effectiveness_rating = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )

    # Derived — see module docstring. Not client-writable.
    inherent_risk_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    residual_risk_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    risk_level = models.CharField(max_length=50, blank=True)

    target_closure_date = models.DateField(null=True, blank=True)
    occurred_flag = models.BooleanField(null=True, blank=True)
    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "risks"
        ordering = ["-residual_risk_score", "risk_name"]

    def __str__(self):
        return self.risk_name


class RiskAction(models.Model):
    risk_action_id = models.BigAutoField(primary_key=True)
    risk = models.ForeignKey(
        Risk, on_delete=models.CASCADE, db_column="risk_id", related_name="actions"
    )
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=50, blank=True)
    target_date = models.DateField(null=True, blank=True)
    comments = models.TextField(blank=True)
    resource_type = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "risk_actions"
        indexes = [models.Index(fields=["status", "target_date"], name="idx_actions_status_due")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(action_type__in=ActionType.values),
                name="ck_risk_action_type",
            )
        ]

    def __str__(self):
        return f"{self.action_type} on risk {self.risk_id}"


class RecoveryStrategy(models.Model):
    recovery_strategy_id = models.BigAutoField(primary_key=True)
    plan_version = models.ForeignKey(
        "plans.PlanVersion",
        on_delete=models.CASCADE,
        db_column="plan_version_id",
        related_name="recovery_strategies",
    )
    owner_employee = models.ForeignKey(
        "accounts.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="owner_employee_id",
        related_name="recovery_strategies",
    )
    core_strategy = models.TextField(blank=True)
    tactical_strategy = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recovery_strategies"
        verbose_name_plural = "recovery strategies"

    def __str__(self):
        return f"recovery strategy {self.recovery_strategy_id}"
