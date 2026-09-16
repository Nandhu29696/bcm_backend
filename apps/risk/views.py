"""
Risk register, actions and recovery strategy of a plan version (Phase 5.4-5.6).

    /plan-versions/{id}/risks/                      the register
    /plan-versions/{id}/risks/{risk_id}/actions/    mitigation and contingency actions
    /plan-versions/{id}/recovery-strategies/
    /plan-versions/{id}/risk-options/               catalogue lists for the editor

Scores are derived on every save by `scoring.apply_scores`; the serializer
refuses them as input.
"""

from __future__ import annotations

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.lookups.models import LookupType
from apps.plans.access import PlanVersionScopedMixin
from apps.plans.childviews import PlanVersionChildViewSet
from apps.risk.models import ActionType, RecoveryStrategy, Risk, RiskAction
from apps.risk.scoring import RATING_TYPES, apply_scores, rating_options
from apps.risk.serializers import (
    RecoveryStrategySerializer,
    RiskActionSerializer,
    RiskSerializer,
    catalogue_names,
)


class RiskViewSet(PlanVersionChildViewSet):
    model = Risk
    serializer_class = RiskSerializer
    select_related = ("owner_employee",)

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .prefetch_related(Prefetch("actions", queryset=RiskAction.objects.order_by("pk")))
            .order_by("-residual_risk_score", "risk_name")
        )

    def perform_create(self, serializer):
        self.assert_writable()
        version = self.perform_write_bookkeeping()
        risk = Risk(plan_version=version, **serializer.validated_data)
        apply_scores(risk)
        risk.save()
        serializer.instance = risk

    def perform_update(self, serializer):
        self.assert_writable()
        self.perform_write_bookkeeping()
        risk = serializer.instance
        for name, value in serializer.validated_data.items():
            setattr(risk, name, value)
        apply_scores(risk)
        risk.save()


class RiskActionViewSet(PlanVersionChildViewSet):
    """Actions nested under one risk of the version."""

    model = RiskAction
    serializer_class = RiskActionSerializer

    def get_risk(self) -> Risk:
        return get_object_or_404(
            Risk.objects.filter(plan_version=self.get_plan_version()), pk=self.kwargs["risk_id"]
        )

    def get_queryset(self):
        return RiskAction.objects.filter(risk=self.get_risk()).order_by("pk")

    def perform_create(self, serializer):
        self.assert_writable()
        self.perform_write_bookkeeping()
        serializer.save(risk=self.get_risk())


class RecoveryStrategyViewSet(PlanVersionChildViewSet):
    model = RecoveryStrategy
    serializer_class = RecoveryStrategySerializer
    select_related = ("owner_employee",)


class RiskOptionsView(PlanVersionScopedMixin, APIView):
    """Every catalogue list the risk and strategy editors need, in one call."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(summary="Catalogue options for the risk register and recovery strategy")
    def get(self, request, *args, **kwargs):
        self.get_plan_version()  # scope check only
        return Response(
            {
                "ratings": {
                    field: [
                        {"points": str(o.points), "label": o.label}
                        for o in rating_options(lookup_type)
                    ]
                    for field, lookup_type in RATING_TYPES.items()
                },
                "resource_types": catalogue_names(LookupType.RESOURCE_TYPE),
                "risk_names": catalogue_names(LookupType.RISK_NAME),
                "action_statuses": {
                    ActionType.MITIGATION: catalogue_names(LookupType.MITIGATION_STATUS),
                    ActionType.CONTINGENCY: catalogue_names(LookupType.CONTINGENCY_STATUS),
                },
                "core_strategies": catalogue_names(LookupType.CORE_STRATEGY),
                "tactical_strategies": catalogue_names(LookupType.TACTICAL_STRATEGY),
            }
        )
