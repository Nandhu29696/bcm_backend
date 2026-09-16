from django.apps import AppConfig


class ReportingConfig(AppConfig):
    """Dashboards, the estate detail report and scheduled exports (Phase 9)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reporting"
    label = "reporting"
