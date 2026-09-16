"""
The plan editor's endpoints — journey step 5.

    GET    /api/v1/plan-versions/{id}/questionnaire/?context=BCP     the tree (4.1)
    PUT    /api/v1/plan-versions/{id}/answers/{question_id}/         save     (4.2)
    DELETE /api/v1/plan-versions/{id}/answers/{question_id}/         clear
    GET    /api/v1/plan-versions/{id}/questions/{question_id}/comments/
    POST   /api/v1/plan-versions/{id}/questions/{question_id}/comments/       (4.5)

Reading needs estate scope. Writing needs an authoring claim on the version —
admin, or a coordinator actively assigned to it. Seeing a plan is not the same
as owning it.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.assessments.models import AnswerContext, QuestionComment
from apps.plans.access import PlanVersionScopedMixin
from apps.questionnaire.models import Question
from apps.questionnaire.services import (
    AnswerValidationError,
    PlanNotEditable,
    build_questionnaire,
    clear_answer,
    save_answer,
)


def _context_from(request) -> str:
    context = request.query_params.get("context", AnswerContext.BCP)
    if context not in AnswerContext.values:
        raise serializers.ValidationError({"context": [f"Unknown context '{context}'."]})
    return context


def _section_payload(summaries) -> list[dict]:
    return [
        {
            "section_id": s.section.section_id,
            "status": s.status,
            "percent": s.percent,
            "required_visible": s.required_visible,
            "required_answered": s.required_answered,
        }
        for s in summaries
    ]


class AuthoringMixin(PlanVersionScopedMixin):
    """Adds the write gate on top of estate scoping."""

    def require_author(self, version) -> Response | None:
        if self.caller_may_author(version):
            return None
        return Response(
            {
                "detail": "Only an assigned coordinator or an administrator can change this plan.",
                "code": "not_an_author",
                "field_errors": {},
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )


class QuestionnaireView(PlanVersionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(
        parameters=[OpenApiParameter("context", str, description="Answer context; default BCP")],
        summary="The questionnaire for a plan version with answers merged (4.1)",
    )
    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        payload = build_questionnaire(version, _context_from(request))
        payload["can_author"] = self.caller_may_author(version)
        return Response(payload)


class AnswerSerializer(serializers.Serializer):
    answer = serializers.JSONField()
    comments = serializers.CharField(required=False, allow_blank=True, max_length=4000)


class AnswerView(AuthoringMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get_question(self) -> Question:
        return get_object_or_404(
            Question.objects.select_related("section"), pk=self.kwargs["question_id"]
        )

    @extend_schema(request=AnswerSerializer, summary="Save one answer (4.2)")
    def put(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if (refused := self.require_author(version)) is not None:
            return refused
        question = self.get_question()
        body = AnswerSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            row, summaries = save_answer(
                version=version,
                question=question,
                answer=body.validated_data["answer"],
                actor=request.user,
                context=_context_from(request),
                comments=body.validated_data.get("comments"),
            )
        except AnswerValidationError as error:
            return Response(
                {
                    "detail": str(error),
                    "code": error.code,
                    "field_errors": {"answer": [str(error)]},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        except PlanNotEditable as error:
            return Response(
                {"detail": str(error), "code": "plan_not_editable", "field_errors": {}},
                status=http_status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "question_id": question.pk,
                "answer": row.answer_json,
                "answered_by": row.respondent_user.display_name,
                "answered_at": row.updated_at,
                "sections": _section_payload(summaries),
            }
        )

    @extend_schema(summary="Clear one answer")
    def delete(self, request, *args, **kwargs):
        version = self.get_plan_version()
        if (refused := self.require_author(version)) is not None:
            return refused
        try:
            summaries = clear_answer(
                version=version,
                question=self.get_question(),
                actor=request.user,
                context=_context_from(request),
            )
        except PlanNotEditable as error:
            return Response(
                {"detail": str(error), "code": "plan_not_editable", "field_errors": {}},
                status=http_status.HTTP_409_CONFLICT,
            )
        return Response({"sections": _section_payload(summaries)})


class CommentSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = QuestionComment
        fields = ["question_comment_id", "question_id", "comment", "author_name", "created_at"]
        read_only_fields = ["question_comment_id", "question_id", "created_at"]

    def get_author_name(self, comment) -> str:
        return comment.author.display_name if comment.author_id else "System"

    def validate_comment(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A comment cannot be empty.")
        return value


class CommentListView(PlanVersionScopedMixin, APIView):
    """Per-question comment threads (4.5).

    Anyone who can see the plan can comment — review conversation is not
    authoring. Comments are never edited or deleted; they are the record of a
    review cycle, and stay with the version they were made on (they are not
    copied forward, see `apps.plans.versioning`).
    """

    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        version = self.get_plan_version()
        comments = (
            QuestionComment.objects.filter(
                plan_version=version, question_id=self.kwargs["question_id"]
            )
            .select_related("author")
            .order_by("created_at")
        )
        return Response(CommentSerializer(comments, many=True).data)

    @extend_schema(request=CommentSerializer, responses={201: CommentSerializer})
    def post(self, request, *args, **kwargs):
        version = self.get_plan_version()
        question = get_object_or_404(Question, pk=self.kwargs["question_id"])
        body = CommentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        comment = QuestionComment.objects.create(
            plan_version=version,
            question=question,
            author=request.user,
            comment=body.validated_data["comment"],
        )
        return Response(CommentSerializer(comment).data, status=http_status.HTTP_201_CREATED)
