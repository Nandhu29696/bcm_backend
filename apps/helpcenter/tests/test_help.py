"""Help library: search, upload, download with permission enforced (Phase 9.1)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.helpcenter.models import HelpResource

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def controller_client(user_factory):
    client = APIClient()
    client.force_authenticate(
        user=user_factory(email="docs@example.com", roles=["BCM_DOCUMENT_CONTROLLER"])
    )
    return client


def upload(client, title, **fields):
    return client.post(
        reverse("helpcenter:help-list"),
        {
            "title": title,
            "description": fields.get("description", ""),
            "category": fields.get("category", "Guides"),
            "file": SimpleUploadedFile(
                fields.get("file_name", "guide.pdf"), fields.get("content", b"%PDF-1.4 guide")
            ),
        },
        format="multipart",
    )


class TestLibrary:
    def test_controller_uploads_everyone_reads_and_downloads(
        self, controller_client, coordinator_client, api_client
    ):
        created = upload(
            controller_client,
            "BCP authoring guide",
            description="How to fill in each section",
            category="Guides",
        )
        assert created.status_code == 201, created.data
        assert created.data["document"]["file_name"] == "guide.pdf"

        listing = coordinator_client.get(reverse("helpcenter:help-list"))
        assert listing.status_code == 200 and listing.data["can_manage"] is False
        assert [r["title"] for r in listing.data["results"]] == ["BCP authoring guide"]

        link = coordinator_client.get(
            reverse(
                "documents:document-link", args=[created.data["document"]["entity_document_id"]]
            )
        )
        assert link.status_code == 200
        download = api_client.get(link.data["url"])
        assert (
            download.status_code == 200
            and b"".join(download.streaming_content) == b"%PDF-1.4 guide"
        )

    def test_search_and_category_filter(self, controller_client, coordinator_client):
        upload(
            controller_client,
            "Call tree runbook",
            description="Escalation steps",
            category="Runbooks",
            file_name="runbook.docx",
        )
        upload(
            controller_client,
            "Risk scoring FAQ",
            description="How residual scores work",
            category="Guides",
            file_name="faq.pdf",
        )
        url = reverse("helpcenter:help-list")
        assert [
            r["title"] for r in coordinator_client.get(url, {"q": "residual"}).data["results"]
        ] == ["Risk scoring FAQ"]
        assert [
            r["title"] for r in coordinator_client.get(url, {"q": "runbook.docx"}).data["results"]
        ] == ["Call tree runbook"]
        assert [
            r["title"]
            for r in coordinator_client.get(url, {"category": "Runbooks"}).data["results"]
        ] == ["Call tree runbook"]
        assert coordinator_client.get(reverse("helpcenter:help-categories")).data == [
            "Guides",
            "Runbooks",
        ]

    def test_coordinator_cannot_upload_edit_or_delete(self, controller_client, coordinator_client):
        created = upload(controller_client, "Doc")
        assert upload(coordinator_client, "Mine").status_code == 403
        detail = reverse("helpcenter:help-detail", args=[created.data["help_resource_id"]])
        assert coordinator_client.patch(detail, {"title": "x"}).status_code == 403
        assert coordinator_client.delete(detail).status_code == 403

    def test_edit_replaces_the_file_and_delete_hides_it_and_its_download(
        self, controller_client, coordinator_client
    ):
        created = upload(controller_client, "Doc", file_name="v1.pdf")
        detail = reverse("helpcenter:help-detail", args=[created.data["help_resource_id"]])
        edited = controller_client.patch(
            detail,
            {"title": "Doc v2", "file": SimpleUploadedFile("v2.pdf", b"%PDF-1.4 v2")},
            format="multipart",
        )
        assert (
            edited.status_code == 200
            and edited.data["title"] == "Doc v2"
            and edited.data["document"]["file_name"] == "v2.pdf"
        )
        attachment_id = edited.data["document"]["entity_document_id"]

        assert controller_client.delete(detail).status_code == 204
        assert coordinator_client.get(reverse("helpcenter:help-list")).data["results"] == []
        assert (
            HelpResource.all_objects.get(pk=created.data["help_resource_id"]).active_flag is False
        )
        assert (
            coordinator_client.get(
                reverse("documents:document-link", args=[attachment_id])
            ).status_code
            == 404
        )

    def test_unacceptable_file_is_refused(self, controller_client):
        response = upload(controller_client, "Bad", file_name="tool.exe", content=b"MZ")
        assert response.status_code == 400 and response.data["code"] == "upload_rejected"
        assert not HelpResource.objects.exists()
