"""Shared pytest fixtures."""

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import Role, UserRole


@pytest.fixture
def user_factory(db):
    """Create a UserAccount, optionally with role codes attached."""

    def _create(email="user@example.com", *, roles=(), **extra):
        user = get_user_model().objects.create_user(
            email=email,
            password="test-pass-123",
            display_name=extra.pop("display_name", email.split("@")[0]),
            **extra,
        )
        for code in roles:
            role, _ = Role.objects.get_or_create(
                role_code=code, defaults={"role_name": code.replace("_", " ").title()}
            )
            UserRole.objects.create(user=user, role=role)
        return user

    return _create


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def authenticated_client(api_client, user_factory):
    def _authenticate(**kwargs):
        user = user_factory(**kwargs)
        api_client.force_authenticate(user=user)
        return api_client, user

    return _authenticate
