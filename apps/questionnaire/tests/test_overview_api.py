"""
The plan overview and the network-diagram attachments.

    GET  /plan-versions/{id}/overview/          objectives, stage statuses, people
    GET/POST/DELETE /plan-versions/{id}/attachments/

Lives here rather than in `plans` because the overview reads the seeded
question bank (RTO-001, MBCO-001, ...), which this package's fixtures provide.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Employee
from apps.assessments.models import BiaCriticalContact
from apps.documents.models import EntityDocument
from apps.organization.models import BuLead
from apps.plans.models import PlanStatus
from apps.questionnaire.answers import lookup_options
from apps.risk.models import RecoveryStrategy, Risk

pytestmark = pytest.mark.django_db


def overview_url(version):
    return reverse("plans:plan-version-overview", args=[version.plan_version_id])


def attachments_url(version):
    return reverse("documents:version-attachments", args=[version.plan_version_id])


def attachment_url(version, entity_document_id):
    return reverse(
        "documents:version-attachment-detail", args=[version.plan_version_id, entity_document_id]
    )


def put_answer(client, version, question, answer):
    url = reverse("questionnaire:answer", args=[version.plan_version_id, question.pk])
    return client.put(url, {"answer": answer}, format="json")


def png(name="diagram.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png")


# --------------------------------------------------------------------------- #
# Objectives
# --------------------------------------------------------------------------- #


def test_overview_requires_scope(api_client, user_factory, version):
    user = user_factory(email="nobody@example.com", roles=["BCM_VIEWER"])
    api_client.force_authenticate(user=user)
    assert api_client.get(overview_url(version)).status_code == 404


def test_objectives_are_empty_until_answered(author_client, version):
    data = author_client.get(overview_url(version)).data
    assert data["objectives"] == {
        "rto_hours": None,
        "mbco_percent": None,
        "rpo_in_contract": None,
        "rpo_hours": None,
        "mao_hours": None,
    }
    assert data["stages"]["bia"]["status"] == "Not Started"
    assert data["stages"]["ra"]["status"] == "Not Started"
    assert data["stages"]["plan"]["status"] == "Not Started"


def test_objectives_read_the_questionnaire_answers(author_client, version, questions):
    put_answer(author_client, version, questions["RTO-001"], {"value": 8})
    put_answer(author_client, version, questions["MBCO-001"], {"value": 60})
    put_answer(author_client, version, questions["MAO-001"], {"value": 24})
    put_answer(author_client, version, questions["RPO-001"], {"value": "YES"})
    put_answer(author_client, version, questions["RPO-002"], {"value": 4})

    objectives = author_client.get(overview_url(version)).data["objectives"]
    assert objectives == {
        "rto_hours": 8,
        "mbco_percent": 60,
        "rpo_in_contract": "YES",
        "rpo_hours": 4,
        "mao_hours": 24,
    }


def test_rpo_hours_hidden_behind_a_no(author_client, version, questions):
    """A number typed before the answer flipped to No must not surface."""
    put_answer(author_client, version, questions["RPO-001"], {"value": "YES"})
    put_answer(author_client, version, questions["RPO-002"], {"value": 4})
    put_answer(author_client, version, questions["RPO-001"], {"value": "NO"})

    objectives = author_client.get(overview_url(version)).data["objectives"]
    assert objectives["rpo_in_contract"] == "NO"
    assert objectives["rpo_hours"] is None


# --------------------------------------------------------------------------- #
# Stage statuses
# --------------------------------------------------------------------------- #


def test_bia_stage_follows_the_bia_questions(author_client, version, questions):
    put_answer(author_client, version, questions["BIA-002"], {"value": "YES"})
    stage = author_client.get(overview_url(version)).data["stages"]["bia"]
    assert stage["status"] == "In Progress"
    assert stage["required_answered"] == 1
    assert stage["required_visible"] == 5

    # Every required BIA question answered; the "No"s hide the sub-form.
    site = lookup_options("Primary sites")[0].code
    put_answer(author_client, version, questions["BIA-001"], {"value": site})
    put_answer(author_client, version, questions["BIA-003"], {"value": "NO"})
    put_answer(author_client, version, questions["BIA-005"], {"value": "NO"})
    put_answer(author_client, version, questions["BIA-006"], {"value": "NO"})
    stage = author_client.get(overview_url(version)).data["stages"]["bia"]
    assert stage["status"] == "Completed"


def test_bia_stage_in_progress_from_a_list_alone(author_client, version):
    BiaCriticalContact.objects.create(plan_version=version, contact_type="Primary")
    stage = author_client.get(overview_url(version)).data["stages"]["bia"]
    assert stage["status"] == "In Progress"
    assert stage["critical_resources"] == 1


def test_ra_stage_needs_every_risk_rated(author_client, version):
    Risk.objects.create(plan_version=version, risk_name="Power outage")
    stage = author_client.get(overview_url(version)).data["stages"]["ra"]
    assert stage == {"status": "In Progress", "risks": 1, "unrated": 1}

    Risk.objects.filter(plan_version=version).update(risk_level="High")
    stage = author_client.get(overview_url(version)).data["stages"]["ra"]
    assert stage["status"] == "Completed"


def test_plan_stage_is_the_strategy(author_client, version):
    RecoveryStrategy.objects.create(plan_version=version, core_strategy="Split site")
    stage = author_client.get(overview_url(version)).data["stages"]["plan"]
    assert stage["status"] == "Completed"
    assert stage["strategies"] == 1


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #


def test_people_lists_the_cost_code_and_the_mbco_headcount(author_client, version, questions):
    cost_code = version.plan.cost_code
    lead = BuLead.objects.create(lead_name="Priya Lead", email="priya@example.com")
    addon = BuLead.objects.create(lead_name="Ravi Shared", email="ravi@example.com")
    cost_code.bu_lead = lead
    cost_code.save(update_fields=["bu_lead"])
    for n in range(7):
        Employee.objects.create(
            employee_number=f"22000{n}",
            full_name=f"Person {n}",
            cost_code=cost_code,
            bu_lead=addon if n == 6 else lead,
        )
    # Someone outside the cost code must not be counted.
    Employee.objects.create(employee_number="990001", full_name="Elsewhere")
    put_answer(author_client, version, questions["MBCO-001"], {"value": 60})

    people = author_client.get(overview_url(version)).data["people"]
    assert people["headcount"] == 7
    assert people["mbco_percent"] == 60
    assert people["mbco_required"] == 5  # ceil(4.2)
    assert [e["full_name"] for e in people["employees"]][:2] == ["Person 0", "Person 1"]
    assert [(lead["lead_name"], lead["primary"]) for lead in people["bu_leads"]] == [
        ("Priya Lead", True),
        ("Ravi Shared", False),
    ]
    assert [c["full_name"] for c in people["coordinators"]] == ["Arun Coordinator"]


def test_mbco_required_is_none_without_a_percentage(author_client, version):
    Employee.objects.create(
        employee_number="220001", full_name="Person", cost_code=version.plan.cost_code
    )
    people = author_client.get(overview_url(version)).data["people"]
    assert people["headcount"] == 1
    assert people["mbco_required"] is None


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #


def test_upload_lists_and_detaches_a_network_diagram(author_client, version):
    response = author_client.post(
        attachments_url(version), {"file": png(), "type": "NETWORK_DIAGRAM"}, format="multipart"
    )
    assert response.status_code == 201, response.data
    assert response.data["document_type"] == "NETWORK_DIAGRAM"
    assert response.data["file_name"] == "diagram.png"
    assert response.data["uploaded_by"] == "arun"

    listed = author_client.get(f"{attachments_url(version)}?type=NETWORK_DIAGRAM").data
    assert [a["entity_document_id"] for a in listed] == [response.data["entity_document_id"]]
    assert author_client.get(overview_url(version)).data["stages"]["plan"] == {
        "status": "In Progress",
        "strategies": 0,
        "network_diagrams": 1,
    }

    # The generated-document list stays clean.
    docs = author_client.get(
        reverse("documents:version-documents", args=[version.plan_version_id])
    ).data
    assert docs == []

    assert (
        author_client.delete(
            attachment_url(version, response.data["entity_document_id"])
        ).status_code
        == 204
    )
    assert author_client.get(attachments_url(version)).data == []
    assert not EntityDocument.objects.filter(pk=response.data["entity_document_id"]).exists()


def test_upload_rejects_a_missing_file_and_an_unknown_type(author_client, version):
    response = author_client.post(attachments_url(version), {"type": "NETWORK_DIAGRAM"})
    assert response.status_code == 400
    assert response.data["code"] == "file_required"

    response = author_client.post(
        attachments_url(version), {"file": png(), "type": "SELFIE"}, format="multipart"
    )
    assert response.status_code == 400
    assert "type" in response.data["field_errors"]


def test_only_an_author_may_attach(onlooker_client, viewer_client, version):
    for client in (onlooker_client, viewer_client):
        response = client.post(
            attachments_url(version), {"file": png(), "type": "NETWORK_DIAGRAM"}, format="multipart"
        )
        assert response.status_code == 403
        assert client.get(attachments_url(version)).status_code == 200


def test_a_closed_version_refuses_attachments(author_client, version):
    version.status = PlanStatus.APPROVED
    version.save(update_fields=["status"])
    response = author_client.post(
        attachments_url(version), {"file": png(), "type": "NETWORK_DIAGRAM"}, format="multipart"
    )
    assert response.status_code == 409
