"""Cost code action and plan version routes (journey step 4)."""

from django.urls import path

from apps.plans import review_views, views

app_name = "plans"

urlpatterns = [
    path(
        "cost-codes/<int:cost_code_id>/",
        views.CostCodeDetailView.as_view(),
        name="cost-code-detail",
    ),
    path(
        "cost-codes/<int:cost_code_id>/plan-versions/",
        views.CostCodePlanVersionsView.as_view(),
        name="cost-code-plan-versions",
    ),
    path(
        "plan-versions/<int:plan_version_id>/",
        views.PlanVersionDetailView.as_view(),
        name="plan-version-detail",
    ),
    path(
        "plan-versions/<int:plan_version_id>/overview/",
        views.PlanVersionOverviewView.as_view(),
        name="plan-version-overview",
    ),
    # A verb on a subresource, not a PATCH of status or version_number (AD-10).
    path(
        "plan-versions/<int:plan_version_id>/copy/",
        views.PlanVersionCopyView.as_view(),
        name="plan-version-copy",
    ),
    # Phase 6: transitions as verbs on the version, never a PATCH of status.
    path(
        "plan-versions/<int:plan_version_id>/readiness/",
        review_views.ReadinessView.as_view(),
        name="plan-version-readiness",
    ),
    path(
        "plan-versions/<int:plan_version_id>/submit/",
        review_views.SubmitView.as_view(),
        name="plan-version-submit",
    ),
    path(
        "plan-versions/<int:plan_version_id>/approve/",
        review_views.ApproveView.as_view(),
        name="plan-version-approve",
    ),
    path(
        "plan-versions/<int:plan_version_id>/rework/",
        review_views.ReworkView.as_view(),
        name="plan-version-rework",
    ),
    path("review-queue/", review_views.ReviewQueueView.as_view(), name="review-queue"),
    path(
        "plan-versions/<int:plan_version_id>/history/",
        views.PlanVersionHistoryView.as_view(),
        name="plan-version-history",
    ),
    path(
        "plan-versions/<int:plan_version_id>/coordinators/",
        views.CoordinatorAssignmentListView.as_view(),
        name="plan-version-coordinators",
    ),
    path(
        "plan-versions/<int:plan_version_id>/coordinators/<int:assignment_id>/",
        views.CoordinatorAssignmentDetailView.as_view(),
        name="plan-version-coordinator-detail",
    ),
]
