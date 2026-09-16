from django.apps import AppConfig


class OrganizationConfig(AppConfig):
    """Master data: regions, estates, locations, centers, processes, cost codes."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.organization"
    label = "organization"
