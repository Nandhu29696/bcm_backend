"""Risk register, actions and recovery strategy (Phase 5.4-5.6)."""

from __future__ import annotations

import datetime as dt

from rest_framework import serializers

from apps.assessments.serializers import EmployeeRefField
from apps.lookups.models import LookupCategory, LookupType
from apps.risk.models import ActionType, RecoveryStrategy, Risk, RiskAction
from apps.risk.scoring import RATING_TYPES, allowed_points, label_for

#: Action statuses come from the catalogue, per action type.
STATUS_LOOKUP_FOR_ACTION = {
    ActionType.MITIGATION: LookupType.MITIGATION_STATUS,
    ActionType.CONTINGENCY: LookupType.CONTINGENCY_STATUS,
}

#: Statuses that mean an action is finished and can no longer be overdue.
CLOSED_ACTION_STATUSES = frozenset({"Mitigated", "Contingency Plan Created"})


def catalogue_names(lookup_type: str) -> list[str]:
    return list(
        LookupCategory.objects.filter(category_type=lookup_type)
        .order_by("category_name")
        .values_list("category_name", flat=True)
        .distinct()
    )


class RatingField(serializers.DecimalField):
    """A rating is a catalogue weight, and only a weight the catalogue has.

    Accepting any number here would let a client score a risk 100 x 100 and
    bypass the whole point of catalogue-driven scoring.
    """

    def __init__(self, lookup_type: str, **kwargs):
        self.lookup_type = lookup_type
        kwargs.setdefault("max_digits", 5)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("allow_null", True)
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value is not None and value not in allowed_points(self.lookup_type):
            options = ", ".join(str(p) for p in sorted(allowed_points(self.lookup_type)))
            raise serializers.ValidationError(
                f"Must be one of the {self.lookup_type} weights: {options}."
            )
        return value


class RiskActionSerializer(serializers.ModelSerializer):
    action_type = serializers.ChoiceField(choices=ActionType.choices)
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = RiskAction
        fields = [
            "risk_action_id",
            "risk_id",
            "action_type",
            "description",
            "status",
            "target_date",
            "comments",
            "resource_type",
            "is_overdue",
            "created_at",
        ]
        read_only_fields = ["risk_action_id", "risk_id"]

    def get_is_overdue(self, action) -> bool:
        return is_overdue(action)

    def validate(self, attrs):
        action_type = attrs.get("action_type", getattr(self.instance, "action_type", None))
        status = attrs.get("status", getattr(self.instance, "status", ""))
        if status:
            allowed = catalogue_names(STATUS_LOOKUP_FOR_ACTION[action_type])
            if status not in allowed:
                raise serializers.ValidationError(
                    {
                        "status": f"For a {action_type.lower()} action, status must be one of: {', '.join(allowed)}."
                    }
                )
        return attrs


def is_overdue(action: RiskAction, today: dt.date | None = None) -> bool:
    """Past its target date and not closed (5.5)."""
    if action.target_date is None or action.status in CLOSED_ACTION_STATUSES:
        return False
    return action.target_date < (today or dt.date.today())


class RiskSerializer(serializers.ModelSerializer):
    """A risk register row. The three scores are read-only — see scoring.py."""

    owner_employee = EmployeeRefField()
    likelihood_rating = RatingField(LookupType.LIKELIHOOD)
    severity_rating = RatingField(LookupType.SEVERITY_RATING)
    control_effectiveness_rating = RatingField(LookupType.CONTROL_EFFECTIVENESS)

    likelihood_label = serializers.SerializerMethodField()
    severity_label = serializers.SerializerMethodField()
    control_effectiveness_label = serializers.SerializerMethodField()

    inherent_risk_score = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    residual_risk_score = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    risk_level = serializers.CharField(read_only=True)

    actions = RiskActionSerializer(many=True, read_only=True)
    open_action_count = serializers.SerializerMethodField()
    overdue_action_count = serializers.SerializerMethodField()

    class Meta:
        model = Risk
        fields = [
            "risk_id",
            "plan_version_id",
            "owner_employee",
            "resource_type",
            "risk_name",
            "description",
            "impact_area",
            "likelihood_rating",
            "likelihood_label",
            "severity_rating",
            "severity_label",
            "control_effectiveness_rating",
            "control_effectiveness_label",
            "inherent_risk_score",
            "residual_risk_score",
            "risk_level",
            "target_closure_date",
            "occurred_flag",
            "comments",
            "actions",
            "open_action_count",
            "overdue_action_count",
            "created_at",
        ]
        read_only_fields = ["risk_id", "plan_version_id"]

    def get_likelihood_label(self, risk):
        return label_for(RATING_TYPES["likelihood_rating"], risk.likelihood_rating)

    def get_severity_label(self, risk):
        return label_for(RATING_TYPES["severity_rating"], risk.severity_rating)

    def get_control_effectiveness_label(self, risk):
        return label_for(
            RATING_TYPES["control_effectiveness_rating"], risk.control_effectiveness_rating
        )

    def get_open_action_count(self, risk) -> int:
        return sum(1 for a in risk.actions.all() if a.status not in CLOSED_ACTION_STATUSES)

    def get_overdue_action_count(self, risk) -> int:
        return sum(1 for a in risk.actions.all() if is_overdue(a))

    def validate_risk_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A risk needs a name.")
        return value

    def validate_resource_type(self, value):
        value = (value or "").strip()
        if value and value not in catalogue_names(LookupType.RESOURCE_TYPE):
            raise serializers.ValidationError(
                f"Must be one of: {', '.join(catalogue_names(LookupType.RESOURCE_TYPE))}."
            )
        return value

    def validate(self, attrs):
        # Any attempt to supply a score is refused loudly rather than ignored, so
        # a client that thinks it is setting one finds out.
        for name in ("inherent_risk_score", "residual_risk_score", "risk_level"):
            if name in self.initial_data:
                raise serializers.ValidationError(
                    {name: "Scores are derived from the ratings and cannot be set."}
                )
        return attrs


class RecoveryStrategySerializer(serializers.ModelSerializer):
    """Core and tactical strategy, from the catalogue (5.6)."""

    owner_employee = EmployeeRefField()

    class Meta:
        model = RecoveryStrategy
        fields = [
            "recovery_strategy_id",
            "plan_version_id",
            "owner_employee",
            "core_strategy",
            "tactical_strategy",
            "created_at",
        ]
        read_only_fields = ["recovery_strategy_id", "plan_version_id"]

    def validate_core_strategy(self, value):
        return self._catalogue_value(value, LookupType.CORE_STRATEGY, "core strategy")

    def validate_tactical_strategy(self, value):
        return self._catalogue_value(value, LookupType.TACTICAL_STRATEGY, "tactical strategy")

    @staticmethod
    def _catalogue_value(value: str, lookup_type: str, label: str) -> str:
        value = (value or "").strip()
        if value and value not in catalogue_names(lookup_type):
            raise serializers.ValidationError(
                f"The {label} must be one of: {', '.join(catalogue_names(lookup_type))}."
            )
        return value

    def validate(self, attrs):
        core = attrs.get("core_strategy", getattr(self.instance, "core_strategy", ""))
        tactical = attrs.get("tactical_strategy", getattr(self.instance, "tactical_strategy", ""))
        if not core and not tactical:
            raise serializers.ValidationError(
                {"core_strategy": "Choose a core strategy, a tactical strategy, or both."}
            )
        return attrs


__all__ = [
    "CLOSED_ACTION_STATUSES",
    "RecoveryStrategySerializer",
    "RiskActionSerializer",
    "RiskSerializer",
    "is_overdue",
]
