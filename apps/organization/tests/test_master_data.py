"""The option lists behind the cost code editor (Phase 3.1)."""

import pytest
from django.urls import reverse

from apps.organization.models import Center, Location, Process, Subprocess

pytestmark = pytest.mark.django_db

URL = reverse("organization:master-data")


def test_requires_authentication(api_client, org):
    assert api_client.get(URL).status_code == 401


def test_returns_every_list(alpha_client, org):
    payload = alpha_client.get(URL).data
    assert set(payload) == {
        "process",
        "subprocess",
        "region",
        "location",
        "center",
        "bu_lead",
        "lob",
    }


def test_lists_are_global_not_estate_scoped(alpha_client, org):
    """The editor must be able to assign a process this estate has never used."""
    Process.objects.create(process_name="Unused Anywhere")
    names = [row["name"] for row in alpha_client.get(URL).data["process"]]
    assert "Unused Anywhere" in names


def test_children_carry_their_parent_id(alpha_client, org):
    """So the form can narrow subprocess by process before the server has to."""
    sub = next(row for row in alpha_client.get(URL).data["subprocess"] if row["name"] == "Tier 1")
    assert sub["process_id"] == org["support"].process_id

    location = Location.objects.create(location_name="Bangalore", region=org["north"])
    Center.objects.create(center_name="Tech Park", location=location)
    payload = alpha_client.get(URL).data
    assert payload["location"][0]["region_id"] == org["north"].region_id
    assert payload["center"][0]["location_id"] == location.location_id


def test_soft_deleted_values_are_omitted(alpha_client, org):
    Subprocess.objects.get(subprocess_name="Tier 2").soft_delete()
    names = [row["name"] for row in alpha_client.get(URL).data["subprocess"]]
    assert "Tier 2" not in names


def test_lists_are_sorted_by_name(alpha_client, org):
    payload = alpha_client.get(URL).data
    for key, rows in payload.items():
        names = [row["name"] for row in rows]
        assert names == sorted(names), key
