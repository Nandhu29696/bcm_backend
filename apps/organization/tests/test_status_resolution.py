"""
Current BCP status resolution (Phase 2.2).

The status shown against a cost code is the status of its newest plan version.
Getting this wrong is not a cosmetic bug — the BCP status filter, the estate
rollup and (from Phase 6) the review queue all read the same annotation, so a
stale or missing resolution misreports compliance across the whole product.
"""

import pytest

from apps.organization.models import CostCode
from apps.organization.querysets import (
    CURRENT_STATUS,
    empty_rollup,
    status_rollup_by_estate,
    with_current_status,
)
from apps.plans.models import Plan, PlanStatus, PlanVersion

pytestmark = pytest.mark.django_db


def status_of(cost_code) -> str:
    row = with_current_status(CostCode.objects.filter(pk=cost_code.pk)).get()
    return getattr(row, CURRENT_STATUS)


def test_status_comes_from_the_newest_version(org):
    """Three versions exist; only v3's status may surface."""
    assert status_of(org["cc_approved"]) == PlanStatus.APPROVED


def test_a_cost_code_with_no_plan_is_not_started(org):
    """Must be a real value, not NULL — otherwise the status filter misses it."""
    assert status_of(org["cc_planless"]) == PlanStatus.NOT_STARTED


def test_not_started_is_filterable(org):
    """The NULL-vs-default distinction, stated as the behaviour that depends on it."""
    matches = with_current_status(CostCode.objects.all()).filter(
        **{CURRENT_STATUS: PlanStatus.NOT_STARTED}
    )
    assert org["cc_planless"].pk in {row.pk for row in matches}


def test_adding_a_newer_version_changes_the_resolved_status(org):
    cost_code = org["cc_wip"]
    assert status_of(cost_code) == PlanStatus.WORK_IN_PROGRESS

    plan = Plan.objects.get(cost_code=cost_code)
    PlanVersion.objects.create(
        plan=plan, version_number=2, status=PlanStatus.PENDING_BU_LEAD_REVIEW
    )

    assert status_of(cost_code) == PlanStatus.PENDING_BU_LEAD_REVIEW


def test_version_number_not_insertion_order_decides(org):
    """A back-filled older version must not win just by being inserted last."""
    cost_code = org["cc_wip"]
    plan = Plan.objects.get(cost_code=cost_code)
    PlanVersion.objects.create(plan=plan, version_number=0, status=PlanStatus.EXEMPTED)
    assert status_of(cost_code) == PlanStatus.WORK_IN_PROGRESS


def test_a_soft_deleted_plan_does_not_supply_a_status(org):
    """Related-object filtering uses the base manager, which ignores active_flag.

    Without the explicit `plan__active_flag=True` in the subquery, a deleted plan
    would keep reporting its status forever.
    """
    cost_code = org["cc_approved"]
    Plan.objects.get(cost_code=cost_code).soft_delete()
    assert status_of(cost_code) == PlanStatus.NOT_STARTED


def test_resolution_is_one_query_for_any_number_of_rows(org, django_assert_num_queries):
    """The property that makes this an annotation rather than a Python loop."""
    with django_assert_num_queries(1):
        list(with_current_status(CostCode.objects.all()).values("pk", CURRENT_STATUS))


def test_ordering_by_status_works_in_the_database(org):
    ordered = list(
        with_current_status(CostCode.objects.filter(estate=org["alpha"]))
        .order_by(CURRENT_STATUS, "cost_code")
        .values_list(CURRENT_STATUS, flat=True)
    )
    assert ordered == sorted(ordered)


# --------------------------------------------------------------------------- #
# Estate rollup
# --------------------------------------------------------------------------- #


def test_rollup_counts_by_resolved_status(org):
    rollup = status_rollup_by_estate([org["alpha"].estate_id])
    assert rollup[org["alpha"].estate_id] == {
        PlanStatus.NOT_STARTED: 1,
        PlanStatus.WORK_IN_PROGRESS: 1,
        PlanStatus.PENDING_BU_LEAD_REVIEW: 1,
        PlanStatus.APPROVED: 1,
        PlanStatus.REWORK: 0,
        PlanStatus.EXEMPTED: 0,
    }


def test_rollup_is_one_query_regardless_of_estate_count(org, django_assert_num_queries):
    """Fixed cost is the whole reason the rollup is a second query, not a join."""
    with django_assert_num_queries(1):
        status_rollup_by_estate([org["alpha"].estate_id, org["beta"].estate_id])


def test_rollup_separates_estates(org):
    rollup = status_rollup_by_estate([org["alpha"].estate_id, org["beta"].estate_id])
    beta = rollup[org["beta"].estate_id]
    assert beta[PlanStatus.APPROVED] == 1
    assert sum(beta.values()) == 1


def test_empty_rollup_lists_every_status_at_zero():
    """The UI renders a fixed set of columns; a missing key would blank one out."""
    rollup = empty_rollup()
    assert set(rollup) == set(PlanStatus.values)
    assert set(rollup.values()) == {0}
