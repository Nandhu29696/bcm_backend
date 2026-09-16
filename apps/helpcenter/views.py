"""
Help library (Phase 9.1).

    GET    /help/?q=&category=       search the catalogue (any signed-in user)
    GET    /help/categories/         distinct categories, for the filter
    POST   /help/                    multipart: title, description, category, display_order, file
    PATCH  /help/{id}/               catalogue fields, optionally a replacement file
    DELETE /help/{id}/               soft delete

Reading is open to every active user - the library has no estate dimension;
it is the BCM team's guidance for everyone. Writing is for administrators and
document controllers. The file itself is a `Document` attached as a
HELP_RESOURCE, downloaded through the same signed link as everything else.
"""

from __future__ import annotations

from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser, user_role_codes
from apps.accounts.roles import RoleCode
from apps.accounts.services import record_audit
from apps.core.models import AuditLog
from apps.documents.models import EntityDocument
from apps.documents.uploads import attach, store_upload
from apps.helpcenter.models import HelpResource

DOCUMENT_TYPE = "HELP_DOCUMENT"
MANAGER_ROLES = (RoleCode.ADMIN, RoleCode.DOCUMENT_CONTROLLER)


class HelpResourceSerializer(serializers.ModelSerializer):
    document = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = HelpResource
        fields = [
            "help_resource_id",
            "title",
            "description",
            "category",
            "display_order",
            "document",
            "created_by_name",
            "created_at",
            "updated_at",
        ]

    def get_document(self, resource):
        if not resource.document_id:
            return None
        attachments = self.context.get("attachments")
        if attachments is not None:
            attachment_id = attachments.get((resource.document_id, resource.pk))
        else:
            attachment = EntityDocument.objects.filter(
                document_id=resource.document_id,
                entity_type=EntityDocument.EntityType.HELP_RESOURCE,
                entity_id=resource.pk,
            ).first()
            attachment_id = attachment.pk if attachment else None
        return {
            "entity_document_id": attachment_id,
            "file_name": resource.document.file_name,
            "file_size_bytes": resource.document.file_size_bytes,
            "mime_type": resource.document.mime_type,
        }

    def get_created_by_name(self, resource):
        return resource.created_by.display_name if resource.created_by_id else ""


class HelpResourceInput(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    category = serializers.CharField(
        required=False, allow_blank=True, max_length=80, default="General"
    )
    display_order = serializers.IntegerField(required=False, allow_null=True)


def _queryset():
    return HelpResource.objects.select_related("document", "created_by")


def _is_manager(request) -> bool:
    return bool(user_role_codes(request.user) & set(MANAGER_ROLES))


def _require_manager(request) -> None:
    if not _is_manager(request):
        raise PermissionDenied(
            "Only an administrator or document controller can manage the help library."
        )


class HelpListView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(responses=HelpResourceSerializer(many=True))
    def get(self, request, *args, **kwargs):
        rows = _queryset()
        query = (request.query_params.get("q") or "").strip()
        if query:
            rows = rows.filter(
                Q(title__icontains=query)
                | Q(description__icontains=query)
                | Q(document__file_name__icontains=query)
            )
        category = (request.query_params.get("category") or "").strip()
        if category:
            rows = rows.filter(category=category)
        rows = list(rows)
        attachments = {
            (document_id, entity_id): pk
            for document_id, entity_id, pk in EntityDocument.objects.filter(
                entity_type=EntityDocument.EntityType.HELP_RESOURCE,
                entity_id__in=[r.pk for r in rows],
            ).values_list("document_id", "entity_id", "pk")
        }
        data = HelpResourceSerializer(rows, many=True, context={"attachments": attachments}).data
        return Response({"results": data, "can_manage": _is_manager(request)})

    @extend_schema(request=HelpResourceInput, responses={201: HelpResourceSerializer})
    def post(self, request, *args, **kwargs):
        _require_manager(request)
        body = HelpResourceInput(data=request.data)
        body.is_valid(raise_exception=True)
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {
                    "detail": "Attach the document as 'file'.",
                    "code": "file_required",
                    "field_errors": {},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        document = store_upload(upload, actor=request.user)
        resource = HelpResource.objects.create(
            document=document, created_by=request.user, **body.validated_data
        )
        attach(
            document,
            entity_type=EntityDocument.EntityType.HELP_RESOURCE,
            entity_id=resource.pk,
            document_type=DOCUMENT_TYPE,
        )
        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=request.user,
            entity_type="HelpResource",
            entity_id=resource.pk,
            detail={"title": resource.title, "file": document.file_name},
        )
        return Response(
            HelpResourceSerializer(_queryset().get(pk=resource.pk)).data,
            status=http_status.HTTP_201_CREATED,
        )


class HelpCategoriesView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        rows = (
            HelpResource.objects.exclude(category="")
            .order_by("category")
            .values_list("category", flat=True)
            .distinct()
        )
        return Response(list(rows))


class HelpDetailView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _resource(self, request) -> HelpResource:
        _require_manager(request)
        return get_object_or_404(_queryset(), pk=self.kwargs["help_resource_id"])

    @extend_schema(request=HelpResourceInput, responses=HelpResourceSerializer)
    def patch(self, request, *args, **kwargs):
        resource = self._resource(request)
        body = HelpResourceInput(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        for field, value in body.validated_data.items():
            setattr(resource, field, value)
        upload = request.FILES.get("file")
        if upload is not None:
            document = store_upload(upload, actor=request.user)
            resource.document = document
            attach(
                document,
                entity_type=EntityDocument.EntityType.HELP_RESOURCE,
                entity_id=resource.pk,
                document_type=DOCUMENT_TYPE,
            )
        resource.save()
        record_audit(
            action=AuditLog.Action.RECORD_UPDATED,
            actor=request.user,
            entity_type="HelpResource",
            entity_id=resource.pk,
            detail={
                **{k: str(v) for k, v in body.validated_data.items()},
                "file_replaced": upload is not None,
            },
        )
        return Response(HelpResourceSerializer(_queryset().get(pk=resource.pk)).data)

    def delete(self, request, *args, **kwargs):
        resource = self._resource(request)
        resource.soft_delete()
        record_audit(
            action=AuditLog.Action.RECORD_DELETED,
            actor=request.user,
            entity_type="HelpResource",
            entity_id=resource.pk,
        )
        return Response(status=http_status.HTTP_204_NO_CONTENT)
