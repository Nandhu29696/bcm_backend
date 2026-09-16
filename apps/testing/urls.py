"""Test routes (Phase 8)."""

from django.urls import path

from apps.testing import views

app_name = "testing"

urlpatterns = [
    path("tests/", views.TestListView.as_view(), name="test-list"),
    path(
        "cost-codes/<int:cost_code_id>/tests/",
        views.CostCodeTestsView.as_view(),
        name="cost-code-tests",
    ),
    path("tests/<int:test_id>/", views.TestDetailView.as_view(), name="test-detail"),
    path("tests/<int:test_id>/cancel/", views.TestCancelView.as_view(), name="test-cancel"),
    path(
        "tests/<int:test_id>/start-call-tree/",
        views.TestStartCallTreeView.as_view(),
        name="test-start-call-tree",
    ),
    path("tests/<int:test_id>/outcome/", views.TestOutcomeView.as_view(), name="test-outcome"),
]
