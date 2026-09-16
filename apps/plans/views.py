"""
Cost code actions, plan versions and history — journey step 4.

    GET   /api/v1/cost-codes/{id}/                        detail
    PATCH /api/v1/cost-codes/{id}/                        edit          (3.1)
    GET   /api/v1/cost-codes/{id}/plan-versions/          current + previous (3.3)
    POST  /api/v1/cost-codes/{id}/plan-versions/          create the first  (3.6)
    GET   /api/v1/plan-versions/{id}/                     one version
    GET   /api/v1/plan-versions/{id}/overview/            objectives, stage statuses, people
    POST  /api/v1/plan-versions/{id}/copy/                copy-on-write (3.4)
    GET   /api/v1/plan-versions/{id}/history/             timeline      (3.5)
    GET   /api/v1/plan-versions/{id}/coordinators/        assignments   (3.2)
    POST  /api/v1/plan-versions/{id}/coordinators/        assign
    DELETE .../coordinators/{assignment_id}/              unassign

`copy` is a verb on a subresource rather than a PATCH of `version_number` or
`status` (AD-10). It is not an update of anything — it creates a row and a dozen
child rows in one transaction — and a PATCH would invite a client to fabricate a
version number.
"""

from __future__ import annotations

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser, role_required
from apps.accounts.roles import AUTHORING_ROLES, RoleCode
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.organization.models import CostCode
from apps.plans.access import PlanVersionScopedMixin, version_list_context
from apps.plans.models import (
    CoordinatorAssignment,
    Plan,
    PlanStatusHistory,
    PlanVersion,
)
from apps.plans.overview import build_overview
from apps.plans.serializers import (
    CoordinatorAssignmentSerializer,
    CopyPlanVersionSerializer,
    CostCodeDetailSerializer,
    CostCodeUpdateSerializer,
    PlanStatusHistorySerializer,
    PlanVersionSerializer,
)
from apps.plans.services import (
    assign_coordinator,
    record_cost_code_change,
    remove_coordinator,
)
from apps.plans.versioning import (
    PlanVersionError,
    copy_plan_version,
    ensure_current_version,
)

#: Fields whose before/after values are worth an audit entry.
AUDITED_COST_CODE_FIELDS = (
    "cost_code",
    "process_id",
    "subprocess_id",
    "region_id",
    "bu_lead_id",
    "lob_id",
    "center_id",
    "location_id",
)

CanEditCostCode = role_required(*AUTHORING_ROLES)
CanAssignCoordinator = role_required(RoleCode.ADMIN, RoleCode.COORDINATOR, RoleCode.BU_LEAD)


class CostCodeDetailView(ScopedQuerySetMixin, RetrieveUpdateAPIView):
    """Read and edit one cost code (Phase 3.1)."""

    estate_scope_path = "estate_id"
    cost_code_scope_path = ""
    permission_classes = [IsAuthenticated, IsActiveUser]
    http_method_names = ["get", "patch", "head", "options"]
    lookup_url_kwarg = "cost_code_id"

    def get_queryset(self):
        # `scope_queryset` must be called explicitly: defining `get_queryset` here
        # overrides the mixin's own, which is where the scoping would otherwise be
        # applied. Forgetting it does not fail loudly — it silently serves any
        # cost code in the database to any authenticated user.
        return self.scope_queryset(
            CostCode.objects.select_related(
                "process",
                "subprocess",
                "region",
                "bu_lead",
                "lob",
                "center",
                "location",
                "estate",
            )
        )

    def get_permissions(self):
        if self.request.method == "PATCH":
            return [IsAuthenticated(), IsActiveUser(), CanEditCostCode()]
        return super().get_permissions()

    def get_serializer_class(self):
        return (
            CostCodeUpdateSerializer if self.request.method == "PATCH" else CostCodeDetailSerializer
        )

    def perform_update(self, serializer):
        before = {name: getattr(serializer.instance, name) for name in AUDITED_COST_CODE_FIELDS}
        cost_code = serializer.save()
        record_cost_code_change(
            cost_code=cost_code,
            before=before,
            actor=self.request.user,
            request=self.request,
        )

    def update(self, request, *args, **kwargs):
        """Respond with the full detail shape, not the sparse write shape.

        The edit drawer re-renders from this response; handing back only the
        written fields would blank every label it did not send.
        """
        super().update(request, *args, **kwargs)
        instance = self.get_object()
        return Response(CostCodeDetailSerializer(instance).data)


class CostCodePlanVersionsView(ScopedQuerySetMixin, APIView):
    """Every version of a cost code's plan, newest first (Phase 3.3).

    Current versus previous is derived, not stored: the current version is simply
    the highest version number. Storing an `is_current` flag would give two places
    to disagree, and they eventually would.
    """

    estate_scope_path = "estate_id"
    cost_code_scope_path = ""
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get_cost_code(self) -> CostCode:
        return get_object_or_404(
            self.scope_queryset(CostCode.objects.select_related("estate", "process")),
            pk=self.kwargs["cost_code_id"],
        )

    @extend_schema(responses=PlanVersionSerializer(many=True))
    def get(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        plan = Plan.objects.filter(cost_code=cost_code).first()
        if plan is None:
            return Response({"plan_id": None, "versions": []})

        versions = (
            PlanVersion.objects.filter(plan=plan)
            .select_related("approved_by", "created_by", "plan", "plan__cost_code")
            .prefetch_related(
                Prefetch(
                    "coordinator_assignments",
                    queryset=CoordinatorAssignment.objects.select_related("employee"),
                )
            )
            .order_by("-version_number")
        )
        serializer = PlanVersionSerializer(versions, many=True, context=version_list_context(plan))
        return Response({"plan_id": plan.pk, "versions": serializer.data})

    @extend_schema(
        request=None,
        responses={201: PlanVersionSerializer, 200: PlanVersionSerializer},
        summary="Create the plan and its first version if they do not exist (3.6)",
    )
    def post(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        if not CanEditCostCode().has_permission(request, self):
            return Response(
                {"detail": "Your role does not permit this action.", "code": "forbidden"},
                status=http_status.HTTP_403_FORBIDDEN,
            )
        if cost_code.process_id is None:
            return Response(
                {
                    "detail": "This cost code has no process, so a plan cannot be created.",
                    "code": "cost_code_has_no_process",
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        plan, _ = Plan.objects.get_or_create(cost_code=cost_code, process=cost_code.process)
        existed = PlanVersion.objects.filter(plan=plan).exists()
        version = ensure_current_version(plan, actor=request.user)

        return Response(
            PlanVersionSerializer(version, context=version_list_context(plan)).data,
            status=http_status.HTTP_200_OK if existed else http_status.HTTP_201_CREATED,
        )


class PlanVersionDetailView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=PlanVersionSerializer)
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        return Response(
            PlanVersionSerializer(version, context=version_list_context(version.plan)).data
        )


class PlanVersionOverviewView(PlanVersionScopedMixin, APIView):
    """Recovery objectives, BIA / RA / Plan statuses and the people behind the
    cost code — the editor's hub between the questionnaire and the parts."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=OpenApiResponse(description="See apps.plans.overview"))
    def get(self, request, *args, **kwargs):
        return Response(build_overview(self.get_plan_version()))


class PlanVersionCopyView(PlanVersionScopedMixin, APIView):
    """Copy-on-write (Phase 3.4)."""

    permission_classes = [IsAuthenticated, IsActiveUser, CanEditCostCode]

    @extend_schema(
        request=CopyPlanVersionSerializer,
        responses={
            201: PlanVersionSerializer,
            400: OpenApiResponse(description="The version cannot be copied right now."),
        },
        summary="Create a new editable version from this one",
    )
    def post(self, request, *args, **kwargs):
        source = self.get_plan_version()
        body = CopyPlanVersionSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            target = copy_plan_version(
                source,
                actor=request.user,
                comments=body.validated_data.get("comments", ""),
            )
        except PlanVersionError as error:
            return Response(
                {"detail": str(error), "code": error.code, "field_errors": {}},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            PlanVersionSerializer(target, context=version_list_context(target.plan)).data,
            status=http_status.HTTP_201_CREATED,
        )


class PlanVersionHistoryView(PlanVersionScopedMixin, APIView):
    """The history timeline (Phase 3.5) — oldest first, as a story reads."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=PlanStatusHistorySerializer(many=True))
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        entries = (
            PlanStatusHistory.objects.filter(plan_version=version)
            .select_related("changed_by")
            .order_by("changed_at", "plan_status_history_id")
        )
        return Response(PlanStatusHistorySerializer(entries, many=True).data)


class CoordinatorAssignmentListView(PlanVersionScopedMixin, ListCreateAPIView):
    """Assign and list coordinators for a plan version (Phase 3.2)."""

    serializer_class = CoordinatorAssignmentSerializer
    permission_classes = [IsAuthenticated, IsActiveUser]
    pagination_class = None

    #: Scope comes from the parent: `get_plan_version()` resolves the version
    #: through the caller's estate scope and 404s outside it, so every assignment
    #: reachable here is already in scope. Set to None so the inherited
    #: `plan__cost_code__estate_id` path — which does not exist on this model — is
    #: never applied to it.
    estate_scope_path = None

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), IsActiveUser(), CanAssignCoordinator()]
        return super().get_permissions()

    def get_queryset(self):
        return (
            CoordinatorAssignment.objects.filter(plan_version=self.get_plan_version())
            .select_related("employee")
            .order_by("employee__full_name")
        )

    def create(self, request, *args, **kwargs):
        plan_version = self.get_plan_version()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assignment = assign_coordinator(
            plan_version=plan_version,
            employee=serializer.validated_data["employee"],
            coordinator_type=serializer.validated_data.get("coordinator_type", ""),
            additional_user_flag=serializer.validated_data.get("additional_user_flag", False),
            actor=request.user,
            request=request,
        )
        return Response(
            CoordinatorAssignmentSerializer(assignment).data,
            status=http_status.HTTP_201_CREATED,
        )


class CoordinatorAssignmentDetailView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser, CanAssignCoordinator]

    @extend_schema(responses={204: None}, summary="Unassign a coordinator")
    def delete(self, request, *args, **kwargs):
        assignment = get_object_or_404(
            CoordinatorAssignment.objects.filter(plan_version=self.get_plan_version()),
            pk=self.kwargs["assignment_id"],
        )
        remove_coordinator(assignment, actor=request.user, request=request)
        return Response(status=http_status.HTTP_204_NO_CONTENT)
