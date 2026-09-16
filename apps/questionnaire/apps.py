from django.apps import AppConfig


class QuestionnaireConfig(AppConfig):
    """Question bank: sections, questions, options, conditional visibility."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.questionnaire"
    label = "questionnaire"
