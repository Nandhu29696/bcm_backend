"""
The own-record cut (AD-3, second stage).

Inside their estates:
  * an administrator (or auditor) sees everything;
  * a viewer or reviewer sees every cost code in the estate;
  * a coordinator sees only the cost codes they are assigned to;
  * a BU lead or approver sees only the cost codes they lead;
  * holding a viewer role alongside widens back to the estate.

Proven at the real endpoints — the cost code list and the estate card count
(journey steps 2-3), the version list and the plan editor (steps 4-5) — because
that is where a coordinator would otherwise open a colleague's plan.
"""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Employee, UserEstateScope
from apps.organization.models import BuLead, CostCode, Estate, Process
from apps.plans.models import CoordinatorAssignment, Plan, PlanStatus, PlanVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    """One estate, three cost codes: one assigned to Arun, one led by Priya, one neither."""
    estate = Estate.objects.create(estate_name="Alpha Estate")
    process = Process.objects.create(process_name="Support")
    priya = BuLead.objects.create(lead_name="Priya Lead", email="priya@example.com")
    other = BuLead.objects.create(lead_name="Someone Else", email="else@example.com")
    codes = {
        "mine": CostCode.objects.create(
            cost_code="CC-MINE", estate=estate, process=process, bu_lead=other
        ),
        "led": CostCode.objects.create(
            cost_code="CC-LED", estate=estate, process=process, bu_lead=priya
        ),
        "other": CostCode.objects.create(
            cost_code="CC-OTHER", estate=estate, process=process, bu_lead=other
        ),
    }
    versions = {}
    for key, code in codes.items():
        plan = Plan.objects.create(cost_code=code, process=process)
        versions[key] = PlanVersion.objects.create(
            plan=plan, version_number=1, status=PlanStatus.WORK_IN_PROGRESS
        )
    # Arun was assigned to v1 of CC-MINE; v2 exists too, unassigned — he must
    # still see it (the claim is on the cost code, not the single version).
    versions["mine_v2"] = PlanVersion.objects.create(
        plan=versions["mine"].plan, version_number=2, status=PlanStatus.NOT_STARTED
    )
    versions["mine"].status = PlanStatus.APPROVED
    versions["mine"].save(update_fields=["status"])
    arun = Employee.objects.create(
        employee_number="1100002", full_name="Arun", email="arun@example.com"
    )
    CoordinatorAssignment.objects.create(
        plan_version=versions["mine"], employee=arun, coordinator_type="Primary", estate=estate
    )
    return {"estate": estate, "codes": codes, "versions": versions, "arun": arun}


def client_for(user_factory, world, *, roles, email, employee=None):
    user = user_factory(email=email, roles=roles)
    if employee is not None:
        user.employee = employee
        user.save(update_fields=["employee"])
    UserEstateScope.objects.create(user=user, estate=world["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def listed_codes(client, world):
    url = reverse("organization:estate-cost-codes", args=[world["estate"].pk])
    response = client.get(url)
    assert response.status_code == 200
    return {row["cost_code"] for row in response.data["results"]}


def estate_count(client, world):
    response = client.get(reverse("organization:estate-list"))
    return next(
        e["cost_code_count"]
        for e in response.data["results"]
        if e["estate_id"] == world["estate"].pk
    )


def can_open_plan(client, version) -> bool:
    url = reverse("questionnaire:questionnaire", args=[version.plan_version_id])
    return client.get(url).status_code == 200


def can_list_versions(client, cost_code) -> bool:
    url = reverse("plans:cost-code-plan-versions", args=[cost_code.pk])
    return client.get(url).status_code == 200


# --------------------------------------------------------------------------- #


def test_a_coordinator_sees_only_the_cost_codes_they_are_assigned_to(user_factory, world):
    client = client_for(
        user_factory,
        world,
        roles=["BCM_COORDINATOR"],
        email="arun@example.com",
        employee=world["arun"],
    )

    assert listed_codes(client, world) == {"CC-MINE"}
    assert estate_count(client, world) == 1
    assert can_list_versions(client, world["codes"]["mine"])
    assert not can_list_versions(client, world["codes"]["other"])
    assert can_open_plan(client, world["versions"]["mine"])
    assert can_open_plan(client, world["versions"]["mine_v2"])  # the cost code's other version
    assert not can_open_plan(client, world["versions"]["other"])
    assert not can_open_plan(client, world["versions"]["led"])


def test_a_coordinator_without_an_hr_record_sees_nothing(user_factory, world):
    client = client_for(user_factory, world, roles=["BCM_COORDINATOR"], email="new.sso@example.com")
    assert listed_codes(client, world) == set()
    assert estate_count(client, world) == 0


def test_a_bu_lead_sees_only_the_cost_codes_they_lead(user_factory, world):
    client = client_for(user_factory, world, roles=["BCM_BU_LEAD"], email="priya@example.com")

    assert listed_codes(client, world) == {"CC-LED"}
    assert can_open_plan(client, world["versions"]["led"])
    assert not can_open_plan(client, world["versions"]["mine"])


def test_the_bu_lead_match_is_by_email_case_insensitively(user_factory, world):
    client = client_for(user_factory, world, roles=["BCM_APPROVER"], email="Priya@Example.com")
    assert listed_codes(client, world) == {"CC-LED"}


def test_a_viewer_sees_the_whole_estate(user_factory, world):
    client = client_for(user_factory, world, roles=["BCM_VIEWER"], email="viewer@example.com")
    assert listed_codes(client, world) == {"CC-MINE", "CC-LED", "CC-OTHER"}
    assert estate_count(client, world) == 3
    assert can_open_plan(client, world["versions"]["other"])


def test_a_coordinator_who_is_also_a_viewer_sees_the_whole_estate(user_factory, world):
    client = client_for(
        user_factory,
        world,
        roles=["BCM_COORDINATOR", "BCM_VIEWER"],
        email="arun@example.com",
        employee=world["arun"],
    )
    assert listed_codes(client, world) == {"CC-MINE", "CC-LED", "CC-OTHER"}


def test_a_coordinator_who_is_also_a_bu_lead_sees_both_claims(user_factory, world):
    client = client_for(
        user_factory,
        world,
        roles=["BCM_COORDINATOR", "BCM_BU_LEAD"],
        email="priya@example.com",
        employee=world["arun"],
    )
    assert listed_codes(client, world) == {"CC-MINE", "CC-LED"}


def test_an_administrator_sees_everything(user_factory, world):
    user = user_factory(email="admin@example.com", roles=["BCM_ADMIN"])
    client = APIClient()
    client.force_authenticate(user=user)
    assert listed_codes(client, world) == {"CC-MINE", "CC-LED", "CC-OTHER"}
    assert can_open_plan(client, world["versions"]["other"])


def test_estate_scope_still_bounds_the_claim(user_factory, world):
    """A claim on a cost code in an estate the user was not granted counts for nothing."""
    user = user_factory(email="arun@example.com", roles=["BCM_COORDINATOR"])
    user.employee = world["arun"]
    user.save(update_fields=["employee"])
    UserEstateScope.objects.create(
        user=user, estate=Estate.objects.create(estate_name="Beta Estate")
    )
    client = APIClient()
    client.force_authenticate(user=user)
    assert not can_open_plan(client, world["versions"]["mine"])
