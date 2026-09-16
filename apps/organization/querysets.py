"""
Current BCP status resolution (Phase 2.2).

A cost code has no status column. Its status is the status of the newest
`plan_versions` row belonging to its plan — which means resolving it is a
correlated lookup, not a field read.

The obvious implementation is a Python loop over the page, asking each cost code
for `plan.current_version.status`. That is wrong twice over: it is N+1, and the
result is invisible to the database, so the column cannot be filtered or sorted —
and filtering by BCP status is one of the six required filters on the cost code
list. Both problems disappear if the resolution is an annotation.

The subqueries below are correlated, so MySQL evaluates one per row of the page.
That is only acceptable because `idx_pv_plan_version` covers
`(plan_id, version_number DESC)` exactly, making each evaluation an index seek to
the first matching row. Dropping that index turns this endpoint into a table scan
per row; see `apps.plans.models.PlanVersion.Meta.indexes`.
"""

from __future__ import annotations

from django.db.models import CharField, Count, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce

from apps.plans.models import PlanStatus, PlanVersion

#: Name of the annotation carrying the resolved status. Filters, ordering and
#: serializers all reference this, so it is a constant rather than a literal.
CURRENT_STATUS = "current_bcp_status"


def _latest_version_value(field: str, *, outer_ref: str = "pk") -> Subquery:
    """The value of `field` on the newest plan version of the referenced cost code.

    Ordered by `-version_number` first, then `-plan_version_id` as a tiebreak.
    The tiebreak matters: `plan_versions` is unique on (plan, version_number) but
    a cost code may own more than one plan (`plans` is unique on
    (process, cost_code)), so two rows can share a version number. Without a
    deterministic second key MySQL is free to return either, and the status
    column would flicker between page loads.
    """
    return Subquery(
        PlanVersion.objects.filter(
            plan__cost_code_id=OuterRef(outer_ref),
            # Related-object filtering uses the base manager, which does not hide
            # soft-deleted rows. A deleted plan must not supply a status.
            plan__active_flag=True,
        )
        .order_by("-version_number", "-plan_version_id")
        .values(field)[:1]
    )


def with_current_status(queryset: QuerySet) -> QuerySet:
    """Annotate a `CostCode` queryset with its resolved plan status.

    `current_bcp_status` falls back to "Not Started" rather than NULL. A cost code
    with no plan has genuinely not started, and a real value keeps the status
    filter honest — with NULL, filtering by "Not Started" would silently miss
    every cost code that most needs attention.
    """
    return queryset.annotate(
        **{
            CURRENT_STATUS: Coalesce(
                _latest_version_value("status"),
                Value(PlanStatus.NOT_STARTED),
                output_field=CharField(),
            )
        },
        current_plan_version_id=_latest_version_value("plan_version_id"),
        current_version_number=_latest_version_value("version_number"),
    )


def cost_code_list_queryset(queryset: QuerySet) -> QuerySet:
    """The full list queryset: status annotations plus the joins the serializer reads.

    `select_related` here is what keeps the list endpoint at a constant query
    count. Every FK the list serializer touches must appear in it.
    """
    return with_current_status(
        queryset.select_related(
            "process", "subprocess", "region", "bu_lead", "lob", "center", "location", "estate"
        )
    )


def status_rollup_by_estate(estate_ids) -> dict[int, dict[str, int]]:
    """Count cost codes per (estate, resolved status), in one query.

    Returned as `{estate_id: {status: count}}`.

    This is deliberately a second query rather than a conditional aggregate on the
    estate list itself. Counting by a status that is only known through a
    correlated subquery would mean repeating that subquery inside a `Count(...,
    filter=...)` once per status, against a join that already multiplies estate
    rows by their cost codes. One grouped pass over the cost codes is both simpler
    and cheaper, and — the property that actually matters — its cost is fixed. Two
    queries for ten estates, two queries for a thousand.
    """
    from apps.organization.models import CostCode

    rows = (
        with_current_status(CostCode.objects.filter(estate_id__in=list(estate_ids)))
        # CostCode.Meta.ordering would otherwise leak `cost_code` into the GROUP BY
        # and defeat the aggregation entirely.
        .order_by()
        .values("estate_id", CURRENT_STATUS)
        .annotate(total=Count("cost_code_id"))
    )

    # Every estate reports every status, zeros included. A status that is simply
    # absent from the result would render as a missing column rather than a zero,
    # so the set of columns would change from estate to estate.
    rollup: dict[int, dict[str, int]] = {}
    for row in rows:
        rollup.setdefault(row["estate_id"], empty_rollup())[row[CURRENT_STATUS]] = row["total"]
    return rollup


def empty_rollup() -> dict[str, int]:
    """Every status at zero — so the UI can render a stable set of columns."""
    return {status.value: 0 for status in PlanStatus}


__all__ = [
    "CURRENT_STATUS",
    "cost_code_list_queryset",
    "empty_rollup",
    "status_rollup_by_estate",
    "with_current_status",
]
