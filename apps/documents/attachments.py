"""
Files a coordinator attaches to a plan version — today the network diagram.

    GET    /plan-versions/{id}/attachments/?type=NETWORK_DIAGRAM   list
    POST   /plan-versions/{id}/attachments/                        upload (multipart: file, type)
    DELETE /plan-versions/{id}/attachments/{entity_document_id}/   detach

Same store and the same signed-link download as generated documents
(`/documents/{entity_document_id}/link/`). They attach to the PLAN_VERSION entity
under their own `document_type`, which is what keeps them out of the generated
document list — `documents_for_version` filters on the DOCX/PDF types.

Detaching removes the `EntityDocument` row only. The underlying `Document` is
content-addressed and may be shared with another version (a copied plan, the
same diagram uploaded twice), so it is never deleted here.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.services import record_audit
from apps.core.models import AuditLog
from apps.documents.models import EntityDocument
from apps.documents.uploads import attach, store_upload
from apps.plans.access import PlanVersionScopedMixin
from apps.plans.childviews import NotAnAuthor, VersionNotEditable
from apps.plans.models import PlanVersion

#: What a plan author may attach, by `document_type`. Generated outputs
#: (BCP_DOCX / BCP_PDF) are deliberately not in this list — they are produced by
#: approval, never uploaded.
ATTACHMENT_TYPES = {
    "NETWORK_DIAGRAM": "Network diagram",
}


class AttachmentSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="document.file_name")
    mime_type = serializers.CharField(source="document.mime_type")
    file_size_bytes = serializers.IntegerField(source="document.file_size_bytes")
    uploaded_at = serializers.DateTimeField(source="document.uploaded_at")
    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = EntityDocument
        fields = [
            "entity_document_id",
            "document_type",
            "file_name",
            "mime_type",
            "file_size_bytes",
            "uploaded_at",
            "uploaded_by",
        ]

    def get_uploaded_by(self, attachment) -> str:
        user = attachment.document.uploaded_by
        return getattr(user, "display_name", "") if user else ""


class AttachmentInput(serializers.Serializer):
    type = serializers.ChoiceField(choices=list(ATTACHMENT_TYPES))


def attachments_for_version(version: PlanVersion, document_type: str | None = None):
    rows = EntityDocument.objects.filter(
        entity_type=EntityDocument.EntityType.PLAN_VERSION,
        entity_id=version.pk,
        document_type__in=[document_type] if document_type else list(ATTACHMENT_TYPES),
    )
    return rows.select_related("document", "document__uploaded_by").order_by(
        "-document__uploaded_at", "-entity_document_id"
    )


class _AttachmentViewBase(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def writable_version(self) -> PlanVersion:
        version = self.get_plan_version()
        if not self.caller_may_author(version):
            raise NotAnAuthor()
        if not version.is_editable:
            raise VersionNotEditable(version)
        return version


class VersionAttachmentsView(_AttachmentViewBase):
    @extend_schema(
        parameters=[OpenApiParameter("type", str, description="Filter by document type")],
        responses=AttachmentSerializer(many=True),
    )
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        wanted = (request.query_params.get("type") or "").strip() or None
        if wanted and wanted not in ATTACHMENT_TYPES:
            raise serializers.ValidationError({"type": [f"Unknown attachment type '{wanted}'."]})
        rows = attachments_for_version(version, wanted)
        return Response(AttachmentSerializer(rows, many=True).data)

    @extend_schema(request=AttachmentInput, responses={201: AttachmentSerializer})
    def post(self, request, *args, **kwargs):
        version = self.writable_version()
        body = AttachmentInput(data=request.data)
        body.is_valid(raise_exception=True)
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {
                    "detail": "Attach the file as 'file'.",
                    "code": "file_required",
                    "field_errors": {},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        document = store_upload(upload, actor=request.user)
        attachment = attach(
            document,
            entity_type=EntityDocument.EntityType.PLAN_VERSION,
            entity_id=version.pk,
            document_type=body.validated_data["type"],
        )
        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=request.user,
            entity_type="PlanVersionAttachment",
            entity_id=attachment.pk,
            detail={
                "plan_version_id": version.pk,
                "type": attachment.document_type,
                "file": document.file_name,
            },
            request=request,
        )
        return Response(AttachmentSerializer(attachment).data, status=http_status.HTTP_201_CREATED)


class VersionAttachmentDetailView(_AttachmentViewBase):
    @extend_schema(responses={204: None})
    def delete(self, request, *args, **kwargs):
        version = self.writable_version()
        attachment = (
            attachments_for_version(version).filter(pk=kwargs["entity_document_id"]).first()
        )
        if attachment is None:
            return Response(
                {
                    "detail": "That attachment is not on this plan version.",
                    "code": "not_found",
                    "field_errors": {},
                },
                status=http_status.HTTP_404_NOT_FOUND,
            )
        record_audit(
            action=AuditLog.Action.RECORD_DELETED,
            actor=request.user,
            entity_type="PlanVersionAttachment",
            entity_id=attachment.pk,
            detail={
                "plan_version_id": version.pk,
                "type": attachment.document_type,
                "file": attachment.document.file_name,
            },
            request=request,
        )
        attachment.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)
