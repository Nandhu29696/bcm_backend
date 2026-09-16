"""
Evidence behind an answer.

    GET/POST/DELETE /plan-versions/{id}/questions/{question_id}/evidence/

BASIC-004 ("penalties for non-compliance?") requires the penalty clause when
the answer is Yes; RTO / MBCO / MAO / RPO offer the contract excerpt.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.assessments.models import SectionStatusValue
from apps.documents.models import EntityDocument
from apps.plans.models import PlanStatus
from apps.plans.versioning import copy_plan_version
from apps.questionnaire.evidence import document_type_for

pytestmark = pytest.mark.django_db


def evidence_url(version, question):
    return reverse("questionnaire:question-evidence", args=[version.plan_version_id, question.pk])


def evidence_detail_url(version, question, entity_document_id):
    return reverse(
        "questionnaire:question-evidence-detail",
        args=[version.plan_version_id, question.pk, entity_document_id],
    )


def put_answer(client, version, question, answer):
    url = reverse("questionnaire:answer", args=[version.plan_version_id, question.pk])
    return client.put(url, {"answer": answer}, format="json")


def pdf(name="msa-penalty-clause.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 " + b"0" * 64, content_type="application/pdf")


def upload(client, version, question, file=None):
    return client.post(evidence_url(version, question), {"file": file or pdf()}, format="multipart")


def basic_section(response, questions):
    return next(
        s for s in response.data["sections"] if s["section_id"] == questions["BASIC-001"].section_id
    )


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #


def test_a_yes_needs_the_penalty_clause_to_complete(author_client, version, questions):
    put_answer(author_client, version, questions["BASIC-001"], {"value": "NO"})
    response = put_answer(author_client, version, questions["BASIC-004"], {"value": "YES"})
    section = basic_section(response, questions)
    assert section["status"] == SectionStatusValue.IN_PROGRESS
    assert section["required_answered"] == 1  # the Yes does not count yet

    response = upload(author_client, version, questions["BASIC-004"])
    assert response.status_code == 201, response.data
    assert [f["file_name"] for f in response.data["files"]] == ["msa-penalty-clause.pdf"]
    section = basic_section(response, questions)
    assert section["status"] == SectionStatusValue.COMPLETED

    # Removing the only file reopens the section.
    file_id = response.data["files"][0]["entity_document_id"]
    response = author_client.delete(evidence_detail_url(version, questions["BASIC-004"], file_id))
    assert response.status_code == 200
    assert response.data["files"] == []
    assert basic_section(response, questions)["status"] == SectionStatusValue.IN_PROGRESS
    assert not EntityDocument.objects.filter(pk=file_id).exists()


def test_a_no_needs_no_evidence(author_client, version, questions):
    put_answer(author_client, version, questions["BASIC-001"], {"value": "NO"})
    response = put_answer(author_client, version, questions["BASIC-004"], {"value": "NO"})
    assert basic_section(response, questions)["status"] == SectionStatusValue.COMPLETED


def test_optional_evidence_never_blocks(author_client, version, questions):
    response = put_answer(author_client, version, questions["RTO-001"], {"value": 8})
    rto = next(
        s for s in response.data["sections"] if s["section_id"] == questions["RTO-001"].section_id
    )
    assert rto["status"] == SectionStatusValue.COMPLETED

    # ...but the file is still taken, listed and shown in the tree.
    assert upload(author_client, version, questions["RTO-001"]).status_code == 201
    listed = author_client.get(evidence_url(version, questions["RTO-001"])).data
    assert len(listed) == 1
    tree = author_client.get(
        reverse("questionnaire:questionnaire", args=[version.plan_version_id])
    ).data
    rto_q = next(
        q for s in tree["sections"] for q in s["questions"] if q["question_code"] == "RTO-001"
    )
    assert [f["file_name"] for f in rto_q["evidence"]["files"]] == ["msa-penalty-clause.pdf"]


def test_submission_is_refused_without_the_required_evidence(author_client, version, questions):
    from apps.plans.tests.test_workflow import complete_the_plan

    complete_the_plan(author_client, version)
    put_answer(author_client, version, questions["BASIC-004"], {"value": "YES"})
    url = reverse("plans:plan-version-submit", args=[version.plan_version_id])
    response = author_client.post(url, {"comments": ""}, format="json")
    assert response.status_code == 400
    assert response.data["code"] == "plan_incomplete"
    assert "Basic Questions" in response.data["detail"]

    upload(author_client, version, questions["BASIC-004"])
    response = author_client.post(url, {"comments": ""}, format="json")
    assert response.status_code == 200, response.data
    assert response.data["status"] == PlanStatus.PENDING_BU_LEAD_REVIEW


# --------------------------------------------------------------------------- #
# Rules and gates
# --------------------------------------------------------------------------- #


def test_a_question_without_an_evidence_rule_takes_none(author_client, version, questions):
    response = upload(author_client, version, questions["BASIC-001"])
    assert response.status_code == 404


def test_a_missing_file_is_a_400(author_client, version, questions):
    response = author_client.post(evidence_url(version, questions["RTO-001"]), {})
    assert response.status_code == 400
    assert response.data["code"] == "file_required"


def test_only_an_author_may_attach_evidence(onlooker_client, viewer_client, version, questions):
    for client in (onlooker_client, viewer_client):
        assert upload(client, version, questions["RTO-001"]).status_code == 403
        assert client.get(evidence_url(version, questions["RTO-001"])).status_code == 200


def test_a_closed_version_refuses_evidence(author_client, version, questions):
    version.status = PlanStatus.APPROVED
    version.save(update_fields=["status"])
    assert upload(author_client, version, questions["RTO-001"]).status_code == 409


def test_evidence_is_carried_to_a_copied_version(author_client, author, version, questions):
    upload(author_client, version, questions["RTO-001"])
    version.status = PlanStatus.APPROVED
    version.save(update_fields=["status"])

    copy = copy_plan_version(version, actor=author)
    carried = EntityDocument.objects.filter(
        entity_type=EntityDocument.EntityType.PLAN_VERSION,
        entity_id=copy.pk,
        document_type=document_type_for(questions["RTO-001"]),
    )
    assert carried.count() == 1
    # Same bytes, not a second copy of the file.
    assert (
        carried.get().document_id
        == EntityDocument.objects.get(
            entity_id=version.pk, document_type=document_type_for(questions["RTO-001"])
        ).document_id
    )
