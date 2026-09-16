from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    """Upload, dedupe, polymorphic attachment, generated exports."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.documents"
    label = "documents"
