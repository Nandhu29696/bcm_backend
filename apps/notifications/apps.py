from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """Email templates, dispatch, delivery log."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"
