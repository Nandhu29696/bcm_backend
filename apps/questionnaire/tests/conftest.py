"""Fixtures for the plan editor: the real seeded question bank plus an editable plan."""

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from apps.accounts.models import Employee, UserEstateScope
from apps.organization.models import CostCode, Estate, Process
from apps.plans.models import CoordinatorAssignment, Plan, PlanStatus, PlanVersion
from apps.questionnaire.models import Question


@pytest.fixture(scope="session")
def _seeded_questionnaire(django_db_setup, django_db_blocker):
    """Seed the real 6-section / 15-question bank once for the whole session.

    The editor is tested against the genuine questionnaire, branches and lookup
    sources included, rather than a toy one — the branching is the whole point.
    """
    with django_db_blocker.unblock():
        call_command("seed_reference_data", verbosity=0)
        call_command("seed_questionnaire", verbosity=0)


@pytest.fixture(autouse=True)
def _questionnaire_present(db, _seeded_questionnaire):
    """Self-healing, for every test in this package.

    A transactional test (test_zz_concurrency.py) flushes the database at
    teardown, taking the session seed with it; with --reuse-db that empty state
    even survives into the next run. Re-seed inside this test's transaction
    whenever the bank is missing, so no test depends on which ran before it.
    """
    if not Question.objects.exists():
        call_command("seed_reference_data", verbosity=0)
        call_command("seed_questionnaire", verbosity=0)


@pytest.fixture
def questions(_questionnaire_present):
    return {q.question_code: q for q in Question.objects.all()}


@pytest.fixture
def estate(db):
    return Estate.objects.create(estate_name="Alpha Estate")


@pytest.fixture
def employee(db):
    return Employee.objects.create(
        employee_number="1100002", full_name="Arun Coordinator", email="arun@example.com"
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
    """A Work in Progress version the author is assigned to."""
    process = Process.objects.create(process_name="Customer Support")
    cost_code = CostCode.objects.create(cost_code="CC-1001", estate=estate, process=process)
    plan = Plan.objects.create(cost_code=cost_code, process=process)
    version = PlanVersion.objects.create(
        plan=plan, version_number=1, status=PlanStatus.WORK_IN_PROGRESS, created_by=author
    )
    CoordinatorAssignment.objects.create(
        plan_version=version, employee=employee, coordinator_type="Primary", estate=estate
    )
    return version


@pytest.fixture
def author_client(author):
    api_client = APIClient()
    api_client.force_authenticate(user=author)
    return api_client


@pytest.fixture
def onlooker_client(user_factory, estate):
    """In estate scope with an authoring role, but NOT assigned to the version."""
    # Viewer as well: a bare coordinator with no assignment no longer sees the
    # plan at all (the own-record cut), and 404 would not prove the write gate.
    user = user_factory(
        email="other.coordinator@example.com", roles=["BCM_COORDINATOR", "BCM_VIEWER"]
    )
    UserEstateScope.objects.create(user=user, estate=estate)
    api_client = APIClient()
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def viewer_client(user_factory, estate):
    user = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=estate)
    api_client = APIClient()
    api_client.force_authenticate(user=user)
    return api_client
