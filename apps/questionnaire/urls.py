"""Plan editor routes (journey step 5)."""

from django.urls import path

from apps.questionnaire import evidence, views

app_name = "questionnaire"

urlpatterns = [
    path(
        "plan-versions/<int:plan_version_id>/questionnaire/",
        views.QuestionnaireView.as_view(),
        name="questionnaire",
    ),
    path(
        "plan-versions/<int:plan_version_id>/answers/<int:question_id>/",
        views.AnswerView.as_view(),
        name="answer",
    ),
    path(
        "plan-versions/<int:plan_version_id>/questions/<int:question_id>/evidence/",
        evidence.QuestionEvidenceView.as_view(),
        name="question-evidence",
    ),
    path(
        "plan-versions/<int:plan_version_id>/questions/<int:question_id>/evidence/"
        "<int:entity_document_id>/",
        evidence.QuestionEvidenceDetailView.as_view(),
        name="question-evidence-detail",
    ),
    path(
        "plan-versions/<int:plan_version_id>/questions/<int:question_id>/comments/",
        views.CommentListView.as_view(),
        name="question-comments",
    ),
]
