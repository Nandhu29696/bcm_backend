"""Dashboard and report routes (Phase 9)."""

from django.urls import path

from apps.reporting import views

app_name = "reporting"

urlpatterns = [
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("reports/types/", views.ReportTypesView.as_view(), name="report-types"),
    path("reports/requests/", views.ReportRequestListView.as_view(), name="report-requests"),
    path(
        "reports/requests/<int:report_request_id>/",
        views.ReportRequestDetailView.as_view(),
        name="report-request-detail",
    ),
    path(
        "reports/requests/<int:report_request_id>/run/",
        views.ReportRequestDetailView.as_view(action_name="run"),
        name="report-request-run",
    ),
    path(
        "reports/requests/<int:report_request_id>/stop/",
        views.ReportRequestDetailView.as_view(action_name="stop"),
        name="report-request-stop",
    ),
    path(
        "reports/estate-detail/",
        views.EstateDetailReportView.as_view(),
        name="estate-detail-report",
    ),
]
