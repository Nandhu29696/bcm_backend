"""
Self-service profile (the Profile screen).

    GET    /auth/me/            the current user (existing)
    PATCH  /auth/me/            display_name, phone_number, job_title (MeView.patch, this serializer)
    POST   /auth/me/avatar/     multipart `image` -> resized to a square JPEG data URL
    DELETE /auth/me/avatar/

Only the user's own, non-authoritative fields are editable here. Email is the
login identity and roles, status and scope are the administrator's; the
employee record is HR's.
"""

from __future__ import annotations

import base64
import io

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import UserAccount
from apps.accounts.serializers import CurrentUserSerializer
from apps.core.exceptions import DomainError

MAX_AVATAR_UPLOAD_BYTES = 5 * 1024 * 1024


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAccount
        fields = ["display_name", "phone_number", "job_title"]
        extra_kwargs = {"display_name": {"min_length": 2}}


class AvatarRejected(DomainError):
    default_code = "avatar_rejected"


def avatar_data_url(upload) -> str:
    """Validate an uploaded image and return it as a square JPEG data URL."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if upload.size > MAX_AVATAR_UPLOAD_BYTES:
        raise AvatarRejected("The picture is larger than 5 MB.")
    try:
        image = Image.open(upload)
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise AvatarRejected("That file is not an image we can read (use PNG or JPEG).") from exc
    image = ImageOps.exif_transpose(image).convert("RGB")
    size = settings.AVATAR_SIZE_PX
    image = ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


class AvatarView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(summary="Upload my profile picture", responses=CurrentUserSerializer)
    def post(self, request, *args, **kwargs):
        upload = request.FILES.get("image")
        if upload is None:
            return Response(
                {
                    "detail": "Attach the picture as 'image'.",
                    "code": "image_required",
                    "field_errors": {},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        request.user.avatar_data_url = avatar_data_url(upload)
        request.user.save(update_fields=["avatar_data_url"])
        return Response(CurrentUserSerializer(request.user).data)

    def delete(self, request, *args, **kwargs):
        request.user.avatar_data_url = ""
        request.user.save(update_fields=["avatar_data_url"])
        return Response(CurrentUserSerializer(request.user).data)
