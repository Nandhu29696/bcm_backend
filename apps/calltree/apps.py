from django.apps import AppConfig


class CalltreeConfig(AppConfig):
    """Call tree runs, members, per-channel attempt tracking."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.calltree"
    label = "calltree"
