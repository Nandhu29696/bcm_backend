from django.apps import AppConfig


class LookupsConfig(AppConfig):
    """Scored reference catalogue driving risk ratings and BIA dropdowns."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lookups"
    label = "lookups"
