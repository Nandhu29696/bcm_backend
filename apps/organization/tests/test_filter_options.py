"""
Filter facets (Phase 2.4).

The filter bar's dropdowns come from the values actually present in the estate,
not from the global master-data tables. That distinction is the point of the
endpoint: offering a user two hundred processes when their estate has four, and
letting them pick one that returns nothing, is a worse screen than offering four.
"""

import pytest
from django.urls import reverse

from apps.organization.models import CostCode, Process
from apps.plans.models import PlanStatus

pytestmark = pytest.mark.django_db


def url(estate):
    return reverse("organization:estate-cost-code-filters", args=[estate.estate_id])


def names(payload, facet):
    return [option["name"] for option in payload[facet]]


def test_requires_authentication(api_client, org):
    assert api_client.get(url(org["alpha"])).status_code == 401


def test_an_estate_outside_scope_is_404(alpha_client, org):
    assert alpha_client.get(url(org["beta"])).status_code == 404


def test_returns_every_facet(alpha_client, org):
    payload = alpha_client.get(url(org["alpha"])).data
    assert set(payload) == {"process", "subprocess", "region", "bu_lead", "bcp_status"}


def test_options_are_limited_to_values_present_in_the_estate(alpha_client, org):
    """A process that exists globally but not in this estate must not be offered."""
    Process.objects.create(process_name="Unused Elsewhere")
    payload = alpha_client.get(url(org["alpha"])).data
    assert names(payload, "process") == ["Customer Support", "Finance Operations"]


def test_options_are_deduplicated(alpha_client, org):
    """Two cost codes share a process; it must appear once.

    Regression guard: `Meta.ordering` columns leak into the SELECT list ahead of
    DISTINCT and silently defeat it, which is exactly how this went wrong in the
    reference-data seeder.
    """
    payload = alpha_client.get(url(org["alpha"])).data
    process_names = names(payload, "process")
    assert len(process_names) == len(set(process_names))


def test_options_are_sorted_by_name(alpha_client, org):
    payload = alpha_client.get(url(org["alpha"])).data
    for facet in ("process", "subprocess", "region", "bu_lead"):
        assert names(payload, facet) == sorted(names(payload, facet)), facet


def test_options_carry_the_id_the_filter_expects(alpha_client, org):
    payload = alpha_client.get(url(org["alpha"])).data
    option = next(o for o in payload["process"] if o["name"] == "Finance Operations")
    assert option["id"] == org["finance"].process_id


def test_soft_deleted_cost_codes_do_not_contribute_options(alpha_client, org):
    """Deleting the only cost code using a process retires that option."""
    org["cc_review"].soft_delete()
    org["cc_planless"].soft_delete()
    payload = alpha_client.get(url(org["alpha"])).data
    assert names(payload, "process") == ["Customer Support"]


def test_status_facet_lists_statuses_actually_present(alpha_client, org):
    payload = alpha_client.get(url(org["alpha"])).data
    assert payload["bcp_status"] == sorted(
        [
            PlanStatus.APPROVED,
            PlanStatus.NOT_STARTED,
            PlanStatus.PENDING_BU_LEAD_REVIEW,
            PlanStatus.WORK_IN_PROGRESS,
        ]
    )


def test_facets_do_not_leak_another_estate(admin_client, org):
    payload = admin_client.get(url(org["beta"])).data
    assert names(payload, "bu_lead") == ["Priya Lead"]
    assert payload["bcp_status"] == [PlanStatus.APPROVED]


def test_cost_is_fixed_at_one_query_per_facet(alpha_client, org, django_assert_max_num_queries):
    """Five facets, plus auth and scope resolution — not one query per option.

    Eight on a cold request: the caller's role codes, their estate scopes, the
    estate itself, then exactly one per facet. Adding fifteen cost codes changes
    none of them.
    """
    for index in range(15):
        CostCode.objects.create(
            cost_code=f"CC-F-{index:03d}",
            estate=org["alpha"],
            process=org["support"],
            subprocess=org["tier1"],
            region=org["north"],
            bu_lead=org["priya"],
        )

    with django_assert_max_num_queries(9):
        assert alpha_client.get(url(org["alpha"])).status_code == 200
