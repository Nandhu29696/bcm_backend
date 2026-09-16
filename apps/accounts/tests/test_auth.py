"""Local login, OTP, token lifecycle, password reset and lockout."""

import pytest
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import OtpChallenge, UserAccount
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db

PASSWORD = "test-pass-123"


@pytest.fixture(autouse=True)
def _clear_throttle_state():
    """Lockout counters live in the cache and leak between tests otherwise."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def local_user(user_factory):
    return user_factory(email="local@example.com", display_name="Local User")


def login(client, email=..., password=PASSWORD):
    return client.post(
        reverse("accounts:auth:login"),
        {"email": "local@example.com" if email is ... else email, "password": password},
        format="json",
    )


# --------------------------------------------------------------------------- #
# Password step
# --------------------------------------------------------------------------- #


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_login_without_otp_returns_tokens(api_client, local_user):
    response = login(api_client)
    assert response.status_code == 200
    assert response.json()["otp_required"] is False
    assert "access" in response.json()
    assert "refresh" in response.json()


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_login_with_wrong_password_is_401(api_client, local_user):
    response = login(api_client, password="wrong")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_login_response_does_not_reveal_whether_account_exists(api_client, local_user):
    """Unknown account and wrong password must be indistinguishable."""
    unknown = login(api_client, email="nobody@example.com", password="whatever")
    wrong = login(api_client, password="wrong")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_login_is_audited(api_client, local_user):
    login(api_client)
    assert AuditLog.objects.filter(action=AuditLog.Action.LOGIN_SUCCESS, actor=local_user).exists()


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_failed_login_is_audited(api_client, local_user):
    login(api_client, password="wrong")
    assert AuditLog.objects.filter(action=AuditLog.Action.LOGIN_FAILURE).exists()


# --------------------------------------------------------------------------- #
# Lockout
# --------------------------------------------------------------------------- #


@override_settings(REQUIRE_OTP_FOR_LOGIN=False, LOGIN_MAX_FAILED_ATTEMPTS=3)
def test_repeated_failures_lock_the_account(api_client, local_user):
    for _ in range(3):
        assert login(api_client, password="wrong").status_code == 401

    # The correct password is now refused too — that is the point of a lockout.
    locked = login(api_client)
    assert locked.status_code == 429
    assert locked.json()["code"] == "account_locked"
    assert AuditLog.objects.filter(action=AuditLog.Action.LOGIN_LOCKOUT).exists()


@override_settings(REQUIRE_OTP_FOR_LOGIN=False, LOGIN_MAX_FAILED_ATTEMPTS=3)
def test_successful_login_clears_the_failure_counter(api_client, local_user):
    login(api_client, password="wrong")
    login(api_client, password="wrong")
    assert login(api_client).status_code == 200

    # Counter reset, so two more failures must not trip the lockout.
    login(api_client, password="wrong")
    login(api_client, password="wrong")
    assert login(api_client).status_code == 200


# --------------------------------------------------------------------------- #
# OTP second factor
# --------------------------------------------------------------------------- #


@override_settings(REQUIRE_OTP_FOR_LOGIN=True)
def test_login_with_otp_emails_a_code_and_withholds_tokens(api_client, local_user):
    mail.outbox.clear()
    response = login(api_client)

    assert response.status_code == 200
    assert response.json()["otp_required"] is True
    assert "access" not in response.json()
    assert len(mail.outbox) == 1
    assert OtpChallenge.objects.filter(user=local_user, consumed_at=None).count() == 1


def _extract_code(message) -> str:
    for token in message.body.split():
        if token.isdigit() and len(token) == 6:
            return token
    raise AssertionError(f"No 6-digit code in email:\n{message.body}")


@override_settings(REQUIRE_OTP_FOR_LOGIN=True)
def test_valid_otp_returns_tokens(api_client, local_user):
    mail.outbox.clear()
    login(api_client)
    code = _extract_code(mail.outbox[0])

    response = api_client.post(
        reverse("accounts:auth:otp-verify"),
        {"email": local_user.email, "code": code},
        format="json",
    )
    assert response.status_code == 200
    assert "access" in response.json()


@override_settings(REQUIRE_OTP_FOR_LOGIN=True)
def test_otp_is_single_use(api_client, local_user):
    mail.outbox.clear()
    login(api_client)
    code = _extract_code(mail.outbox[0])
    url = reverse("accounts:auth:otp-verify")

    assert (
        api_client.post(url, {"email": local_user.email, "code": code}, format="json").status_code
        == 200
    )

    replay = api_client.post(url, {"email": local_user.email, "code": code}, format="json")
    assert replay.status_code == 400
    assert replay.json()["code"] == "otp_not_found"


@override_settings(REQUIRE_OTP_FOR_LOGIN=True, OTP_MAX_ATTEMPTS=2)
def test_otp_attempts_are_limited(api_client, local_user):
    login(api_client)
    url = reverse("accounts:auth:otp-verify")

    first = api_client.post(url, {"email": local_user.email, "code": "000000"}, format="json")
    assert first.json()["code"] == "otp_invalid"

    second = api_client.post(url, {"email": local_user.email, "code": "000000"}, format="json")
    assert second.json()["code"] == "otp_invalid"

    # The challenge is burned, so even the right code is now useless.
    third = api_client.post(url, {"email": local_user.email, "code": "000000"}, format="json")
    assert third.json()["code"] in {"otp_attempts", "otp_not_found"}


@override_settings(REQUIRE_OTP_FOR_LOGIN=True)
def test_otp_cannot_be_obtained_without_the_password(api_client, local_user):
    """The challenge only exists after the password step."""
    response = api_client.post(
        reverse("accounts:auth:otp-verify"),
        {"email": local_user.email, "code": "123456"},
        format="json",
    )
    assert response.status_code == 400
    assert not OtpChallenge.objects.exists()


@override_settings(REQUIRE_OTP_FOR_LOGIN=True, OTP_RESEND_COOLDOWN_SECONDS=300)
def test_otp_resend_is_rate_limited(api_client, local_user):
    login(api_client)
    response = api_client.post(
        reverse("accounts:auth:otp-resend"), {"email": local_user.email}, format="json"
    )
    assert response.status_code == 429
    assert response.json()["code"] == "otp_cooldown"


def test_otp_resend_does_not_reveal_unknown_accounts(api_client):
    response = api_client.post(
        reverse("accounts:auth:otp-resend"), {"email": "nobody@example.com"}, format="json"
    )
    assert response.status_code == 200
    assert "If that account exists" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_refresh_rotation_invalidates_the_old_token(api_client, local_user):
    tokens = login(api_client).json()
    url = reverse("accounts:auth:refresh")

    rotated = api_client.post(url, {"refresh": tokens["refresh"]}, format="json")
    assert rotated.status_code == 200
    assert rotated.json()["refresh"] != tokens["refresh"]

    # The original is blacklisted and must not work a second time.
    replay = api_client.post(url, {"refresh": tokens["refresh"]}, format="json")
    assert replay.status_code == 401


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_logout_revokes_the_refresh_token(api_client, local_user):
    tokens = login(api_client).json()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    assert (
        api_client.post(
            reverse("accounts:auth:logout"), {"refresh": tokens["refresh"]}, format="json"
        ).status_code
        == 204
    )

    api_client.credentials()
    replay = api_client.post(
        reverse("accounts:auth:refresh"), {"refresh": tokens["refresh"]}, format="json"
    )
    assert replay.status_code == 401


@override_settings(REQUIRE_OTP_FOR_LOGIN=False)
def test_me_returns_roles_and_scope(api_client, user_factory):
    user = user_factory(email="scoped@example.com", roles=["BCM_COORDINATOR"])
    api_client.force_authenticate(user=user)

    body = api_client.get(reverse("accounts:auth:me")).json()
    assert body["email"] == "scoped@example.com"
    assert body["role_codes"] == ["BCM_COORDINATOR"]
    assert body["sees_all_estates"] is False


def test_me_requires_authentication(api_client):
    assert api_client.get(reverse("accounts:auth:me")).status_code == 401


# --------------------------------------------------------------------------- #
# Registration and password reset
# --------------------------------------------------------------------------- #


def test_registration_creates_a_pending_account_with_no_roles(api_client):
    response = api_client.post(
        reverse("accounts:auth:register"),
        {
            "email": "newcomer@example.com",
            "display_name": "New Comer",
            "password": "a-strong-password-42",
        },
        format="json",
    )
    assert response.status_code == 201

    user = UserAccount.objects.get(email="newcomer@example.com")
    assert user.user_status == UserAccount.Status.PENDING
    assert user.role_codes == set()


def test_registration_rejects_a_weak_password(api_client):
    response = api_client.post(
        reverse("accounts:auth:register"),
        {"email": "weak@example.com", "display_name": "Weak", "password": "123"},
        format="json",
    )
    assert response.status_code == 400
    assert "password" in response.json()["field_errors"]


def test_password_reset_sends_a_link_and_accepts_the_token(api_client, local_user):
    mail.outbox.clear()
    assert (
        api_client.post(
            reverse("accounts:auth:password-reset"),
            {"email": local_user.email},
            format="json",
        ).status_code
        == 200
    )
    assert len(mail.outbox) == 1

    body = mail.outbox[0].body
    query = body.split("reset-password?")[1].split()[0]
    params = dict(pair.split("=") for pair in query.split("&"))

    response = api_client.post(
        reverse("accounts:auth:password-reset-confirm"),
        {
            "uid": params["uid"],
            "token": params["token"],
            "new_password": "a-brand-new-password-9",
        },
        format="json",
    )
    assert response.status_code == 200

    local_user.refresh_from_db()
    assert local_user.check_password("a-brand-new-password-9")


def test_reset_email_url_is_not_html_escaped(api_client, local_user):
    """Regression: plain-text email must not go through the autoescaping engine.

    With autoescaping on, the "&" between uid and token renders as "&amp;", the
    token parameter never reaches the frontend, and password reset fails for
    every user. Notification templates render with using="text_email".
    """
    mail.outbox.clear()
    api_client.post(
        reverse("accounts:auth:password-reset"), {"email": local_user.email}, format="json"
    )
    body = mail.outbox[0].body
    assert "&amp;" not in body
    assert "&token=" in body


def test_password_reset_for_unknown_email_looks_identical(api_client):
    response = api_client.post(
        reverse("accounts:auth:password-reset"),
        {"email": "nobody@example.com"},
        format="json",
    )
    assert response.status_code == 200
    assert len(mail.outbox) == 0


def test_password_reset_rejects_a_tampered_token(api_client, local_user):
    response = api_client.post(
        reverse("accounts:auth:password-reset-confirm"),
        {"uid": "bogus", "token": "bogus", "new_password": "another-password-42"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "reset_invalid"


def test_password_change_requires_the_current_password(api_client, local_user):
    api_client.force_authenticate(user=local_user)
    response = api_client.post(
        reverse("accounts:auth:password-change"),
        {"current_password": "wrong", "new_password": "yet-another-password-7"},
        format="json",
    )
    assert response.status_code == 400
    assert "current_password" in response.json()["field_errors"]
