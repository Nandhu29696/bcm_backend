"""Service descriptions, critical contacts and network requirements (5.1-5.3)."""

import pytest
from django.urls import reverse

from apps.assessments.models import BiaCriticalContact, BiaServiceDescription
from apps.organization.models import Process, Subprocess
from apps.plans.models import PlanStatus

pytestmark = pytest.mark.django_db


def url(name, version, pk=None):
    args = [version.plan_version_id] + ([pk] if pk else [])
    return reverse(f"assessments:{name}-{'detail' if pk else 'list'}", args=args)


# --------------------------------------------------------------------------- #
# Service descriptions (5.1)
# --------------------------------------------------------------------------- #


def test_service_description_defaults_to_the_plans_process(author_client, version, owner):
    response = author_client.post(
        url("service-description", version),
        {
            "process_description": "Inbound customer contact.",
            "mao": " 24 ",
            "mbco": "60",
            "rto": "8",
            "rpo": "4",
            "owner_employee": owner.pk,
        },
    )
    assert response.status_code == 201, response.data
    assert response.data["process_name"] == "Customer Support"
    assert response.data["subprocess_name"] == "Tier 1"  # from the cost code
    assert response.data["mao"] == "24"
    assert response.data["owner_employee"]["name"] == "Sneha Risk Analyst"


def test_a_subprocess_must_belong_to_the_process(author_client, version):
    finance = Process.objects.create(process_name="Finance")
    payables = Subprocess.objects.create(subprocess_name="Payables", process=finance)
    response = author_client.post(
        url("service-description", version), {"subprocess": payables.pk, "rto": "8"}
    )
    assert response.status_code == 400
    assert "subprocess" in response.data["field_errors"]


def test_service_description_update_and_delete(author_client, version):
    created = author_client.post(url("service-description", version), {"rto": "8"})
    pk = created.data["service_description_id"]
    assert (
        author_client.patch(url("service-description", version, pk), {"rto": "12"}).data["rto"]
        == "12"
    )
    assert author_client.delete(url("service-description", version, pk)).status_code == 204
    assert not BiaServiceDescription.objects.filter(pk=pk).exists()


# --------------------------------------------------------------------------- #
# Critical contacts (5.2)
# --------------------------------------------------------------------------- #


def test_a_contact_needs_a_person_or_a_phone(author_client, version, owner):
    assert (
        author_client.post(
            url("critical-contact", version), {"contact_type": "Primary"}
        ).status_code
        == 400
    )
    ok = author_client.post(
        url("critical-contact", version),
        {
            "contact_type": "Primary",
            "primary_phone": "+91 99999 11111",
            "seat_count": 12,
            "voice_non_voice": "Voice",
        },
    )
    assert ok.status_code == 201, ok.data
    with_person = author_client.post(
        url("critical-contact", version), {"employee": owner.pk, "contact_type": "Backup"}
    )
    assert with_person.status_code == 201
    assert with_person.data["employee"]["name"] == "Sneha Risk Analyst"


def test_seat_count_cannot_be_negative(author_client, version):
    response = author_client.post(
        url("critical-contact", version), {"primary_phone": "1", "seat_count": -1}
    )
    assert response.status_code == 400
    assert "seat_count" in response.data["field_errors"]


def test_contacts_list_is_scoped_to_the_version(author_client, version, estate, employee):
    from apps.plans.models import CoordinatorAssignment, PlanVersion

    author_client.post(url("critical-contact", version), {"primary_phone": "1"})
    other = PlanVersion.objects.create(
        plan=version.plan, version_number=2, status=PlanStatus.WORK_IN_PROGRESS
    )
    CoordinatorAssignment.objects.create(plan_version=other, employee=employee, estate=estate)
    assert author_client.get(url("critical-contact", other)).data == []
    assert BiaCriticalContact.objects.count() == 1


# --------------------------------------------------------------------------- #
# Network requirements (5.3)
# --------------------------------------------------------------------------- #


def test_network_requirement_needs_an_endpoint(author_client, version):
    response = author_client.post(
        url("network-requirement", version), {"requirement_type": "BCP_PLAN"}
    )
    assert response.status_code == 400
    assert "destination_ip" in response.data["field_errors"]


def test_network_requirement_round_trip(author_client, version):
    response = author_client.post(
        url("network-requirement", version),
        {
            "requirement_type": "BCP_PLAN",
            "source_ip": "10.0.0.1",
            "destination_ip": "10.0.1.1",
            "port_number": "443",
            "connectivity_type": "MPLS",
        },
    )
    assert response.status_code == 201, response.data
    assert response.data["requirement_type"] == "BCP_PLAN"
    assert author_client.get(url("network-requirement", version)).data[0]["port_number"] == "443"


def test_requirement_type_is_validated(author_client, version):
    response = author_client.post(
        url("network-requirement", version), {"requirement_type": "OTHER", "source_ip": "10.0.0.1"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# The shared rules, once per section
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["service-description", "critical-contact", "network-requirement"])
def test_sections_are_readable_in_scope_and_404_outside(
    author_client, viewer_client, api_client, user_factory, version, name
):
    assert author_client.get(url(name, version)).status_code == 200
    assert viewer_client.get(url(name, version)).status_code == 200
    api_client.force_authenticate(
        user=user_factory(email="s@example.com", roles=["BCM_COORDINATOR"])
    )
    assert api_client.get(url(name, version)).status_code == 404


@pytest.mark.parametrize("name", ["service-description", "critical-contact", "network-requirement"])
def test_only_authors_write(viewer_client, onlooker_client, version, name):
    assert viewer_client.post(url(name, version), {}).status_code == 403
    assert onlooker_client.post(url(name, version), {}).status_code == 403


@pytest.mark.parametrize("name", ["service-description", "critical-contact", "network-requirement"])
def test_closed_versions_are_read_only(author_client, version, name):
    version.status = PlanStatus.APPROVED
    version.save(update_fields=["status"])
    response = author_client.post(
        url(name, version), {"primary_phone": "1", "source_ip": "1", "rto": "8"}
    )
    assert response.status_code == 409
    assert response.data["code"] == "plan_not_editable"
