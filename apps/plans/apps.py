from django.apps import AppConfig


class PlansConfig(AppConfig):
    """Plan lifecycle, versioning, status workflow, coordinator assignment."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.plans"
    label = "plans"
