"""Fixtures for the estate and cost code endpoints."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import UserEstateScope
from apps.organization.models import BuLead, CostCode, Estate, Process, Region, Subprocess
from apps.plans.models import Plan, PlanStatus, PlanVersion


@pytest.fixture
def org(db):
    """A small but complete organisation across two estates.

    Alpha has four cost codes spanning two processes, two regions, two BU leads
    and four different BCP statuses. Beta exists solely so scoping has something
    to exclude.
    """
    alpha = Estate.objects.create(estate_name="Alpha Estate")
    beta = Estate.objects.create(estate_name="Beta Estate")

    north = Region.objects.create(region_name="North", geography="Americas")
    south = Region.objects.create(region_name="South", geography="Asia")

    support = Process.objects.create(process_name="Customer Support")
    finance = Process.objects.create(process_name="Finance Operations")

    tier1 = Subprocess.objects.create(subprocess_name="Tier 1", process=support)
    tier2 = Subprocess.objects.create(subprocess_name="Tier 2", process=support)
    payables = Subprocess.objects.create(subprocess_name="Accounts Payable", process=finance)

    priya = BuLead.objects.create(lead_name="Priya Lead", email="priya@example.com")
    ramesh = BuLead.objects.create(lead_name="Ramesh Lead", email="ramesh@example.com")

    def make(code, estate, process, subprocess, region, bu_lead, status=None, versions=1):
        cost_code = CostCode.objects.create(
            cost_code=code,
            estate=estate,
            process=process,
            subprocess=subprocess,
            region=region,
            bu_lead=bu_lead,
        )
        if status is not None:
            plan = Plan.objects.create(cost_code=cost_code, process=process)
            for number in range(1, versions + 1):
                PlanVersion.objects.create(
                    plan=plan,
                    version_number=number,
                    # Only the newest version should reach the list.
                    status=status if number == versions else PlanStatus.WORK_IN_PROGRESS,
                )
        return cost_code

    data = {
        "alpha": alpha,
        "beta": beta,
        "north": north,
        "south": south,
        "support": support,
        "finance": finance,
        "tier1": tier1,
        "tier2": tier2,
        "payables": payables,
        "priya": priya,
        "ramesh": ramesh,
    }

    data["cc_approved"] = make(
        "CC-1001", alpha, support, tier1, north, priya, PlanStatus.APPROVED, versions=3
    )
    data["cc_wip"] = make(
        "CC-1002", alpha, support, tier2, south, priya, PlanStatus.WORK_IN_PROGRESS
    )
    data["cc_review"] = make(
        "CC-2001",
        alpha,
        finance,
        payables,
        north,
        ramesh,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
    )
    # No plan at all -> must resolve to "Not Started", not NULL.
    data["cc_planless"] = make("CC-2002", alpha, finance, payables, south, ramesh)
    data["cc_beta"] = make("CC-9001", beta, support, tier1, north, priya, PlanStatus.APPROVED)
    return data


@pytest.fixture
def alpha_user(user_factory, org):
    """A viewer scoped to Alpha only — sees every cost code in that estate.

    (A coordinator would see only the cost codes they are assigned to; the
    estate-level filtering these tests are about is best shown on a viewer.)
    """
    user = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=org["alpha"])
    return user


@pytest.fixture
def alpha_client(alpha_user):
    api_client = APIClient()
    api_client.force_authenticate(user=alpha_user)
    return api_client


@pytest.fixture
def admin_client(user_factory):
    """BCM_ADMIN sees every estate without a scope row."""
    user = user_factory(email="admin@example.com", roles=["BCM_ADMIN"])
    api_client = APIClient()
    api_client.force_authenticate(user=user)
    return api_client


def rows(response):
    """Unwrap a paginated list response."""
    data = response.data
    return data["results"] if isinstance(data, dict) and "results" in data else data
