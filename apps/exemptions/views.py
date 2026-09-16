"""
Exemption endpoints (Phase 7.5).

    GET  /plan-versions/{id}/exemptions/          the version's requests, newest first
    POST /plan-versions/{id}/exemptions/          request one          (author)
    GET  /exemptions/{id}/                        one, with its comment trail
    POST /exemptions/{id}/approve/                                     (approver)
    POST /exemptions/{id}/reject/                                      (approver)
    POST /exemptions/{id}/rework/                 comment required     (approver)
    POST /exemptions/{id}/resubmit/               after a rework       (author)
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.exemptions import services
from apps.exemptions.models import Exemption, ExemptionComment
from apps.plans.access import PlanVersionScopedMixin
from apps.plans.childviews import NotAnAuthor
from apps.plans.models import PlanVersion


class NotAnApprover(NotAnAuthor):
    default_detail = "Only the cost code's BU lead or an administrator can decide an exemption."
    default_code = "not_an_approver"


class ExemptionCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = ExemptionComment
        fields = [
            "exemption_comment_id",
            "comment_type",
            "comment",
            "status",
            "author_name",
            "created_at",
        ]

    def get_author_name(self, comment):
        return comment.author.display_name if comment.author_id else "System"


class ExemptionSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.SerializerMethodField()
    comments = ExemptionCommentSerializer(many=True, read_only=True)

    class Meta:
        model = Exemption
        fields = [
            "exemption_id",
            "plan_version_id",
            "status",
            "reason",
            "answer_1",
            "answer_2",
            "answer_3",
            "requested_by_name",
            "created_at",
            "updated_at",
            "comments",
        ]

    def get_requested_by_name(self, exemption):
        return exemption.requested_by.display_name if exemption.requested_by_id else ""


class RequestSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=4000)
    answer_1 = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    answer_2 = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    answer_3 = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class DecisionSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, max_length=4000, default="")


class VersionExemptionsView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=ExemptionSerializer(many=True))
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        rows = (
            Exemption.objects.filter(plan_version=version)
            .select_related("requested_by")
            .prefetch_related("comments__author")
        )
        return Response(ExemptionSerializer(rows, many=True).data)

    @extend_schema(request=RequestSerializer, responses={201: ExemptionSerializer})
    def post(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if not self.caller_may_author(version):
            raise NotAnAuthor()
        body = RequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        exemption = services.request_exemption(
            version,
            actor=request.user,
            reason=body.validated_data["reason"],
            answers=body.validated_data,
        )
        return Response(ExemptionSerializer(exemption).data, status=http_status.HTTP_201_CREATED)


class ExemptionScopedMixin(PlanVersionScopedMixin):
    """Resolve an exemption through its plan version's scope."""

    def get_exemption(self) -> Exemption:
        exemption = get_object_or_404(
            Exemption.objects.select_related(
                "plan_version__plan__cost_code__bu_lead", "requested_by"
            ),
            pk=self.kwargs["exemption_id"],
        )
        # Reuse the plan-version scope check, which 404s outside scope.
        self.kwargs["plan_version_id"] = exemption.plan_version_id
        self.get_plan_version()
        return exemption


class ExemptionDetailView(ExemptionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=ExemptionSerializer)
    def get(self, request, *args, **kwargs):
        return Response(ExemptionSerializer(self.get_exemption()).data)


class ExemptionDecisionView(ExemptionScopedMixin, APIView):
    """approve / reject / rework, chosen by the URL."""

    permission_classes = [IsAuthenticated, IsActiveUser]
    decision: str = ""

    @extend_schema(request=DecisionSerializer, responses=ExemptionSerializer)
    def post(self, request, *args, **kwargs):
        exemption = self.get_exemption()
        version: PlanVersion = self.get_plan_version()
        if not self.caller_may_approve(version):
            raise NotAnApprover()
        body = DecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        comment = body.validated_data["comment"]
        handler = {
            "approve": services.approve_exemption,
            "reject": services.reject_exemption,
            "rework": services.rework_exemption,
        }[self.decision]
        decided = handler(exemption, actor=request.user, comment=comment)
        return Response(ExemptionSerializer(decided).data)


class ExemptionResubmitView(ExemptionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(request=RequestSerializer, responses=ExemptionSerializer)
    def post(self, request, *args, **kwargs):
        exemption = self.get_exemption()
        version = self.get_plan_version()
        if not self.caller_may_author(version):
            raise NotAnAuthor()
        body = RequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        decided = services.resubmit_exemption(
            exemption, actor=request.user, reason=body.validated_data["reason"]
        )
        return Response(ExemptionSerializer(decided).data)
