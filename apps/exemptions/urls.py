"""Exemption routes (Phase 7.5)."""

from django.urls import path

from apps.exemptions import views

app_name = "exemptions"

urlpatterns = [
    path(
        "plan-versions/<int:plan_version_id>/exemptions/",
        views.VersionExemptionsView.as_view(),
        name="version-exemptions",
    ),
    path(
        "exemptions/<int:exemption_id>/",
        views.ExemptionDetailView.as_view(),
        name="exemption-detail",
    ),
    path(
        "exemptions/<int:exemption_id>/approve/",
        views.ExemptionDecisionView.as_view(decision="approve"),
        name="exemption-approve",
    ),
    path(
        "exemptions/<int:exemption_id>/reject/",
        views.ExemptionDecisionView.as_view(decision="reject"),
        name="exemption-reject",
    ),
    path(
        "exemptions/<int:exemption_id>/rework/",
        views.ExemptionDecisionView.as_view(decision="rework"),
        name="exemption-rework",
    ),
    path(
        "exemptions/<int:exemption_id>/resubmit/",
        views.ExemptionResubmitView.as_view(),
        name="exemption-resubmit",
    ),
]
