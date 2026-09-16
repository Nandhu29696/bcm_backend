from django.apps import AppConfig


class ExemptionsConfig(AppConfig):
    """Exemption requests and approval trail."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.exemptions"
    label = "exemptions"
