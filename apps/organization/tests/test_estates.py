"""
The estate list (Phase 2.1) — journey step 2, the screen users land on.

Two things matter here and they pull in opposite directions: the list must be
scoped to what the caller may see, and it must carry a per-estate cost code count
and BCP status rollup without its query count growing with the number of estates.
"""

import pytest
from django.urls import reverse

from apps.organization.models import CostCode, Estate
from apps.organization.tests.conftest import rows
from apps.plans.models import PlanStatus

pytestmark = pytest.mark.django_db

LIST_URL = reverse("organization:estate-list")


def test_requires_authentication(api_client):
    assert api_client.get(LIST_URL).status_code == 401


def test_lists_only_estates_in_scope(alpha_client, org):
    names = {row["estate_name"] for row in rows(alpha_client.get(LIST_URL))}
    assert names == {"Alpha Estate"}


def test_admin_sees_every_estate(admin_client, org):
    names = {row["estate_name"] for row in rows(admin_client.get(LIST_URL))}
    assert names == {"Alpha Estate", "Beta Estate"}


def test_user_with_no_scope_sees_an_empty_list_not_an_error(api_client, user_factory, org):
    """The landing state for an SSO sign-in with no estate grant yet."""
    api_client.force_authenticate(user=user_factory(email="new@example.com"))
    response = api_client.get(LIST_URL)
    assert response.status_code == 200
    assert rows(response) == []


def test_cost_code_count_excludes_soft_deleted(alpha_client, org):
    org["cc_wip"].soft_delete()
    row = rows(alpha_client.get(LIST_URL))[0]
    assert row["cost_code_count"] == 3


def test_status_rollup_counts_by_current_status(alpha_client, org):
    row = rows(alpha_client.get(LIST_URL))[0]
    assert row["status_rollup"][PlanStatus.APPROVED] == 1
    assert row["status_rollup"][PlanStatus.WORK_IN_PROGRESS] == 1
    assert row["status_rollup"][PlanStatus.PENDING_BU_LEAD_REVIEW] == 1
    assert row["status_rollup"][PlanStatus.NOT_STARTED] == 1


def test_rollup_reports_every_status_even_when_zero(alpha_client, org):
    """A stable key set, so the UI's status columns do not shift between estates."""
    row = rows(alpha_client.get(LIST_URL))[0]
    assert set(row["status_rollup"]) == set(PlanStatus.values)
    assert row["status_rollup"][PlanStatus.EXEMPTED] == 0


def test_rollup_does_not_leak_other_estates(admin_client, org):
    by_name = {row["estate_name"]: row for row in rows(admin_client.get(LIST_URL))}
    assert by_name["Beta Estate"]["status_rollup"][PlanStatus.APPROVED] == 1
    assert by_name["Beta Estate"]["cost_code_count"] == 1


def test_query_count_does_not_grow_with_the_number_of_estates(
    admin_client, org, django_assert_max_num_queries
):
    """The N+1 guard the plan asks for (2.1).

    Ten more estates, each with cost codes and plans, must not cost ten more
    queries. Three are needed — the paginator count, the estate page, the rollup —
    so the budget is set at one above the real figure rather than a loose ceiling:
    a regression to per-estate rollups would show up immediately.
    """
    for index in range(10):
        estate = Estate.objects.create(estate_name=f"Extra Estate {index}")
        CostCode.objects.create(cost_code=f"EX-{index}", estate=estate, process=org["support"])

    with django_assert_max_num_queries(4):
        response = admin_client.get(LIST_URL, {"page_size": 50})
    assert len(rows(response)) == 12


def test_ordering_by_cost_code_count(admin_client, org):
    result = rows(admin_client.get(LIST_URL, {"ordering": "-cost_code_count"}))
    assert result[0]["estate_name"] == "Alpha Estate"
    assert result[0]["cost_code_count"] == 4


def test_retrieve_returns_a_rollup(alpha_client, org):
    url = reverse("organization:estate-detail", args=[org["alpha"].estate_id])
    response = alpha_client.get(url)
    assert response.status_code == 200
    assert response.data["status_rollup"][PlanStatus.APPROVED] == 1


def test_retrieving_an_estate_outside_scope_is_404(alpha_client, org):
    url = reverse("organization:estate-detail", args=[org["beta"].estate_id])
    assert alpha_client.get(url).status_code == 404
