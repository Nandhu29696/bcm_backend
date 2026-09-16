"""Self-service profile: details and picture."""

import base64
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def me(user_factory):
    return user_factory(email="me@example.com", display_name="Me", roles=["BCM_VIEWER"])


@pytest.fixture
def client(me):
    client = APIClient()
    client.force_authenticate(user=me)
    return client


def png(width=640, height=400, colour=(200, 40, 40)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


class TestProfile:
    def test_update_my_details(self, client, me):
        response = client.patch(
            reverse("accounts:auth:me"),
            {
                "display_name": "Meera K",
                "phone_number": "+91 98000 00000",
                "job_title": "BCM Coordinator",
            },
            format="json",
        )
        assert response.status_code == 200, response.data
        assert (
            response.data["display_name"] == "Meera K"
            and response.data["job_title"] == "BCM Coordinator"
        )
        me.refresh_from_db()
        assert me.phone_number == "+91 98000 00000"
        assert AuditLog.objects.filter(
            entity_type="UserAccount", entity_id=me.pk, actor=me
        ).exists()

    def test_identity_and_authority_are_not_mine_to_change(self, client, me):
        response = client.patch(
            reverse("accounts:auth:me"),
            {"email": "other@example.com", "user_status": "Pending", "mfa_enabled": False},
            format="json",
        )
        assert response.status_code == 200
        me.refresh_from_db()
        assert (
            me.email == "me@example.com" and me.user_status == "Active" and me.mfa_enabled is True
        )

    def test_display_name_must_be_real(self, client):
        response = client.patch(reverse("accounts:auth:me"), {"display_name": "M"}, format="json")
        assert response.status_code == 400 and "display_name" in response.data["field_errors"]

    def test_upload_resizes_to_a_square_jpeg_data_url(self, client, me, settings):
        settings.AVATAR_SIZE_PX = 64
        response = client.post(
            reverse("accounts:auth:me-avatar"),
            {"image": SimpleUploadedFile("photo.png", png(), content_type="image/png")},
            format="multipart",
        )
        assert response.status_code == 200, response.data
        data_url = response.data["avatar_data_url"]
        assert data_url.startswith("data:image/jpeg;base64,")
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
        assert image.size == (64, 64) and image.format == "JPEG"
        assert len(data_url) < 20_000

        assert client.get(reverse("accounts:auth:me")).data["avatar_data_url"] == data_url
        removed = client.delete(reverse("accounts:auth:me-avatar"))
        assert removed.status_code == 200 and removed.data["avatar_data_url"] == ""

    def test_not_an_image_is_refused(self, client):
        response = client.post(
            reverse("accounts:auth:me-avatar"),
            {
                "image": SimpleUploadedFile(
                    "photo.png", b"not really a png", content_type="image/png"
                )
            },
            format="multipart",
        )
        assert response.status_code == 400 and response.data["code"] == "avatar_rejected"

    def test_anonymous_cannot_touch_a_profile(self):
        assert (
            APIClient()
            .patch(reverse("accounts:auth:me"), {"display_name": "x"}, format="json")
            .status_code
            == 401
        )
