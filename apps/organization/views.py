"""
Estate and cost code endpoints — journey steps 2 and 3.

    GET /api/v1/estates/                              the landing screen
    GET /api/v1/estates/{id}/                         one estate
    GET /api/v1/estates/{id}/cost-codes/              the filtered table
    GET /api/v1/estates/{id}/cost-code-filters/       facet options for the bar

Every queryset here goes through `ScopedQuerySetMixin` (AD-3). The cost code list
is the endpoint most exposed to a scoping mistake: it is the highest-traffic list
in the application and it returns commercially sensitive rows, so it is scoped in
`get_queryset()` and the estate in the URL is resolved through the *scoped* estate
queryset — an estate outside the caller's scope 404s rather than yielding an empty
list, so a misconfigured grant looks like a missing estate instead of an empty one.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.organization.filters import CostCodeFilterSet
from apps.organization.models import CostCode, Estate
from apps.organization.querysets import (
    CURRENT_STATUS,
    cost_code_list_queryset,
    empty_rollup,
    status_rollup_by_estate,
    with_current_status,
)
from apps.organization.serializers import (
    CostCodeListSerializer,
    EstateSerializer,
    FilterOptionsSerializer,
)


class EstateViewSet(ScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Journey step 2 — the list a user lands on after logging in.

    Estates are master data maintained by administrators, so this is read-only.
    """

    estate_scope_path = ""  # the model *is* Estate
    serializer_class = EstateSerializer
    permission_classes = [IsAuthenticated, IsActiveUser]
    filter_backends = [OrderingFilter]
    ordering_fields = ["estate_name", "cost_code_count"]
    ordering = ["estate_name"]
    search_fields = ["estate_name"]

    def get_queryset(self):
        # The count on the card must match the list behind it: for a coordinator
        # or BU lead that is their own cost codes, not the estate's.
        scope = self.get_scope()
        counted = Q(cost_codes__active_flag=True)
        if scope.narrowed_to_own:
            counted &= Q(cost_codes__in=scope.own_cost_code_ids())
        queryset = Estate.objects.annotate(
            cost_code_count=Count("cost_codes", filter=counted, distinct=True)
        )
        return self.scope_queryset(queryset)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["empty_rollup"] = empty_rollup()
        return context

    def list(self, request, *args, **kwargs):
        """Two queries' worth of rollup, whatever the number of estates.

        The rollup is resolved for the page only, after pagination — resolving it
        for every estate the user can see would make the cost of page 1 depend on
        the size of the whole scope.
        """
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        estates = page if page is not None else list(queryset)

        context = self.get_serializer_context()
        context["status_rollup"] = status_rollup_by_estate([estate.estate_id for estate in estates])

        serializer = self.get_serializer(estates, many=True, context=context)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        estate = self.get_object()
        context = self.get_serializer_context()
        context["status_rollup"] = status_rollup_by_estate([estate.estate_id])
        return Response(self.get_serializer(estate, context=context).data)


class EstateScopedMixin(ScopedQuerySetMixin):
    """Resolves `estate_id` from the URL through the caller's estate scope."""

    estate_scope_path = "estate_id"
    # The rows are cost codes: a coordinator sees the ones they are assigned to,
    # a BU lead the ones they lead (see ScopeResolver.narrowed_to_own).
    cost_code_scope_path = ""

    def get_estate(self) -> Estate:
        if not hasattr(self, "_estate"):
            scoped_estates = self.scope_estates(Estate.objects.all())
            self._estate = get_object_or_404(scoped_estates, pk=self.kwargs["estate_id"])
        return self._estate

    def scope_estates(self, queryset):
        """Scope a queryset *of estates*, independently of this view's own path."""
        scope = self.get_scope()
        if not scope.is_authenticated:
            return queryset.none()
        if scope.sees_all_estates:
            return queryset
        if not scope.estate_ids:
            return queryset.none()
        return queryset.filter(pk__in=scope.estate_ids)


@extend_schema(
    parameters=[
        OpenApiParameter("cost_code", str, description="Cost code contains (case-insensitive)"),
        OpenApiParameter("process", int, many=True, description="Process id; repeatable"),
        OpenApiParameter("subprocess", int, many=True, description="Subprocess id; repeatable"),
        OpenApiParameter("region", int, many=True, description="Region id; repeatable"),
        OpenApiParameter("bu_lead", int, many=True, description="BU lead id; repeatable"),
        OpenApiParameter("bcp_status", str, many=True, description="BCP status; repeatable"),
        OpenApiParameter("search", str, description="Free text across code, process, BU lead"),
        OpenApiParameter("ordering", str, description="Sort field; prefix with - to reverse"),
    ],
    summary="Cost codes in an estate, filtered (journey step 3)",
)
class CostCodeListView(EstateScopedMixin, ListAPIView):
    """Journey step 3 — the screen users spend their day in."""

    serializer_class = CostCodeListSerializer
    permission_classes = [IsAuthenticated, IsActiveUser]
    filterset_class = CostCodeFilterSet
    ordering_fields = [
        "cost_code",
        "process__process_name",
        "subprocess__subprocess_name",
        "region__region_name",
        "bu_lead__lead_name",
        CURRENT_STATUS,
        "current_version_number",
    ]
    ordering = ["cost_code"]

    def get_queryset(self):
        estate = self.get_estate()
        queryset = cost_code_list_queryset(CostCode.objects.filter(estate=estate))
        # Scoping again, on top of a URL estate already resolved through scope, is
        # deliberate belt-and-braces: if this view is ever subclassed onto a
        # different lookup, it still cannot outrun the mixin.
        return self.scope_queryset(queryset)


class CostCodeFilterOptionsView(EstateScopedMixin, APIView):
    """Facet options for the filter bar (Phase 2.4).

    Options come from the distinct values *present in this estate*, not from the
    global master-data tables. An estate with four processes should offer four
    choices; offering the organisation's full list of two hundred and letting the
    user pick one that returns nothing is a worse screen.

    One query per facet, so the cost is fixed at six regardless of estate size.
    """

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=FilterOptionsSerializer, summary="Filter facets for an estate")
    def get(self, request, *args, **kwargs):
        estate = self.get_estate()
        in_estate = Q(cost_codes__estate=estate, cost_codes__active_flag=True)

        payload = {
            "process": self._facet("process", in_estate, "process_id", "process_name"),
            "subprocess": self._facet("subprocess", in_estate, "subprocess_id", "subprocess_name"),
            "region": self._facet("region", in_estate, "region_id", "region_name"),
            "bu_lead": self._facet("bu_lead", in_estate, "bu_lead_id", "lead_name"),
            "bcp_status": self._status_facet(estate),
        }
        return Response(payload)

    def _facet(self, field: str, condition: Q, id_attr: str, name_attr: str) -> list[dict]:
        """Distinct related rows referenced by the estate's active cost codes."""
        model = CostCode._meta.get_field(field).related_model
        rows = (
            model.objects.filter(condition)
            # `values_list` before `distinct` keeps Meta.ordering columns out of the
            # SELECT list — otherwise they leak in and defeat DISTINCT entirely.
            .values_list(id_attr, name_attr)
            .distinct()
            .order_by(name_attr)
        )
        return [{"id": row[0], "name": row[1]} for row in rows]

    def _status_facet(self, estate) -> list[str]:
        rows = (
            with_current_status(CostCode.objects.filter(estate=estate))
            .order_by()
            .values_list(CURRENT_STATUS, flat=True)
            .distinct()
        )
        return sorted(set(rows))


class MasterDataView(APIView):
    """Every option list the cost code edit form needs, in one round trip.

    Distinct from the estate-scoped facets above: those list values *present* in
    an estate so a filter never returns nothing; this lists every active value so
    an editor can assign one the estate has not used yet. Child lists carry their
    parent id so the form can narrow subprocesses to the chosen process, and
    centres to the chosen location, before the server rejects the combination.

    Not estate-scoped, and deliberately so — master data has no estate
    dimension. It is read-only and holds nothing sensitive.
    """

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(summary="Option lists for the cost code editor")
    def get(self, request, *args, **kwargs):
        from apps.organization.models import (
            BuLead,
            Center,
            Lob,
            Location,
            Process,
            Region,
            Subprocess,
        )

        def rows(queryset, id_attr, name_attr, **extra):
            fields = [id_attr, name_attr, *extra.values()]
            return [
                {"id": row[0], "name": row[1], **dict(zip(extra.keys(), row[2:], strict=True))}
                for row in queryset.values_list(*fields).order_by(name_attr)
            ]

        return Response(
            {
                "process": rows(Process.objects.all(), "process_id", "process_name"),
                "subprocess": rows(
                    Subprocess.objects.all(),
                    "subprocess_id",
                    "subprocess_name",
                    process_id="process_id",
                ),
                "region": rows(Region.objects.all(), "region_id", "region_name"),
                "location": rows(
                    Location.objects.all(),
                    "location_id",
                    "location_name",
                    region_id="region_id",
                ),
                "center": rows(
                    Center.objects.all(),
                    "center_id",
                    "center_name",
                    location_id="location_id",
                ),
                "bu_lead": rows(BuLead.objects.all(), "bu_lead_id", "lead_name"),
                "lob": rows(Lob.objects.all(), "lob_id", "lob_name"),
            }
        )
