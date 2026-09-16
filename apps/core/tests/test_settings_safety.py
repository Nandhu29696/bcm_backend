"""
Guards against configuration that is dangerous rather than merely wrong.

These assert properties of the settings modules themselves, so they fail in CI
the moment someone reintroduces a footgun.
"""

import importlib
import sys

import pytest
from django.conf import settings


@pytest.fixture(autouse=True)
def _production_grade_key(monkeypatch):
    """Production refuses a weak key (Phase 10); give it one so the module imports."""
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-only-" + "k" * 60)
    # A developer's .env may legitimately carry these for local testing; the
    # properties asserted here are about the settings modules, not that file.
    monkeypatch.setenv("DEV_ALLOW_REAL_EMAIL", "False")
    monkeypatch.setenv("NOTIFICATION_REDIRECT_TO", "")
    for name in ("production", "staging", "development"):
        sys.modules.pop(f"bcm_backend.settings.{name}", None)


def load_settings_module(name: str):
    return importlib.import_module(f"bcm_backend.settings.{name}")


def test_development_cannot_send_real_email_by_default():
    """Regression: a `.env` with real SMTP credentials must not make dev send mail.

    This happened during Phase 1 verification — an OTP was delivered through
    Gmail from a local run, because development read EMAIL_BACKEND from the
    environment and `.env` named the SMTP backend. Development now forces the
    console backend unless DEV_ALLOW_REAL_EMAIL is explicitly set.
    """
    development = load_settings_module("development")
    assert development.EMAIL_BACKEND == "django.core.mail.backends.console.EmailBackend"


def test_production_refuses_a_notification_redirect(monkeypatch):
    """A redirect left over from a test host would swallow every review request."""
    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setenv("NOTIFICATION_REDIRECT_TO", "inbox@example.com")
    with pytest.raises(ImproperlyConfigured, match="NOTIFICATION_REDIRECT_TO"):
        load_settings_module("production")


def test_test_settings_use_locmem_email():
    """Tests must never touch a network mail server."""
    assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"


def test_call_tree_providers_are_off_in_tests():
    """A misconfigured test run must be incapable of phoning real people."""
    assert settings.TWILIO_ENABLED is False
    assert settings.TEAMS_ENABLED is False


def test_staging_never_dials_real_providers():
    staging = load_settings_module("staging")
    assert staging.TWILIO_ENABLED is False
    assert staging.TEAMS_ENABLED is False


@pytest.mark.parametrize("module_name", ["production", "staging"])
def test_the_second_factor_cannot_be_switched_off_by_environment(module_name):
    """REQUIRE_OTP_FOR_LOGIN is env-driven so E2E runs can disable it.

    That flexibility is only safe because production and staging pin it. A
    leftover `REQUIRE_OTP_FOR_LOGIN=False` in a deployed `.env` would otherwise
    turn off multi-factor login for every user, with nothing in the running
    system looking any different.
    """
    assert load_settings_module(module_name).REQUIRE_OTP_FOR_LOGIN is True


def test_production_has_no_cors_wildcard():
    """CORS_ALLOW_ALL_ORIGINS silently voids the allow-list."""
    production = load_settings_module("production")
    assert getattr(production, "CORS_ALLOW_ALL_ORIGINS", False) is False
    assert getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False) is False


def test_production_locks_down_cookies_and_transport():
    production = load_settings_module("production")
    assert production.DEBUG is False
    assert production.SESSION_COOKIE_SECURE is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SECURE_SSL_REDIRECT is True
    assert production.SECURE_HSTS_SECONDS > 0


def test_jwt_user_id_field_matches_the_user_model():
    """SimpleJWT defaults to `id`; our primary key is `user_id`.

    Getting this wrong fails at token creation with an AttributeError, which is
    exactly what happened the first time Phase 1 ran.
    """
    from django.contrib.auth import get_user_model

    field = settings.SIMPLE_JWT["USER_ID_FIELD"]
    assert field == "user_id"
    get_user_model()._meta.get_field(field)  # raises if it does not exist


def test_text_email_template_engine_does_not_autoescape():
    """Plain-text email must not be HTML-escaped.

    With autoescaping on, "&" in a URL becomes "&amp;" and every multi-parameter
    link in an email breaks — password reset arrives with its token mangled.
    """
    engines = {engine.get("NAME"): engine for engine in settings.TEMPLATES}
    assert "text_email" in engines, "The text_email template engine is missing."
    assert engines["text_email"]["OPTIONS"]["autoescape"] is False


@pytest.mark.parametrize("module_name", ["development", "staging", "production", "test"])
def test_every_settings_module_imports(module_name):
    assert load_settings_module(module_name) is not None
