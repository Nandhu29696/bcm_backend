"""
Phase 0 smoke tests.

These prove the foundation holds: the test database really is MySQL, the custom
user model is wired, soft delete behaves, and the health endpoints respond. They
are cheap and they fail loudly if someone breaks the base setup.
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse

from apps.organization.models import Estate


def test_test_database_is_mysql():
    """SQLite would pass tests that production fails (ARCHITECTURE.md §8)."""
    assert connection.vendor == "mysql", f"Tests must run against MySQL, got {connection.vendor!r}."


@pytest.mark.django_db
def test_test_database_uses_utf8mb4():
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@character_set_database, @@collation_database")
        charset, collation = cursor.fetchone()
    assert charset == "utf8mb4"
    assert collation.startswith("utf8mb4")


def test_auth_user_model_is_user_account():
    """AD-2. If this ever fails, the fix is not in this test."""
    model = get_user_model()
    assert model._meta.label == "accounts.UserAccount"
    assert model.USERNAME_FIELD == "email"
    assert model._meta.db_table == "user_accounts"


@pytest.mark.django_db
def test_create_user_hashes_password(user_factory):
    user = user_factory(email="hash@example.com")
    assert user.password != "test-pass-123"
    assert user.check_password("test-pass-123")


@pytest.mark.django_db
def test_sso_user_has_no_usable_password():
    user = get_user_model().objects.create_user(
        email="sso@example.com", password=None, display_name="SSO User"
    )
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_role_codes_property(user_factory):
    user = user_factory(email="roles@example.com", roles=["BCM_COORDINATOR"])
    assert user.role_codes == {"BCM_COORDINATOR"}


@pytest.mark.django_db
def test_soft_delete_hides_from_default_manager():
    """AD-6: `objects` hides inactive rows, `all_objects` still sees them."""
    estate = Estate.objects.create(estate_name="Temp Estate")
    assert Estate.objects.filter(pk=estate.pk).exists()

    estate.soft_delete()

    assert not Estate.objects.filter(pk=estate.pk).exists()
    assert Estate.all_objects.filter(pk=estate.pk).exists()

    estate.restore()
    assert Estate.objects.filter(pk=estate.pk).exists()


@pytest.mark.django_db
def test_health_endpoint_is_public(api_client):
    response = api_client.get(reverse("core:health"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readiness_endpoint_reports_database(api_client):
    response = api_client.get(reverse("core:ready"))
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
