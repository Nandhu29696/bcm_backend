"""
Generated documents (Phase 7.3-7.4).

    GET  /plan-versions/{id}/documents/               list, newest first
    POST /plan-versions/{id}/documents/generate/      regenerate on demand
    GET  /documents/{entity_document_id}/link/        a short-lived signed URL (any attachable entity)
    GET  /documents/{entity_document_id}/download/    the file (token-authenticated)

Permission is checked on the LINKED ENTITY, not the document: to get a link you
must be able to see the plan version it belongs to. The download itself is
authenticated by the signed token alone, because a browser opening a download
in a new tab sends no Authorization header. The token is the credential, and
it expires.
"""

from __future__ import annotations

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.documents.generation import documents_for_version, generate_for_version, media_root
from apps.documents.models import EntityDocument
from apps.plans.access import PlanVersionScopedMixin
from apps.plans.childviews import NotAnAuthor
from apps.plans.models import PlanVersion

SIGNER_SALT = "bcm.document.download"


class GeneratedDocumentSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="document.file_name")
    mime_type = serializers.CharField(source="document.mime_type")
    file_size_bytes = serializers.IntegerField(source="document.file_size_bytes")
    checksum_sha256 = serializers.CharField(source="document.checksum_sha256")
    generated_at = serializers.DateTimeField(source="document.uploaded_at")
    template = serializers.SerializerMethodField()
    format = serializers.SerializerMethodField()

    class Meta:
        model = EntityDocument
        fields = [
            "entity_document_id",
            "document_type",
            "format",
            "file_name",
            "mime_type",
            "file_size_bytes",
            "checksum_sha256",
            "generated_at",
            "template",
        ]

    def get_template(self, attachment) -> str:
        template = attachment.document.template
        return f"{template.template_code} v{template.version_number}" if template else ""

    def get_format(self, attachment) -> str:
        return "pdf" if attachment.document_type.endswith("PDF") else "docx"


class VersionDocumentsView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=GeneratedDocumentSerializer(many=True))
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        return Response(GeneratedDocumentSerializer(documents_for_version(version), many=True).data)


class RegenerateView(PlanVersionScopedMixin, APIView):
    """Regenerate on demand (7.4). Earlier outputs are kept."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(request=None, responses=GeneratedDocumentSerializer(many=True))
    def post(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if not (self.caller_may_author(version) or self.caller_may_approve(version)):
            raise NotAnAuthor()
        attachments = generate_for_version(version, actor=request.user)
        return Response(
            GeneratedDocumentSerializer(attachments, many=True).data,
            status=http_status.HTTP_201_CREATED,
        )


#: How each attachable entity reaches an estate, for the scope check below.
ENTITY_ESTATE_PATHS = {
    EntityDocument.EntityType.PLAN_VERSION: (PlanVersion, "plan__cost_code__estate_id"),
    EntityDocument.EntityType.TEST_OUTCOME: (
        "testing.TestOutcome",
        "test__plan_version__plan__cost_code__estate_id",
    ),
    EntityDocument.EntityType.TEST: ("testing.Test", "plan_version__plan__cost_code__estate_id"),
    EntityDocument.EntityType.CRISIS_EVENT: ("crisis.CrisisEvent", "cost_code__estate_id"),
    # No estate dimension: every active user may read the help library.
    EntityDocument.EntityType.HELP_RESOURCE: ("helpcenter.HelpResource", None),
}


def caller_sees_entity(scope, attachment: EntityDocument) -> bool:
    """Scope through the linked entity, exactly as that entity's own endpoints do."""
    from django.apps import apps as django_apps

    if attachment.entity_type == EntityDocument.EntityType.REPORT_REQUEST:
        # A report is the requester's own: it was built in their scope, for them.
        return (
            django_apps.get_model("reporting.ReportRequest")
            .objects.filter(pk=attachment.entity_id, requested_by=scope.user)
            .exists()
        )
    entry = ENTITY_ESTATE_PATHS.get(attachment.entity_type)
    if entry is None:
        return False
    model, path = entry
    if isinstance(model, str):
        model = django_apps.get_model(model)
    rows = model.objects.filter(pk=attachment.entity_id)
    if path is not None and not scope.sees_all_estates:
        rows = rows.filter(**{f"{path}__in": scope.estate_ids})
    return rows.exists()


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=SIGNER_SALT)


class DocumentLinkView(ScopedQuerySetMixin, APIView):
    """Issue a short-lived download URL after checking the linked entity."""

    estate_scope_path = None
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        attachment = get_object_or_404(
            EntityDocument.objects.select_related("document"), pk=self.kwargs["entity_document_id"]
        )
        if not caller_sees_entity(self.get_scope(), attachment):
            raise Http404

        token = _signer().sign(str(attachment.pk))
        expires = settings.AWS_S3_SIGNED_URL_EXPIRY_SECONDS
        path = f"{settings.API_BASE_PATH}/documents/{attachment.pk}/download/?token={token}"
        return Response(
            {
                # Relative: the browser reaches the API through its own origin (a
                # dev proxy, a reverse proxy in production), so a relative link
                # works wherever the page does. The absolute form is for email.
                "url": path,
                "absolute_url": f"{settings.PUBLIC_BASE_URL}{path}",
                "expires_in": expires,
                "file_name": attachment.document.file_name,
            }
        )


class DocumentDownloadView(APIView):
    """Serve the bytes. The token is the credential; nothing else is checked."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, *args, **kwargs):
        token = request.query_params.get("token", "")
        try:
            signed_id = _signer().unsign(token, max_age=settings.AWS_S3_SIGNED_URL_EXPIRY_SECONDS)
        except SignatureExpired:
            return Response(
                {
                    "detail": "This download link has expired.",
                    "code": "link_expired",
                    "field_errors": {},
                },
                status=http_status.HTTP_410_GONE,
            )
        except BadSignature:
            raise Http404 from None
        if signed_id != str(self.kwargs["entity_document_id"]):
            raise Http404

        attachment = get_object_or_404(
            EntityDocument.objects.select_related("document"), pk=self.kwargs["entity_document_id"]
        )
        path = media_root() / attachment.document.storage_key
        if not path.exists():
            raise Http404
        response = FileResponse(
            path.open("rb"),
            content_type=attachment.document.mime_type or "application/octet-stream",
        )
        response["Content-Disposition"] = f'attachment; filename="{attachment.document.file_name}"'
        return response
