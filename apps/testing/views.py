"""
Test endpoints (Phase 8).

    GET       /tests/?from=&to=&status=&test_type=&estate=   calendar / list, in scope
    GET/POST  /cost-codes/{id}/tests/                        the cost code's tests / schedule one
    GET/PATCH /tests/{id}/                                   detail with outcomes / reschedule
    POST      /tests/{id}/cancel/
    POST      /tests/{id}/start-call-tree/                   Call Tree Test only
    POST      /tests/{id}/outcome/                           multipart: fields + optional report
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.calltree.serializers import RunSummarySerializer
from apps.crisis.access import (
    CostCodeScopedMixin,
    NotACostCodeManager,
    caller_may_manage,
    manageable_cost_code_ids,
)
from apps.documents.models import EntityDocument
from apps.testing import services
from apps.testing.models import Test, TestOutcome


class OutcomeSerializer(serializers.ModelSerializer):
    report = serializers.SerializerMethodField()

    class Meta:
        model = TestOutcome
        fields = [
            "test_outcome_id",
            "conducted_date",
            "conducted_time",
            "result",
            "final_status",
            "report",
            "created_at",
        ]

    def get_report(self, outcome):
        if not outcome.final_report_document_id:
            return None
        attachments = self.context.get("report_attachments")
        if attachments is not None:
            attachment_id = attachments.get(outcome.pk)
        else:
            attachment_id = getattr(services.report_attachment(outcome), "pk", None)
        if attachment_id is None:
            return None
        return {
            "entity_document_id": attachment_id,
            "file_name": outcome.final_report_document.file_name,
            "file_size_bytes": outcome.final_report_document.file_size_bytes,
        }


class TestSerializer(serializers.ModelSerializer):
    cost_code_id = serializers.IntegerField(source="plan_version.plan.cost_code_id", read_only=True)
    cost_code_label = serializers.CharField(
        source="plan_version.plan.cost_code.cost_code", read_only=True
    )
    process_name = serializers.SerializerMethodField()
    estate_name = serializers.SerializerMethodField()
    version_number = serializers.IntegerField(source="plan_version.version_number", read_only=True)
    initiated_by_name = serializers.SerializerMethodField()
    outcomes = OutcomeSerializer(many=True, read_only=True)
    call_tree_run = RunSummarySerializer(read_only=True)
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Test
        fields = [
            "test_id",
            "plan_version_id",
            "version_number",
            "cost_code_id",
            "cost_code_label",
            "process_name",
            "estate_name",
            "test_type",
            "scheduled_date",
            "scheduled_time",
            "status",
            "comments",
            "initiated_by_name",
            "call_tree_run",
            "outcomes",
            "created_at",
            "can_manage",
        ]

    def get_process_name(self, test):
        return getattr(test.plan_version.plan.cost_code.process, "process_name", "")

    def get_estate_name(self, test):
        return getattr(test.plan_version.plan.cost_code.estate, "estate_name", "")

    def get_initiated_by_name(self, test):
        return test.initiated_by.display_name if test.initiated_by_id else ""

    def get_can_manage(self, test):
        manageable = self.context.get("manageable")
        if manageable is not None:
            return test.plan_version.plan.cost_code_id in manageable
        scope = self.context.get("scope")
        return bool(scope and caller_may_manage(scope, test.plan_version.plan.cost_code))


def test_context(scope, tests) -> dict:
    """List context: manageability and report attachments resolved in two queries, not two per row."""
    tests = list(tests)
    outcome_ids = [o.pk for t in tests for o in t.outcomes.all() if o.final_report_document_id]
    attachments = {}
    if outcome_ids:
        attachments = dict(
            EntityDocument.objects.filter(
                entity_type=EntityDocument.EntityType.TEST_OUTCOME, entity_id__in=outcome_ids
            ).values_list("entity_id", "pk")
        )
    return {
        "scope": scope,
        "manageable": manageable_cost_code_ids(
            scope, {t.plan_version.plan.cost_code for t in tests}
        ),
        "report_attachments": attachments,
    }


class ScheduleSerializer(serializers.Serializer):
    test_type = serializers.ChoiceField(choices=Test.TestType.choices)
    scheduled_date = serializers.DateField()
    scheduled_time = serializers.TimeField(required=False, allow_null=True)
    comments = serializers.CharField(required=False, allow_blank=True, max_length=4000, default="")


class RescheduleSerializer(serializers.Serializer):
    test_type = serializers.ChoiceField(choices=Test.TestType.choices, required=False)
    scheduled_date = serializers.DateField(required=False)
    scheduled_time = serializers.TimeField(required=False, allow_null=True)
    comments = serializers.CharField(required=False, allow_blank=True, max_length=4000)


class StartCallTreeSerializer(serializers.Serializer):
    simulation = serializers.BooleanField(default=True)


class OutcomeInputSerializer(serializers.Serializer):
    conducted_date = serializers.DateField()
    conducted_time = serializers.TimeField(required=False, allow_null=True)
    result = serializers.CharField(required=False, allow_blank=True, max_length=8000, default="")
    final_status = serializers.ChoiceField(choices=TestOutcome.FinalStatus.choices)


def _tests_queryset():
    return Test.objects.select_related(
        "plan_version__plan__cost_code__process",
        "plan_version__plan__cost_code__estate",
        "plan_version__plan__cost_code__bu_lead",
        "initiated_by",
        "call_tree_run",
    ).prefetch_related("outcomes__final_report_document", "call_tree_run__members")


class TestFilter(filters.FilterSet):
    status = filters.CharFilter(field_name="status")
    test_type = filters.CharFilter(field_name="test_type")
    estate = filters.NumberFilter(field_name="plan_version__plan__cost_code__estate_id")
    cost_code = filters.CharFilter(
        field_name="plan_version__plan__cost_code__cost_code", lookup_expr="icontains"
    )
    date_from = filters.DateFilter(field_name="scheduled_date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="scheduled_date", lookup_expr="lte")

    class Meta:
        model = Test
        fields = ["status", "test_type", "estate", "cost_code", "date_from", "date_to"]


class TestListView(ScopedQuerySetMixin, ListAPIView):
    estate_scope_path = "plan_version__plan__cost_code__estate_id"
    # Tests belong to a plan: the same own-record cut as the plan itself.
    cost_code_scope_path = "plan_version__plan__cost_code_id"
    permission_classes = [IsAuthenticated, IsActiveUser]
    serializer_class = TestSerializer
    filterset_class = TestFilter
    filter_backends = [filters.DjangoFilterBackend]

    def get_queryset(self):
        return self.scope_queryset(
            _tests_queryset().order_by("scheduled_date", "scheduled_time", "pk")
        )

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "scope": self.get_scope()}

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = page if page is not None else list(queryset)
        context = {**self.get_serializer_context(), **test_context(self.get_scope(), rows)}
        data = TestSerializer(rows, many=True, context=context).data
        return self.get_paginated_response(data) if page is not None else Response(data)


class CostCodeTestsView(CostCodeScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=TestSerializer(many=True))
    def get(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        rows = list(
            _tests_queryset()
            .filter(plan_version__plan__cost_code=cost_code)
            .order_by("-scheduled_date", "-pk")
        )
        return Response(
            TestSerializer(rows, many=True, context=test_context(self.get_scope(), rows)).data
        )

    @extend_schema(request=ScheduleSerializer, responses={201: TestSerializer})
    def post(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        self.require_manager(cost_code)
        body = ScheduleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        test = services.schedule_test(cost_code, actor=request.user, data=body.validated_data)
        test = _tests_queryset().get(pk=test.pk)
        return Response(
            TestSerializer(test, context={"scope": self.get_scope()}).data,
            status=http_status.HTTP_201_CREATED,
        )


class TestScopedMixin(ScopedQuerySetMixin):
    estate_scope_path = "plan_version__plan__cost_code__estate_id"
    cost_code_scope_path = "plan_version__plan__cost_code_id"
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get_test(self, *, manage: bool = False) -> Test:
        test = get_object_or_404(self.scope_queryset(_tests_queryset()), pk=self.kwargs["test_id"])
        if manage and not caller_may_manage(self.get_scope(), test.plan_version.plan.cost_code):
            raise NotACostCodeManager()
        return test

    def respond(self, test, status=http_status.HTTP_200_OK):
        test = _tests_queryset().get(pk=test.pk)
        return Response(
            TestSerializer(test, context={"scope": self.get_scope()}).data, status=status
        )


class TestDetailView(TestScopedMixin, APIView):
    @extend_schema(responses=TestSerializer)
    def get(self, request, *args, **kwargs):
        return self.respond(self.get_test())

    @extend_schema(request=RescheduleSerializer, responses=TestSerializer)
    def patch(self, request, *args, **kwargs):
        test = self.get_test(manage=True)
        body = RescheduleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.reschedule_test(test, actor=request.user, data=body.validated_data)
        return self.respond(test)


class TestCancelView(TestScopedMixin, APIView):
    def post(self, request, *args, **kwargs):
        test = self.get_test(manage=True)
        services.cancel_test(test, actor=request.user)
        return self.respond(test)


class TestStartCallTreeView(TestScopedMixin, APIView):
    @extend_schema(request=StartCallTreeSerializer, responses=TestSerializer)
    def post(self, request, *args, **kwargs):
        test = self.get_test(manage=True)
        body = StartCallTreeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.start_call_tree(
            test, actor=request.user, simulation=body.validated_data["simulation"]
        )
        return self.respond(test)


class TestOutcomeView(TestScopedMixin, APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(request=OutcomeInputSerializer, responses={201: TestSerializer})
    def post(self, request, *args, **kwargs):
        test = self.get_test(manage=True)
        body = OutcomeInputSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.record_outcome(
            test, actor=request.user, data=body.validated_data, report=request.FILES.get("report")
        )
        return self.respond(test, status=http_status.HTTP_201_CREATED)
