"""
Cost code filtering (Phase 2.3) — journey step 3's six filters.

    cost_code    free text, contains
    process      one or more ids
    subprocess   one or more ids
    region       one or more ids
    bcp_status    one or more of the six statuses
    bu_lead      one or more ids

All six combine with AND; repeated values of one filter combine with OR. So
`?process=1&process=2&bcp_status=Approved` reads "in process 1 or 2, and
approved", which is what the filter bar's multi-select chips imply.

`bcp_status` filters the `current_bcp_status` annotation rather than a column.
That works because DRF's filter backends run on the result of `get_queryset()`,
which has already applied `cost_code_list_queryset`. It is the whole reason the
status is resolved as an annotation instead of in Python.
"""

from __future__ import annotations

import django_filters
from django.db.models import Q

from apps.organization.models import BuLead, CostCode, Process, Region, Subprocess
from apps.organization.querysets import CURRENT_STATUS
from apps.plans.models import PlanStatus


class CostCodeFilterSet(django_filters.FilterSet):
    """The estate-scoped cost code list filter."""

    cost_code = django_filters.CharFilter(
        field_name="cost_code",
        lookup_expr="icontains",
        label="Cost code contains",
    )
    process = django_filters.ModelMultipleChoiceFilter(
        field_name="process",
        queryset=Process.objects.all(),
        label="Process",
    )
    subprocess = django_filters.ModelMultipleChoiceFilter(
        field_name="subprocess",
        queryset=Subprocess.objects.all(),
        label="Subprocess",
    )
    region = django_filters.ModelMultipleChoiceFilter(
        field_name="region",
        queryset=Region.objects.all(),
        label="Region",
    )
    bu_lead = django_filters.ModelMultipleChoiceFilter(
        field_name="bu_lead",
        queryset=BuLead.objects.all(),
        label="BU lead",
    )
    bcp_status = django_filters.MultipleChoiceFilter(
        field_name=CURRENT_STATUS,
        choices=PlanStatus.choices,
        label="BCP status",
    )
    #: Free-text across the descriptive columns, for the single search box above
    #: the table. Distinct from `cost_code`, which targets the code itself.
    search = django_filters.CharFilter(method="filter_search", label="Search")

    class Meta:
        model = CostCode
        fields = ["cost_code", "process", "subprocess", "region", "bu_lead", "bcp_status"]

    def filter_search(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(cost_code__icontains=value)
            | Q(process__process_name__icontains=value)
            | Q(subprocess__subprocess_name__icontains=value)
            | Q(bu_lead__lead_name__icontains=value)
        )
