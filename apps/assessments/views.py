"""Structured BIA sections of a plan version (Phase 5.1-5.3)."""

from apps.assessments.models import BiaCriticalContact, BiaServiceDescription, NetworkRequirement
from apps.assessments.serializers import (
    BiaCriticalContactSerializer,
    BiaServiceDescriptionSerializer,
    NetworkRequirementSerializer,
)
from apps.plans.childviews import PlanVersionChildViewSet


class ServiceDescriptionViewSet(PlanVersionChildViewSet):
    model = BiaServiceDescription
    serializer_class = BiaServiceDescriptionSerializer
    select_related = ("process", "subprocess", "owner_employee")


class CriticalContactViewSet(PlanVersionChildViewSet):
    model = BiaCriticalContact
    serializer_class = BiaCriticalContactSerializer
    select_related = ("employee",)


class NetworkRequirementViewSet(PlanVersionChildViewSet):
    model = NetworkRequirement
    serializer_class = NetworkRequirementSerializer
    select_related = ("employee",)
