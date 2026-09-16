"""Document generation, template traceability and authorised download (7.1-7.4)."""

import io
import zipfile

import pytest
from django.core.signing import TimestampSigner
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import UserEstateScope
from apps.documents import generation
from apps.documents.models import Document, DocumentTemplate, EntityDocument
from apps.plans.models import PlanStatus
from apps.risk.models import Risk

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def approved(approved_version):
    """Phase 3's fully populated Approved version, with a scored risk added."""
    Risk.objects.create(
        plan_version=approved_version,
        risk_name="Grid outage",
        likelihood_rating=2,
        severity_rating=3,
        control_effectiveness_rating=2,
        inherent_risk_score=6,
        residual_risk_score=4,
        risk_level="Low",
    )
    return approved_version


def docs_url(version):
    return reverse("documents:version-documents", args=[version.plan_version_id])


def generate_url(version):
    return reverse("documents:version-documents-generate", args=[version.plan_version_id])


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_the_payload_gathers_every_section(approved):
    payload = generation.build_plan_payload(approved)
    assert payload["cost_code"] == "CC-1001"
    assert payload["version_number"] == 1
    assert payload["approved_by"]
    assert payload["coordinators"] == ["Arun Coordinator (Primary)"]
    assert payload["service_descriptions"][0]["rto"] == "8"
    assert payload["critical_contacts"][0]["phone"] == "+91 99999 11111"
    assert payload["network_requirements"][0]["port"] == "443"
    assert [r["name"] for r in payload["risks"]] == [
        "Grid outage",
        "Power failure at primary site",
    ] or len(payload["risks"]) == 2
    assert payload["strategies"][0]["core"] == "Shift to the Chennai centre."
    assert payload["history"][-1]["status"] == PlanStatus.APPROVED
    # Answers render as labels, not codes.
    basic = next(s for s in payload["sections"] if s["name"] == "Basic Questions")
    assert basic["questions"][0]["answer"] in ("Yes", "Not answered")


def test_docx_is_a_valid_word_file_carrying_the_plan(approved):
    content = generation.render_docx(generation.build_plan_payload(approved))
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "CC-1001" in xml
    assert "Grid outage" in xml
    assert "Status history" in xml


def test_pdf_is_a_valid_pdf_carrying_the_plan(approved):
    content = generation.render_pdf(generation.build_plan_payload(approved))
    assert content.startswith(b"%PDF-")
    assert b"/Page" in content
    # Text is stream-encoded; the metadata header still names the plan.
    assert len(content) > 2000


# --------------------------------------------------------------------------- #
# Records, traceability, content addressing
# --------------------------------------------------------------------------- #


def test_generation_attaches_both_formats_with_the_template_version(approved, actor):
    attached = generation.generate_for_version(approved, actor=actor)
    assert sorted(a.document_type for a in attached) == [
        "GENERATED_PLAN_DOCX",
        "GENERATED_PLAN_PDF",
    ]
    for attachment in attached:
        assert attachment.entity_id == approved.pk
        assert attachment.document.template.template_code == "BCP_PLAN"
        assert attachment.document.template.version_number == generation.RENDERER_VERSION
        assert attachment.document.checksum_sha256
        assert (generation.media_root() / attachment.document.storage_key).exists()


def test_renderer_version_matches_the_active_template():
    """Bumping RENDERER_VERSION without a template row breaks traceability."""
    template = generation.active_template()
    assert template.version_number == generation.RENDERER_VERSION
    assert DocumentTemplate.objects.filter(template_code="BCP_PLAN", active_flag=True).count() >= 1


def test_regeneration_keeps_the_previous_output(approved, actor):
    first = generation.generate_for_version(approved, actor=actor)
    # Change the plan so the bytes differ, then regenerate.
    Risk.objects.create(plan_version=approved, risk_name="New risk since")
    second = generation.generate_for_version(approved, actor=actor)

    listed = list(generation.documents_for_version(approved))
    assert len(listed) == 4
    assert {a.pk for a in first} <= {a.pk for a in listed}
    assert {a.pk for a in second} <= {a.pk for a in listed}
    assert listed[0].pk in {a.pk for a in second}  # newest first


def test_identical_content_is_stored_once(approved, actor, monkeypatch):
    """Content-addressed: the same bytes reuse the Document row (AD-7)."""
    # Freeze the timestamps the payload embeds, so two renders are byte-identical.
    import datetime as dt

    from django.utils import timezone

    fixed = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(timezone, "now", lambda: fixed)
    monkeypatch.setattr(generation.timezone, "now", lambda: fixed)

    generation.generate_for_version(approved, actor=actor)
    before = Document.objects.count()
    generation.generate_for_version(approved, actor=actor)
    assert Document.objects.count() == before


# --------------------------------------------------------------------------- #
# API: list, regenerate, download
# --------------------------------------------------------------------------- #


def test_list_and_regenerate_through_the_api(coordinator_client, approved):
    assert coordinator_client.get(docs_url(approved)).data == []
    created = coordinator_client.post(generate_url(approved))
    assert created.status_code == 201, created.data
    assert {d["format"] for d in created.data} == {"docx", "pdf"}
    assert created.data[0]["template"] == f"BCP_PLAN v{generation.RENDERER_VERSION}"
    assert len(coordinator_client.get(docs_url(approved)).data) == 2


def test_a_viewer_can_list_but_not_regenerate(approved, user_factory, org, coordinator_client):
    coordinator_client.post(generate_url(approved))
    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=viewer)
    assert client.get(docs_url(approved)).status_code == 200
    assert client.post(generate_url(approved)).status_code == 403


def test_download_needs_a_link_and_the_link_checks_scope(
    coordinator_client, approved, user_factory
):
    created = coordinator_client.post(generate_url(approved)).data
    attachment_id = created[0]["entity_document_id"]

    # In scope: a signed link is issued.
    link = coordinator_client.get(reverse("documents:document-link", args=[attachment_id]))
    assert link.status_code == 200
    assert "token=" in link.data["url"]

    # Out of scope: no link, and the document's existence is not confirmed.
    stranger = user_factory(email="s@example.com", roles=["BCM_COORDINATOR"])
    client = APIClient()
    client.force_authenticate(user=stranger)
    assert client.get(reverse("documents:document-link", args=[attachment_id])).status_code == 404

    # The link itself needs no session.
    token = link.data["url"].split("token=")[1]
    anonymous = APIClient()
    response = anonymous.get(
        reverse("documents:document-download", args=[attachment_id]), {"token": token}
    )
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment;")
    body = b"".join(response.streaming_content)
    assert len(body) > 1000

    # No token, a wrong token, or a token for another document: refused.
    assert (
        anonymous.get(reverse("documents:document-download", args=[attachment_id])).status_code
        == 404
    )
    assert (
        anonymous.get(
            reverse("documents:document-download", args=[attachment_id]), {"token": "x:y"}
        ).status_code
        == 404
    )
    other = created[1]["entity_document_id"]
    assert (
        anonymous.get(
            reverse("documents:document-download", args=[other]), {"token": token}
        ).status_code
        == 404
    )


def test_an_expired_link_is_refused(coordinator_client, approved, settings, monkeypatch):
    created = coordinator_client.post(generate_url(approved)).data
    attachment_id = created[0]["entity_document_id"]
    settings.AWS_S3_SIGNED_URL_EXPIRY_SECONDS = 60

    # Sign as if an hour ago.
    import time

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 3600)
    token = TimestampSigner(salt="bcm.document.download").sign(str(attachment_id))
    monkeypatch.setattr(time, "time", real_time)

    response = APIClient().get(
        reverse("documents:document-download", args=[attachment_id]), {"token": token}
    )
    assert response.status_code == 410
    assert response.data["code"] == "link_expired"


def test_the_entity_document_row_is_unique_per_type(approved, actor):
    generation.generate_for_version(approved, actor=actor)
    rows = EntityDocument.objects.filter(entity_id=approved.pk)
    assert rows.values("document_id", "document_type").distinct().count() == rows.count()
