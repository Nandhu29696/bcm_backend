"""
Submission and review endpoints (Phase 6) — journey steps 6 and 7.

    POST /api/v1/plan-versions/{id}/submit/       coordinator  (6.1)
    POST /api/v1/plan-versions/{id}/approve/      BU lead      (6.3)
    POST /api/v1/plan-versions/{id}/rework/       BU lead      (6.4)
    GET  /api/v1/plan-versions/{id}/readiness/    what blocks submission
    GET  /api/v1/review-queue/                    everything awaiting the caller (6.2)

Verbs on a subresource (AD-10). The status field is read-only everywhere else.
"""

from __future__ import annotations

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.roles import APPROVAL_ROLES, RoleCode
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.plans import workflow
from apps.plans.access import PlanVersionScopedMixin, is_bu_lead_for, version_list_context
from apps.plans.childviews import NotAnAuthor
from apps.plans.models import CoordinatorAssignment, PlanStatus, PlanVersion
from apps.plans.serializers import PlanVersionSerializer


class TransitionSerializer(serializers.Serializer):
    comments = serializers.CharField(required=False, allow_blank=True, max_length=4000, default="")


class NotAnApprover(NotAnAuthor):
    default_detail = "Only the cost code's BU lead or an administrator can review this plan."
    default_code = "not_an_approver"


def _version_payload(version: PlanVersion) -> dict:
    return PlanVersionSerializer(version, context=version_list_context(version.plan)).data


class ReadinessView(PlanVersionScopedMixin, APIView):
    """What, if anything, stops this version being submitted."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        blockers = workflow.incomplete_sections(version) if version.is_editable else []
        is_approver = self.caller_may_approve(version)
        return Response(
            {
                "can_submit": version.is_editable
                and not blockers
                and self.caller_may_author(version),
                "can_review": version.status == PlanStatus.PENDING_BU_LEAD_REVIEW and is_approver,
                # Whether the caller may decide things on this version at all -
                # an exemption can be decided while the plan is still editable.
                "is_approver": is_approver,
                "is_author": self.caller_may_author(version),
                "incomplete_sections": blockers,
            }
        )


class SubmitView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(
        request=TransitionSerializer,
        responses=PlanVersionSerializer,
        summary="Submit for BU lead review",
    )
    def post(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if not self.caller_may_author(version):
            raise NotAnAuthor()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            moved = workflow.submit(
                version, actor=request.user, comments=body.validated_data["comments"]
            )
        except workflow.PlanIncomplete as error:
            return Response(
                {
                    "detail": str(error.detail),
                    "code": error.default_code,
                    "field_errors": {},
                    "incomplete_sections": error.incomplete,
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        return Response(_version_payload(moved))


class ApproveView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(request=TransitionSerializer, responses=PlanVersionSerializer, summary="Approve")
    def post(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if not self.caller_may_approve(version):
            raise NotAnApprover()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        moved = workflow.approve(
            version, actor=request.user, comments=body.validated_data["comments"]
        )
        return Response(_version_payload(moved))


class ReworkView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(
        request=TransitionSerializer,
        responses=PlanVersionSerializer,
        summary="Send back for rework",
    )
    def post(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if not self.caller_may_approve(version):
            raise NotAnApprover()
        body = TransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        moved = workflow.rework(
            version, actor=request.user, comments=body.validated_data["comments"]
        )
        return Response(_version_payload(moved))


class ReviewQueueView(ScopedQuerySetMixin, APIView):
    """Plans awaiting the caller's review (6.2).

    Administrators see every pending version in scope; BU leads and approvers
    see those for cost codes they lead. Coordinators see their own plans that
    are out for review, so they know where things stand.
    """

    estate_scope_path = "plan__cost_code__estate_id"
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        scope = self.get_scope()
        pending = self.scope_queryset(
            PlanVersion.objects.filter(status=PlanStatus.PENDING_BU_LEAD_REVIEW)
            .select_related(
                "plan",
                "plan__cost_code",
                "plan__cost_code__bu_lead",
                "plan__cost_code__estate",
                "plan__cost_code__process",
            )
            .prefetch_related(
                Prefetch(
                    "coordinator_assignments",
                    queryset=CoordinatorAssignment.objects.select_related("employee"),
                )
            )
            .order_by("updated_at")
        )

        if scope.has_role(RoleCode.ADMIN):
            mine = list(pending)
        elif scope.has_role(*APPROVAL_ROLES):
            mine = [v for v in pending if is_bu_lead_for(request.user, v.plan.cost_code)]
        else:
            employee_id = scope.employee_id
            mine = (
                [
                    v
                    for v in pending
                    if any(
                        a.employee_id == employee_id and a.active_flag
                        for a in v.coordinator_assignments.all()
                    )
                ]
                if employee_id
                else []
            )

        rows = []
        for version in mine:
            data = PlanVersionSerializer(version, context=version_list_context(version.plan)).data
            data["estate_name"] = getattr(version.plan.cost_code.estate, "estate_name", "")
            data["process_name"] = getattr(version.plan.cost_code.process, "process_name", "")
            data["bu_lead_name"] = getattr(version.plan.cost_code.bu_lead, "lead_name", "")
            data["submitted_at"] = version.updated_at
            data["can_review"] = scope.has_role(RoleCode.ADMIN) or (
                scope.has_role(*APPROVAL_ROLES)
                and is_bu_lead_for(request.user, version.plan.cost_code)
            )
            rows.append(data)
        return Response(rows)
