"""Fixtures for the structured sections: the real catalogue plus an editable version."""

import pytest
from django.core.management import call_command

from apps.accounts.models import Employee, UserEstateScope
from apps.lookups.models import LookupCategory
from apps.organization.models import CostCode, Estate, Process, Subprocess
from apps.plans.models import CoordinatorAssignment, Plan, PlanStatus, PlanVersion


@pytest.fixture(autouse=True)
def _catalogue(db):
    """The real lookup catalogue, with its weights. Re-seeded if a flush took it."""
    if not LookupCategory.objects.filter(category_type="Likelihood", points__isnull=False).exists():
        call_command("seed_reference_data", verbosity=0)


@pytest.fixture
def estate(db):
    return Estate.objects.create(estate_name="Alpha Estate")


@pytest.fixture
def employee(db):
    return Employee.objects.create(
        employee_number="1100002", full_name="Arun Coordinator", email="arun@example.com"
    )


@pytest.fixture
def owner(db):
    return Employee.objects.create(
        employee_number="1100005", full_name="Sneha Risk Analyst", email="sneha@example.com"
    )


@pytest.fixture
def author(user_factory, employee, estate):
    user = user_factory(email="arun@example.com", roles=["BCM_COORDINATOR"])
    user.employee = employee
    user.save(update_fields=["employee"])
    UserEstateScope.objects.create(user=user, estate=estate)
    return user


@pytest.fixture
def version(estate, employee, author):
    process = Process.objects.create(process_name="Customer Support")
    subprocess = Subprocess.objects.create(subprocess_name="Tier 1", process=process)
    cost_code = CostCode.objects.create(
        cost_code="CC-1001", estate=estate, process=process, subprocess=subprocess
    )
    plan = Plan.objects.create(cost_code=cost_code, process=process)
    version = PlanVersion.objects.create(
        plan=plan, version_number=1, status=PlanStatus.WORK_IN_PROGRESS, created_by=author
    )
    CoordinatorAssignment.objects.create(
        plan_version=version, employee=employee, coordinator_type="Primary", estate=estate
    )
    return version


@pytest.fixture
def author_client(api_client, author):
    api_client.force_authenticate(user=author)
    return api_client


# Separate client instances per persona. Sharing the `api_client` fixture would
# let whichever fixture authenticated last win — a "viewer" test whose setup
# also used `author_client` would silently run as the author.


@pytest.fixture
def viewer_client(user_factory, estate):
    from rest_framework.test import APIClient

    user = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=estate)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def onlooker_client(user_factory, estate):
    """Authoring role and estate scope, but not assigned to the version."""
    from rest_framework.test import APIClient

    # Viewer as well: a bare coordinator with no assignment no longer sees the
    # plan at all (the own-record cut), and 404 would not prove the write gate.
    user = user_factory(email="other@example.com", roles=["BCM_COORDINATOR", "BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=estate)
    client = APIClient()
    client.force_authenticate(user=user)
    return client
