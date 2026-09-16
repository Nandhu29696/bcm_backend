from django.apps import AppConfig


class TestingConfig(AppConfig):
    """Test scheduling and outcomes."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.testing"
    label = "testing"
