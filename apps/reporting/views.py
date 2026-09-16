"""
Dashboards and reports (Phase 9.2-9.4).

    GET  /dashboard/?estate=            every figure, in the caller's scope
    GET  /reports/types/                report types, formats and schedules
    GET  /reports/requests/             the caller's requests, newest first
    POST /reports/requests/             request (and optionally schedule) a report
    GET  /reports/requests/{id}/        one, with its download attachment id
    POST /reports/requests/{id}/run/    run a scheduled report now
    POST /reports/requests/{id}/stop/   end a schedule
    GET  /reports/estate-detail/?estate=&file_format=   the estate detail report, inline
"""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.scoping import ScopeResolver
from apps.core.tasks import enqueue_after_commit
from apps.documents.models import EntityDocument
from apps.organization.models import Estate
from apps.reporting import exports, reports, services
from apps.reporting.metrics import build_dashboard
from apps.reporting.models import ReportFormat, ReportRequest, ReportType, Schedule


def _estate_in_scope(scope, raw) -> int | None:
    """The estate id if it is a real estate the caller may see, else None (= all)."""
    if raw in (None, ""):
        return None
    try:
        estate_id = int(raw)
    except (TypeError, ValueError):
        return None
    rows = Estate.objects.filter(pk=estate_id)
    if not scope.sees_all_estates:
        rows = rows.filter(pk__in=scope.estate_ids)
    return estate_id if rows.exists() else None


class DashboardView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        scope = ScopeResolver(request.user)
        return Response(
            build_dashboard(
                scope, estate_id=_estate_in_scope(scope, request.query_params.get("estate"))
            )
        )


class ReportTypesView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        return Response(
            {
                "types": [
                    {"code": code, "label": label, "formats": sorted(services.FORMATS[code])}
                    for code, label in ReportType.choices
                ],
                "schedules": [{"code": c, "label": lbl} for c, lbl in Schedule.choices],
            }
        )


class ReportRequestSerializer(serializers.ModelSerializer):
    report_type_label = serializers.CharField(source="get_report_type_display", read_only=True)
    document = serializers.SerializerMethodField()

    class Meta:
        model = ReportRequest
        fields = [
            "report_request_id",
            "report_type",
            "report_type_label",
            "report_format",
            "parameters",
            "schedule",
            "active_flag",
            "next_run_at",
            "status",
            "last_error",
            "run_count",
            "last_run_at",
            "created_at",
            "document",
        ]

    def get_document(self, request):
        if not request.document_id:
            return None
        attachments = self.context.get("attachments")
        if attachments is not None:
            attachment_id = attachments.get((request.document_id, request.pk))
        else:
            attachment = services.latest_attachment(request)
            attachment_id = attachment.pk if attachment else None
        if attachment_id is None:
            return None
        return {
            "entity_document_id": attachment_id,
            "file_name": request.document.file_name,
            "file_size_bytes": request.document.file_size_bytes,
        }


class CreateRequestSerializer(serializers.Serializer):
    report_type = serializers.ChoiceField(choices=ReportType.choices)
    report_format = serializers.ChoiceField(choices=ReportFormat.choices, default=ReportFormat.XLSX)
    schedule = serializers.ChoiceField(choices=Schedule.choices, default=Schedule.ONCE)
    estate_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["report_format"] not in services.FORMATS[attrs["report_type"]]:
            raise serializers.ValidationError(
                {"report_format": "That format is not available for this report."}
            )
        return attrs


class ReportRequestListView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=ReportRequestSerializer(many=True))
    def get(self, request, *args, **kwargs):
        rows = list(
            ReportRequest.objects.filter(requested_by=request.user).select_related("document")[:100]
        )
        attachments = {
            (document_id, entity_id): pk
            for document_id, entity_id, pk in EntityDocument.objects.filter(
                entity_type=EntityDocument.EntityType.REPORT_REQUEST,
                entity_id__in=[r.pk for r in rows],
            ).values_list("document_id", "entity_id", "pk")
        }
        return Response(
            ReportRequestSerializer(rows, many=True, context={"attachments": attachments}).data
        )

    @extend_schema(request=CreateRequestSerializer, responses={201: ReportRequestSerializer})
    def post(self, request, *args, **kwargs):
        body = CreateRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        scope = ScopeResolver(request.user)
        parameters = {}
        requested_estate = body.validated_data.get("estate_id")
        if requested_estate is not None:
            estate_id = _estate_in_scope(scope, requested_estate)
            if estate_id is None:
                raise serializers.ValidationError(
                    {"estate_id": "That estate is not in your scope."}
                )
            parameters["estate_id"] = estate_id
        created = services.create_request(
            user=request.user,
            report_type=body.validated_data["report_type"],
            report_format=body.validated_data["report_format"],
            parameters=parameters,
            schedule=body.validated_data["schedule"],
        )
        created = ReportRequest.objects.select_related("document").get(pk=created.pk)
        return Response(ReportRequestSerializer(created).data, status=http_status.HTTP_201_CREATED)


class ReportRequestDetailView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    action_name = ""

    def _request(self, request) -> ReportRequest:
        return get_object_or_404(
            ReportRequest.objects.filter(requested_by=request.user).select_related("document"),
            pk=self.kwargs["report_request_id"],
        )

    @extend_schema(responses=ReportRequestSerializer)
    def get(self, request, *args, **kwargs):
        return Response(ReportRequestSerializer(self._request(request)).data)

    def post(self, request, *args, **kwargs):
        row = self._request(request)
        if self.action_name == "run":
            from apps.reporting.tasks import run_report_request

            enqueue_after_commit(lambda: run_report_request.delay(row.pk))
        elif self.action_name == "stop":
            row.active_flag = False
            row.next_run_at = None
            row.save(update_fields=["active_flag", "next_run_at"])
        row = ReportRequest.objects.select_related("document").get(pk=row.pk)
        return Response(ReportRequestSerializer(row).data)


class EstateDetailReportView(APIView):
    """The legacy BU details report, straight to the browser."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        scope = ScopeResolver(request.user)
        # Not `?format=`: DRF reserves that query parameter for renderer selection
        # and answers 404 for a format it does not know.
        report_format = request.query_params.get("file_format", "xlsx")
        if report_format not in exports.RENDERERS:
            return Response(
                {"detail": "Unknown format.", "code": "bad_format", "field_errors": {}}, status=400
            )
        estate_id = _estate_in_scope(scope, request.query_params.get("estate"))
        table = reports.build(ReportType.ESTATE_DETAIL, request.user, {"estate_id": estate_id})
        content, mime = exports.render(table, report_format)
        response = HttpResponse(content, content_type=mime)
        response["Content-Disposition"] = (
            f'attachment; filename="estate-detail-report.{report_format}"'
        )
        return response
