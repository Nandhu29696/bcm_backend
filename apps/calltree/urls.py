"""Call tree routes (Phase 8)."""

from django.urls import path

from apps.calltree import views

app_name = "calltree"

urlpatterns = [
    path(
        "call-tree-runs/<int:call_tree_run_id>/", views.RunDetailView.as_view(), name="run-detail"
    ),
    path(
        "call-tree-runs/<int:call_tree_run_id>/report/",
        views.RunReportView.as_view(),
        name="run-report",
    ),
    path(
        "cost-codes/<int:cost_code_id>/call-tree-runs/",
        views.CostCodeRunsView.as_view(),
        name="cost-code-runs",
    ),
    path(
        "webhooks/twilio/voice-status/", views.TwilioStatusWebhook.as_view(), name="twilio-status"
    ),
    path("webhooks/twilio/twiml/", views.TwilioTwimlView.as_view(), name="twilio-twiml"),
    path(
        "webhooks/twilio/voice-response/",
        views.TwilioResponseWebhook.as_view(),
        name="twilio-response",
    ),
    path("webhooks/teams/", views.TeamsWebhook.as_view(), name="teams-webhook"),
]
