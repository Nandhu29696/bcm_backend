from django.apps import AppConfig


class RiskConfig(AppConfig):
    """Risk register, scoring, mitigation/contingency actions, recovery strategy."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.risk"
    label = "risk"
