"""
The estate-scoped cost code list (Phase 2.3) — journey step 3.

This is the busiest list in the application and the one a scoping mistake would
hurt most, so scope is asserted on its own before any filter behaviour.
"""

import pytest
from django.urls import reverse

from apps.organization.models import CostCode
from apps.organization.tests.conftest import rows
from apps.plans.models import PlanStatus

pytestmark = pytest.mark.django_db


def url(estate):
    return reverse("organization:estate-cost-codes", args=[estate.estate_id])


def codes(response):
    return {row["cost_code"] for row in rows(response)}


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


def test_requires_authentication(api_client, org):
    assert api_client.get(url(org["alpha"])).status_code == 401


def test_lists_the_estates_cost_codes(alpha_client, org):
    assert codes(alpha_client.get(url(org["alpha"]))) == {
        "CC-1001",
        "CC-1002",
        "CC-2001",
        "CC-2002",
    }


def test_an_estate_outside_scope_is_404_not_an_empty_list(alpha_client, org):
    """A 404 surfaces a missing grant; an empty list disguises it as no data."""
    assert alpha_client.get(url(org["beta"])).status_code == 404


def test_admin_reaches_any_estate(admin_client, org):
    assert codes(admin_client.get(url(org["beta"]))) == {"CC-9001"}


def test_soft_deleted_cost_codes_are_hidden(alpha_client, org):
    org["cc_wip"].soft_delete()
    assert "CC-1002" not in codes(alpha_client.get(url(org["alpha"])))


# --------------------------------------------------------------------------- #
# Row shape
# --------------------------------------------------------------------------- #


def test_row_carries_the_resolved_status_and_version_pointer(alpha_client, org):
    row = next(r for r in rows(alpha_client.get(url(org["alpha"]))) if r["cost_code"] == "CC-1001")
    assert row["bcp_status"] == PlanStatus.APPROVED
    assert row["current_version_number"] == 3
    assert row["current_plan_version_id"] is not None


def test_a_cost_code_with_no_plan_reports_not_started_and_no_version(alpha_client, org):
    row = next(r for r in rows(alpha_client.get(url(org["alpha"]))) if r["cost_code"] == "CC-2002")
    assert row["bcp_status"] == PlanStatus.NOT_STARTED
    assert row["current_plan_version_id"] is None


def test_foreign_keys_render_as_id_name_pairs(alpha_client, org):
    row = rows(alpha_client.get(url(org["alpha"])))[0]
    assert row["process"] == {
        "id": org["support"].process_id,
        "name": "Customer Support",
    }


def test_null_foreign_keys_render_as_null(alpha_client, org):
    CostCode.objects.create(cost_code="CC-3000", estate=org["alpha"])
    row = next(r for r in rows(alpha_client.get(url(org["alpha"]))) if r["cost_code"] == "CC-3000")
    assert row["process"] is None
    assert row["bu_lead"] is None


# --------------------------------------------------------------------------- #
# The six filters
# --------------------------------------------------------------------------- #


def test_filter_by_cost_code_is_a_contains_match(alpha_client, org):
    assert codes(alpha_client.get(url(org["alpha"]), {"cost_code": "20"})) == {"CC-2001", "CC-2002"}


def test_filter_by_cost_code_is_case_insensitive(alpha_client, org):
    CostCode.objects.create(cost_code="cc-lower", estate=org["alpha"])
    assert "cc-lower" in codes(alpha_client.get(url(org["alpha"]), {"cost_code": "CC-LOWER"}))


def test_filter_by_process(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"process": org["finance"].process_id})
    assert codes(response) == {"CC-2001", "CC-2002"}


def test_filter_by_subprocess(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"subprocess": org["tier1"].subprocess_id})
    assert codes(response) == {"CC-1001"}


def test_filter_by_region(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"region": org["north"].region_id})
    assert codes(response) == {"CC-1001", "CC-2001"}


def test_filter_by_bu_lead(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"bu_lead": org["ramesh"].bu_lead_id})
    assert codes(response) == {"CC-2001", "CC-2002"}


def test_filter_by_bcp_status(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"bcp_status": PlanStatus.APPROVED})
    assert codes(response) == {"CC-1001"}


def test_filter_by_bcp_status_not_started_finds_planless_cost_codes(alpha_client, org):
    """The annotation's Coalesce default, asserted through the API."""
    response = alpha_client.get(url(org["alpha"]), {"bcp_status": PlanStatus.NOT_STARTED})
    assert codes(response) == {"CC-2002"}


def test_repeated_values_of_one_filter_are_or(alpha_client, org):
    response = alpha_client.get(
        url(org["alpha"]),
        {"bcp_status": [PlanStatus.APPROVED, PlanStatus.NOT_STARTED]},
    )
    assert codes(response) == {"CC-1001", "CC-2002"}


def test_all_six_filters_combine_with_and(alpha_client, org):
    """The realistic case: a user narrowing with every control at once."""
    response = alpha_client.get(
        url(org["alpha"]),
        {
            "cost_code": "CC-",
            "process": org["support"].process_id,
            "subprocess": org["tier1"].subprocess_id,
            "region": org["north"].region_id,
            "bu_lead": org["priya"].bu_lead_id,
            "bcp_status": PlanStatus.APPROVED,
        },
    )
    assert codes(response) == {"CC-1001"}


def test_contradictory_filters_return_nothing_not_everything(alpha_client, org):
    """A filter silently ignored would show the full list instead of none."""
    response = alpha_client.get(
        url(org["alpha"]),
        {
            "process": org["finance"].process_id,
            "subprocess": org["tier1"].subprocess_id,
        },
    )
    assert codes(response) == set()


def test_an_invalid_status_is_rejected(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"bcp_status": "Nonsense"})
    assert response.status_code == 400


def test_filters_cannot_reach_outside_the_estate(alpha_client, org):
    """Filtering by a process that exists in Beta must not pull Beta's rows in."""
    response = alpha_client.get(url(org["alpha"]), {"process": org["support"].process_id})
    assert "CC-9001" not in codes(response)


def test_search_spans_code_process_and_bu_lead(alpha_client, org):
    assert codes(alpha_client.get(url(org["alpha"]), {"search": "Ramesh"})) == {
        "CC-2001",
        "CC-2002",
    }


# --------------------------------------------------------------------------- #
# Ordering and pagination
# --------------------------------------------------------------------------- #


def test_default_ordering_is_by_cost_code(alpha_client, org):
    result = [row["cost_code"] for row in rows(alpha_client.get(url(org["alpha"])))]
    assert result == sorted(result)


def test_ordering_by_status_is_done_in_the_database(alpha_client, org):
    result = [
        row["bcp_status"]
        for row in rows(alpha_client.get(url(org["alpha"]), {"ordering": "current_bcp_status"}))
    ]
    assert result == sorted(result)


def test_ordering_is_reversible(alpha_client, org):
    result = [
        row["cost_code"]
        for row in rows(alpha_client.get(url(org["alpha"]), {"ordering": "-cost_code"}))
    ]
    assert result == sorted(result, reverse=True)


def test_ordering_by_a_related_name(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"ordering": "bu_lead__lead_name"})
    assert [row["bu_lead"]["name"] for row in rows(response)][0] == "Priya Lead"


def test_pagination_reports_the_filtered_total(alpha_client, org):
    response = alpha_client.get(url(org["alpha"]), {"page_size": 2})
    assert response.data["count"] == 4
    assert len(response.data["results"]) == 2


def test_query_count_is_flat_across_page_size(alpha_client, org, django_assert_max_num_queries):
    """The N+1 guard on the busiest endpoint in the product (2.1, 2.3).

    Twenty more cost codes, each with a plan and versions, must not cost twenty
    more queries. Four are needed — the caller's estate scope, the estate lookup,
    the paginator count and the page itself; the status annotations ride along
    inside the page query as correlated subqueries rather than as queries of their
    own. The budget is one above that, so an N+1 fails rather than fitting.
    """
    for index in range(20):
        CostCode.objects.create(
            cost_code=f"CC-BULK-{index:03d}",
            estate=org["alpha"],
            process=org["support"],
            subprocess=org["tier1"],
            region=org["north"],
            bu_lead=org["priya"],
        )

    with django_assert_max_num_queries(5):
        response = alpha_client.get(url(org["alpha"]), {"page_size": 100})
    assert len(rows(response)) == 24
