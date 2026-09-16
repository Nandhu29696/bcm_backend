from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Shared base models, mixins, pagination, exceptions, storage, audit log."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
