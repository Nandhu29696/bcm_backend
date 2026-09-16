from django.apps import AppConfig


class CrisisConfig(AppConfig):
    """Crisis events and the CMSC roster."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crisis"
    label = "crisis"
