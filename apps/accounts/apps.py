from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Authentication, JWT, SSO, OTP, RBAC, employees and user accounts."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
