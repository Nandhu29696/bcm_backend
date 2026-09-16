"""
SSO sign-in (Phase 1.5-1.7).

The provider's token endpoint is stubbed. A live OAuth round-trip needs real
Google/Entra credentials and a browser, so these tests cover everything up to and
including the code exchange, and the exchange itself is verified by asserting the
exact request we send.
"""

import base64
import json
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Employee, UserAccount
from apps.accounts.scoping import ScopeResolver
from apps.accounts.sso import SsoError, consume_state, issue_state
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db

GOOGLE_SETTINGS = {
    "GOOGLE_OAUTH_CLIENT_ID": "test-client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_OAUTH_REDIRECT_URI": "http://localhost:5173/auth/google/callback",
}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def make_id_token(claims: dict) -> str:
    """A JWT-shaped string. Only the payload segment is read (see sso.py)."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeTokenResponse:
    status_code = 200

    def __init__(self, claims):
        self._claims = claims

    def json(self):
        return {"access_token": "at", "id_token": make_id_token(self._claims)}


def google_claims(**overrides):
    return {
        "sub": "google-subject-001",
        "email": "person@example.com",
        "email_verified": True,
        "name": "Test Person",
        **overrides,
    }


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def test_state_round_trips():
    state = issue_state("google")
    consume_state(state, "google")  # must not raise


def test_state_is_single_use():
    state = issue_state("google")
    consume_state(state, "google")
    with pytest.raises(SsoError) as exc:
        consume_state(state, "google")
    assert exc.value.code == "sso_state_expired"


def test_state_is_bound_to_its_provider():
    """A state issued for Google must not complete a Microsoft callback."""
    state = issue_state("google")
    with pytest.raises(SsoError) as exc:
        consume_state(state, "microsoft")
    assert exc.value.code == "sso_state_mismatch"


def test_unknown_state_is_rejected():
    with pytest.raises(SsoError):
        consume_state("never-issued", "google")


# --------------------------------------------------------------------------- #
# provider listing and authorize
# --------------------------------------------------------------------------- #


def test_providers_endpoint_reports_configuration(api_client):
    with override_settings(**GOOGLE_SETTINGS, MS_ENTRA_CLIENT_ID="", MS_ENTRA_CLIENT_SECRET=""):
        body = api_client.get(reverse("accounts:auth:sso-providers")).json()

    by_name = {p["name"]: p["configured"] for p in body["providers"]}
    assert by_name["google"] is True
    assert by_name["microsoft"] is False


@override_settings(**GOOGLE_SETTINGS)
def test_authorize_returns_a_provider_url(api_client):
    response = api_client.get(reverse("accounts:auth:sso-authorize", kwargs={"provider": "google"}))
    assert response.status_code == 200
    url = response.json()["authorize_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=test-client-id" in url
    assert f"state={response.json()['state']}" in url


def test_authorize_for_unconfigured_provider_is_refused(api_client):
    with override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET=""):
        response = api_client.get(
            reverse("accounts:auth:sso-authorize", kwargs={"provider": "google"})
        )
    assert response.status_code == 400
    assert response.json()["code"] == "sso_not_configured"


def test_authorize_for_unknown_provider_is_refused(api_client):
    response = api_client.get(
        reverse("accounts:auth:sso-authorize", kwargs={"provider": "facebook"})
    )
    assert response.status_code == 400
    assert response.json()["code"] == "sso_unknown_provider"


# --------------------------------------------------------------------------- #
# callback
# --------------------------------------------------------------------------- #


def callback(api_client, provider="google", code="auth-code", state=None):
    return api_client.post(
        reverse("accounts:auth:sso-callback", kwargs={"provider": provider}),
        {"code": code, "state": state or issue_state(provider)},
        format="json",
    )


@override_settings(**GOOGLE_SETTINGS)
def test_callback_creates_a_pending_account_when_no_employee_matches(api_client):
    """Plan task 1.5: an unknown identity logs in, PENDING, with no roles.

    Refusing the sign-in would present a provisioning gap as a credentials
    failure, which is far more expensive to diagnose.
    """
    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())):
        response = callback(api_client)

    assert response.status_code == 200
    body = response.json()
    assert body["new_account"] is True
    assert body["user_status"] == UserAccount.Status.PENDING
    assert "access" in body

    user = UserAccount.objects.get(email="person@example.com")
    assert user.employee is None
    assert user.role_codes == set()
    assert user.auth_provider == "google"
    assert user.provider_subject == "google-subject-001"


@override_settings(**GOOGLE_SETTINGS)
def test_pending_sso_user_resolves_to_empty_scope_not_an_error(api_client):
    """Exit criterion: such a user must see an empty dashboard, never a 500."""
    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())):
        tokens = callback(api_client).json()

    user = UserAccount.objects.get(email="person@example.com")
    scope = ScopeResolver(user)
    assert scope.estate_ids == frozenset()
    assert scope.sees_all_estates is False

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    me = api_client.get(reverse("accounts:auth:me"))
    assert me.status_code == 200
    assert me.json()["user_status"] == UserAccount.Status.PENDING
    assert me.json()["estate_ids"] == []


@override_settings(**GOOGLE_SETTINGS)
def test_callback_links_a_matching_employee_and_activates(api_client):
    Employee.objects.create(
        employee_number="E-100", full_name="Test Person", email="person@example.com"
    )

    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())):
        response = callback(api_client)

    assert response.json()["user_status"] == UserAccount.Status.ACTIVE
    user = UserAccount.objects.get(email="person@example.com")
    assert user.employee.employee_number == "E-100"


@override_settings(**GOOGLE_SETTINGS)
def test_second_sign_in_reuses_the_account(api_client):
    stub = FakeTokenResponse(google_claims())
    with patch("apps.accounts.sso.requests.post", return_value=stub):
        first = callback(api_client)
        second = callback(api_client)

    assert first.json()["new_account"] is True
    assert second.json()["new_account"] is False
    assert UserAccount.objects.filter(email="person@example.com").count() == 1


@override_settings(**GOOGLE_SETTINGS)
def test_existing_local_account_is_bound_to_the_sso_identity(api_client, user_factory):
    user_factory(email="person@example.com", display_name="Existing")

    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())):
        response = callback(api_client)

    assert response.json()["new_account"] is False
    user = UserAccount.objects.get(email="person@example.com")
    assert user.auth_provider == "google"
    assert user.provider_subject == "google-subject-001"


@override_settings(**GOOGLE_SETTINGS)
def test_unverified_email_is_rejected(api_client):
    claims = google_claims(email_verified=False)
    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(claims)):
        response = callback(api_client)

    assert response.status_code == 401
    assert response.json()["code"] == "sso_email_unverified"
    assert not UserAccount.objects.filter(email="person@example.com").exists()


@override_settings(**GOOGLE_SETTINGS)
def test_reused_state_is_rejected(api_client):
    state = issue_state("google")
    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())):
        assert callback(api_client, state=state).status_code == 200
        replay = callback(api_client, state=state)

    assert replay.status_code == 401
    assert replay.json()["code"] == "sso_state_expired"


@override_settings(**GOOGLE_SETTINGS)
def test_provider_rejection_is_surfaced_not_swallowed(api_client):
    class Rejected:
        status_code = 400

        def json(self):
            return {"error": "invalid_grant"}

    with patch("apps.accounts.sso.requests.post", return_value=Rejected()):
        response = callback(api_client)

    assert response.status_code == 401
    assert response.json()["code"] == "sso_token_rejected"
    assert AuditLog.objects.filter(action=AuditLog.Action.LOGIN_FAILURE).exists()


@override_settings(**GOOGLE_SETTINGS)
def test_successful_sso_login_is_audited(api_client):
    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())):
        callback(api_client)

    entry = AuditLog.objects.filter(action=AuditLog.Action.LOGIN_SUCCESS).first()
    assert entry is not None
    assert entry.detail["provider"] == "google"


@override_settings(
    MS_ENTRA_CLIENT_ID="entra-id",
    MS_ENTRA_CLIENT_SECRET="entra-secret",
    MS_ENTRA_TENANT_ID="tenant-abc",
)
def test_microsoft_uses_the_tenant_endpoint_and_oid_claim(api_client):
    """Entra returns `oid` rather than `sub`, and omits email_verified."""
    claims = {
        "oid": "entra-object-id-9",
        "preferred_username": "worker@corp.example.com",
        "name": "Corp Worker",
    }
    with patch("apps.accounts.sso.requests.post", return_value=FakeTokenResponse(claims)) as mocked:
        response = callback(api_client, provider="microsoft")

    assert response.status_code == 200
    called_url = mocked.call_args[0][0]
    assert "login.microsoftonline.com/tenant-abc/oauth2/v2.0/token" in called_url

    user = UserAccount.objects.get(email="worker@corp.example.com")
    assert user.auth_provider == "microsoft"
    assert user.provider_subject == "entra-object-id-9"


@override_settings(**GOOGLE_SETTINGS)
def test_code_exchange_sends_the_expected_parameters(api_client):
    with patch(
        "apps.accounts.sso.requests.post", return_value=FakeTokenResponse(google_claims())
    ) as mocked:
        callback(api_client, code="the-auth-code")

    sent = mocked.call_args.kwargs["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-auth-code"
    assert sent["client_id"] == "test-client-id"
    assert sent["client_secret"] == "test-client-secret"
    assert sent["redirect_uri"] == GOOGLE_SETTINGS["GOOGLE_OAUTH_REDIRECT_URI"]
