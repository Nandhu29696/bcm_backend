from django.apps import AppConfig


class AssessmentsConfig(AppConfig):
    """BIA authoring: answers, section status, service descriptions, contacts."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assessments"
    label = "assessments"
