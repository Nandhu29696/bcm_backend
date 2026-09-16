"""The employee directory behind the coordinator picker (Phase 3.2)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Employee

pytestmark = pytest.mark.django_db

LIST_URL = reverse("accounts:employee-list")


@pytest.fixture
def employees(db):
    return [
        Employee.objects.create(
            employee_number="1100002",
            full_name="Arun Coordinator",
            email="arun@example.com",
            designation="BCM Coordinator",
        ),
        Employee.objects.create(
            employee_number="1100003",
            full_name="Meera Analyst",
            email="meera@example.com",
            designation="Analyst",
        ),
    ]


def test_requires_authentication(api_client, employees):
    assert api_client.get(LIST_URL).status_code == 401


def test_lists_employees(authenticated_client, employees):
    client, _ = authenticated_client()
    response = client.get(LIST_URL)
    assert response.status_code == 200
    assert {row["full_name"] for row in response.data["results"]} == {
        "Arun Coordinator",
        "Meera Analyst",
    }


def test_search_by_name(authenticated_client, employees):
    client, _ = authenticated_client()
    response = client.get(LIST_URL, {"search": "meera"})
    assert [row["full_name"] for row in response.data["results"]] == ["Meera Analyst"]


def test_search_by_employee_number(authenticated_client, employees):
    client, _ = authenticated_client()
    response = client.get(LIST_URL, {"search": "1100002"})
    assert [row["full_name"] for row in response.data["results"]] == ["Arun Coordinator"]


def test_exposes_only_directory_fields(authenticated_client, employees):
    """HR data is not a picker's business — assert the shape, not just the names."""
    client, _ = authenticated_client()
    row = client.get(LIST_URL).data["results"][0]
    assert set(row) == {"employee_id", "employee_number", "full_name", "email", "designation"}


def test_is_read_only(authenticated_client, employees):
    client, _ = authenticated_client()
    response = client.post(LIST_URL, {"full_name": "Intruder", "employee_number": "9"})
    assert response.status_code == 405
