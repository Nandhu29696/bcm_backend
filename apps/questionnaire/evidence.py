"""
Evidence behind an answer — the contract excerpt that states the RTO, the
penalty clause behind a "Yes".

    GET    /plan-versions/{id}/questions/{question_id}/evidence/          list
    POST   /plan-versions/{id}/questions/{question_id}/evidence/          upload (multipart: file)
    DELETE /plan-versions/{id}/questions/{question_id}/evidence/{entity_document_id}/

Which questions ask for evidence, and when, is data on the question
(`evidence_flag`, `evidence_when_value`, `evidence_required`) — see the model.
A required file is part of what counts as "answered" (4.6): the section stays
short of complete until it is there, and submission says so.

Storage is the shared document store: an `EntityDocument` on the PLAN_VERSION
entity whose `document_type` names the question, so the signed-link download
and the estate-scope check work unchanged. Copy-on-write carries these rows to
the new version along with the answers they support (`plans.versioning`).
"""

from __future__ import annotations

from collections import defaultdict

from drf_spectacular.utils import extend_schema
from rest_framework import status as http_status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.services import record_audit
from apps.core.models import AuditLog
from apps.documents.attachments import AttachmentSerializer
from apps.documents.models import EntityDocument
from apps.documents.uploads import attach, store_upload
from apps.plans.access import PlanVersionScopedMixin
from apps.plans.childviews import NotAnAuthor, VersionNotEditable
from apps.plans.models import PlanVersion
from apps.questionnaire.models import Question

EVIDENCE_TYPE_PREFIX = "QUESTION_EVIDENCE:"


def document_type_for(question: Question) -> str:
    return f"{EVIDENCE_TYPE_PREFIX}{question.pk}"


def question_id_from(document_type: str) -> int | None:
    if not document_type.startswith(EVIDENCE_TYPE_PREFIX):
        return None
    try:
        return int(document_type[len(EVIDENCE_TYPE_PREFIX) :])
    except ValueError:
        return None


def evidence_rows(version: PlanVersion):
    """Every evidence attachment on the version, any question."""
    return (
        EntityDocument.objects.filter(
            entity_type=EntityDocument.EntityType.PLAN_VERSION,
            entity_id=version.pk,
            document_type__startswith=EVIDENCE_TYPE_PREFIX,
        )
        .select_related("document", "document__uploaded_by")
        .order_by("document__uploaded_at", "entity_document_id")
    )


def evidence_by_question(version: PlanVersion) -> dict[int, list[EntityDocument]]:
    grouped: dict[int, list[EntityDocument]] = defaultdict(list)
    for row in evidence_rows(version):
        question_id = question_id_from(row.document_type)
        if question_id is not None:
            grouped[question_id].append(row)
    return grouped


def evidence_payload(question: Question, files: list[EntityDocument]) -> dict:
    """The question's evidence rule and files, for the editor."""
    return {
        "offered": question.evidence_flag,
        "when_value": question.evidence_when_value or None,
        "required": question.evidence_required,
        "files": AttachmentSerializer(files, many=True).data,
    }


class _EvidenceViewBase(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_question(self) -> Question:
        from django.shortcuts import get_object_or_404

        return get_object_or_404(Question, pk=self.kwargs["question_id"], evidence_flag=True)

    def writable_version(self) -> PlanVersion:
        version = self.get_plan_version()
        if not self.caller_may_author(version):
            raise NotAnAuthor()
        if not version.is_editable:
            raise VersionNotEditable(version)
        return version

    def files_for(self, version: PlanVersion, question: Question):
        return evidence_rows(version).filter(document_type=document_type_for(question))

    def respond(self, version: PlanVersion, question: Question, *, status=http_status.HTTP_200_OK):
        from apps.questionnaire.services import recompute_section_statuses
        from apps.questionnaire.views import _section_payload

        summaries = recompute_section_statuses(version, actor=self.request.user)
        return Response(
            {
                "question_id": question.pk,
                "files": AttachmentSerializer(self.files_for(version, question), many=True).data,
                "sections": _section_payload(summaries),
            },
            status=status,
        )


class QuestionEvidenceView(_EvidenceViewBase):
    @extend_schema(
        responses=AttachmentSerializer(many=True), summary="Evidence files for a question"
    )
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        rows = self.files_for(version, self.get_question())
        return Response(AttachmentSerializer(rows, many=True).data)

    @extend_schema(request=None, summary="Attach an evidence file to a question")
    def post(self, request, *args, **kwargs):
        version = self.writable_version()
        question = self.get_question()
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
            document_type=document_type_for(question),
        )
        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=request.user,
            entity_type="QuestionEvidence",
            entity_id=attachment.pk,
            detail={
                "plan_version_id": version.pk,
                "question": question.question_code,
                "file": document.file_name,
            },
            request=request,
        )
        return self.respond(version, question, status=http_status.HTTP_201_CREATED)


class QuestionEvidenceDetailView(_EvidenceViewBase):
    @extend_schema(summary="Remove an evidence file from a question")
    def delete(self, request, *args, **kwargs):
        version = self.writable_version()
        question = self.get_question()
        attachment = (
            self.files_for(version, question).filter(pk=kwargs["entity_document_id"]).first()
        )
        if attachment is None:
            return Response(
                {
                    "detail": "That file is not attached to this question.",
                    "code": "not_found",
                    "field_errors": {},
                },
                status=http_status.HTTP_404_NOT_FOUND,
            )
        record_audit(
            action=AuditLog.Action.RECORD_DELETED,
            actor=request.user,
            entity_type="QuestionEvidence",
            entity_id=attachment.pk,
            detail={
                "plan_version_id": version.pk,
                "question": question.question_code,
                "file": attachment.document.file_name,
            },
            request=request,
        )
        attachment.delete()
        return self.respond(version, question)
