"""
Crisis management endpoints (Phase 8).

    GET/POST  /cost-codes/{id}/cmsc-members/           roster
    POST      /cost-codes/{id}/cmsc-members/upload/    CSV upsert
    PATCH/DEL /cmsc-members/{id}/                      edit / remove (soft)
    GET       /crisis-events/                          every event in scope
    GET/POST  /cost-codes/{id}/crisis-events/          the cost code's events / declare one
    GET       /crisis-events/{id}/                     one, with its call tree run summary
    POST      /crisis-events/{id}/initiate/            start (or restart) the call tree
    POST      /crisis-events/{id}/close/
    POST      /crisis-events/{id}/cancel/
"""

from __future__ import annotations

from django.conf import settings
from django.shortcuts import get_object_or_404
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.calltree.serializers import RunSummarySerializer
from apps.crisis import services
from apps.crisis.access import CostCodeScopedMixin, caller_may_manage, manageable_cost_code_ids
from apps.crisis.models import CmscMember, CrisisEvent

# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #


class CmscMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = CmscMember
        fields = [
            "cmsc_member_id",
            "cost_code_id",
            "member_name",
            "member_email",
            "country_code",
            "phone_number",
            "reporting_manager_name",
            "reporting_manager_email",
            "center",
            "location",
            "updated_at",
        ]
        read_only_fields = ["cmsc_member_id", "cost_code_id", "updated_at"]

    def validate(self, attrs):
        merged = {**getattr(self.instance, "__dict__", {}), **attrs}
        if not merged.get("member_email") and not merged.get("phone_number"):
            raise serializers.ValidationError(
                {"member_email": "An email or a phone number is required."}
            )
        return attrs


class RosterView(CostCodeScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=CmscMemberSerializer(many=True))
    def get(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        rows = CmscMember.objects.filter(cost_code=cost_code).order_by("member_name")
        return Response(
            {
                "results": CmscMemberSerializer(rows, many=True).data,
                "can_manage": caller_may_manage(self.get_scope(), cost_code),
            }
        )

    @extend_schema(request=CmscMemberSerializer, responses={201: CmscMemberSerializer})
    def post(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        self.require_manager(cost_code)
        body = CmscMemberSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        member = body.save(
            cost_code=cost_code,
            plan_version=services.current_version(cost_code),
            process=cost_code.process,
            region=cost_code.region,
            bu_lead=cost_code.bu_lead,
            created_by=request.user,
        )
        return Response(CmscMemberSerializer(member).data, status=http_status.HTTP_201_CREATED)


class RosterUploadView(CostCodeScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
            }
        }
    )
    def post(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        self.require_manager(cost_code)
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {
                    "detail": "Attach a CSV file as 'file'.",
                    "code": "file_required",
                    "field_errors": {},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        if upload.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            return Response(
                {"detail": "The file is too large.", "code": "file_too_large", "field_errors": {}},
                status=413,
            )
        result = services.upload_roster(cost_code, upload.read(), actor=request.user)
        code = http_status.HTTP_400_BAD_REQUEST if result["errors"] else http_status.HTTP_200_OK
        return Response(result, status=code)


class CmscMemberDetailView(ScopedQuerySetMixin, APIView):
    estate_scope_path = "cost_code__estate_id"
    permission_classes = [IsAuthenticated, IsActiveUser]

    def _member(self) -> CmscMember:
        member = get_object_or_404(
            self.scope_queryset(CmscMember.objects.select_related("cost_code__bu_lead")),
            pk=self.kwargs["cmsc_member_id"],
        )
        if not caller_may_manage(self.get_scope(), member.cost_code):
            from apps.crisis.access import NotACostCodeManager

            raise NotACostCodeManager()
        return member

    @extend_schema(request=CmscMemberSerializer, responses=CmscMemberSerializer)
    def patch(self, request, *args, **kwargs):
        member = self._member()
        body = CmscMemberSerializer(member, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        body.save()
        return Response(body.data)

    def delete(self, request, *args, **kwargs):
        member = self._member()
        member.soft_delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class CrisisEventSerializer(serializers.ModelSerializer):
    cost_code_label = serializers.CharField(source="cost_code.cost_code", read_only=True)
    process_name = serializers.SerializerMethodField()
    estate_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    call_tree_run = RunSummarySerializer(read_only=True)
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = CrisisEvent
        fields = [
            "crisis_event_id",
            "cost_code_id",
            "cost_code_label",
            "process_name",
            "estate_name",
            "plan_version_id",
            "csd_ticket_number",
            "event_type",
            "comments",
            "event_date",
            "event_time",
            "initiated_flag",
            "status",
            "call_tree_run",
            "created_by_name",
            "created_at",
            "can_manage",
        ]

    def get_process_name(self, event):
        return getattr(event.cost_code.process, "process_name", "")

    def get_estate_name(self, event):
        return getattr(event.cost_code.estate, "estate_name", "")

    def get_created_by_name(self, event):
        return event.created_by.display_name if event.created_by_id else ""

    def get_can_manage(self, event):
        manageable = self.context.get("manageable")
        if manageable is not None:
            return event.cost_code_id in manageable
        scope = self.context.get("scope")
        return bool(scope and caller_may_manage(scope, event.cost_code))


def event_context(scope, events) -> dict:
    """Serializer context with the manageability of every listed cost code resolved once."""
    return {
        "scope": scope,
        "manageable": manageable_cost_code_ids(scope, {e.cost_code for e in events}),
    }


class CreateEventSerializer(serializers.Serializer):
    event_type = serializers.ChoiceField(choices=CrisisEvent.EventType.choices)
    csd_ticket_number = serializers.CharField(
        required=False, allow_blank=True, max_length=80, default=""
    )
    comments = serializers.CharField(required=False, allow_blank=True, max_length=4000, default="")
    event_date = serializers.DateField(required=False, allow_null=True)
    event_time = serializers.TimeField(required=False, allow_null=True)
    initiate = serializers.BooleanField(default=False)
    # Simulation is the default so a click never dials real people by accident;
    # a live run must be asked for, and is refused unless a provider is enabled.
    simulation = serializers.BooleanField(default=True)


class InitiateSerializer(serializers.Serializer):
    simulation = serializers.BooleanField(default=True)


class CloseSerializer(serializers.Serializer):
    comments = serializers.CharField(required=False, allow_blank=True, max_length=4000, default="")


def _events_queryset():
    return CrisisEvent.objects.select_related(
        "cost_code__process",
        "cost_code__estate",
        "cost_code__bu_lead",
        "created_by",
        "call_tree_run",
    ).prefetch_related("call_tree_run__members")


class EventFilter(filters.FilterSet):
    status = filters.CharFilter(field_name="status")
    event_type = filters.CharFilter(field_name="event_type")
    estate = filters.NumberFilter(field_name="cost_code__estate_id")
    cost_code = filters.CharFilter(field_name="cost_code__cost_code", lookup_expr="icontains")

    class Meta:
        model = CrisisEvent
        fields = ["status", "event_type", "estate", "cost_code"]


class EventListView(ScopedQuerySetMixin, ListAPIView):
    estate_scope_path = "cost_code__estate_id"
    permission_classes = [IsAuthenticated, IsActiveUser]
    serializer_class = CrisisEventSerializer
    filterset_class = EventFilter
    filter_backends = [filters.DjangoFilterBackend]

    def get_queryset(self):
        return self.scope_queryset(_events_queryset().order_by("-created_at"))

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "scope": self.get_scope()}

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = page if page is not None else list(queryset)
        context = {**self.get_serializer_context(), **event_context(self.get_scope(), rows)}
        data = CrisisEventSerializer(rows, many=True, context=context).data
        return self.get_paginated_response(data) if page is not None else Response(data)


class CostCodeEventsView(CostCodeScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=CrisisEventSerializer(many=True))
    def get(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        rows = list(_events_queryset().filter(cost_code=cost_code).order_by("-created_at"))
        return Response(
            CrisisEventSerializer(
                rows, many=True, context=event_context(self.get_scope(), rows)
            ).data
        )

    @extend_schema(request=CreateEventSerializer, responses={201: CrisisEventSerializer})
    def post(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        self.require_manager(cost_code)
        body = CreateEventSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        event = services.create_event(
            cost_code,
            actor=request.user,
            data=data,
            initiate=data["initiate"],
            simulation=data["simulation"],
        )
        event = _events_queryset().get(pk=event.pk)
        return Response(
            CrisisEventSerializer(event, context={"scope": self.get_scope()}).data,
            status=http_status.HTTP_201_CREATED,
        )


class EventScopedMixin(ScopedQuerySetMixin):
    estate_scope_path = "cost_code__estate_id"
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get_event(self) -> CrisisEvent:
        return get_object_or_404(
            self.scope_queryset(_events_queryset()), pk=self.kwargs["crisis_event_id"]
        )

    def respond(self, event):
        event = _events_queryset().get(pk=event.pk)
        return Response(CrisisEventSerializer(event, context={"scope": self.get_scope()}).data)


class EventDetailView(EventScopedMixin, APIView):
    @extend_schema(responses=CrisisEventSerializer)
    def get(self, request, *args, **kwargs):
        return self.respond(self.get_event())


class EventActionView(EventScopedMixin, APIView):
    """initiate / close / cancel, chosen by the URL."""

    action_name = ""

    def post(self, request, *args, **kwargs):
        event = self.get_event()
        from apps.crisis.access import NotACostCodeManager

        if not caller_may_manage(self.get_scope(), event.cost_code):
            raise NotACostCodeManager()
        if self.action_name == "initiate":
            body = InitiateSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            services.initiate_event(
                event, actor=request.user, simulation=body.validated_data["simulation"]
            )
        elif self.action_name == "close":
            body = CloseSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            services.close_event(
                event, actor=request.user, comments=body.validated_data["comments"]
            )
        else:
            services.cancel_event(event, actor=request.user)
        return self.respond(event)
