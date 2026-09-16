"""
Google and Microsoft Entra ID sign-in (Phase 1.6, 1.7).

The OAuth 2.0 authorization-code flow is implemented directly rather than through
social-auth's pipeline. That pipeline is built around server-rendered sessions
and redirects; this is a JWT API where the SPA holds the redirect and posts the
code back, so the direct implementation is both shorter and easier to test.

`social_django` stays installed for its `UserSocialAuth` table, which is useful
if we later need to record several identities against one account.

Security notes:
  * `state` is generated server-side, stored in the cache and consumed once.
    Without that the callback is open to CSRF — an attacker can feed a victim a
    code and have it bound to the attacker's session.
  * The ID token is read without signature verification, which is sound *only*
    because it arrives directly from the provider's token endpoint over TLS in
    response to our authenticated request (OIDC Core 3.1.3.7 case 6). An ID token
    received by any other route MUST be verified.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

STATE_PREFIX = "bcm:oauth:state:"
REQUEST_TIMEOUT = 15


class SsoError(Exception):
    def __init__(self, message: str, code: str = "sso_error"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class SsoIdentity:
    subject: str
    email: str
    display_name: str
    email_verified: bool


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    authorize_url: str
    token_url: str
    scope: str
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def google_provider() -> OAuthProvider:
    return OAuthProvider(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="openid email profile",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
    )


def microsoft_provider() -> OAuthProvider:
    tenant = settings.MS_ENTRA_TENANT_ID or "common"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return OAuthProvider(
        name="microsoft",
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        scope="openid email profile User.Read",
        client_id=settings.MS_ENTRA_CLIENT_ID,
        client_secret=settings.MS_ENTRA_CLIENT_SECRET,
        redirect_uri=settings.MS_ENTRA_REDIRECT_URI,
    )


PROVIDERS = {"google": google_provider, "microsoft": microsoft_provider}


def get_provider(name: str) -> OAuthProvider:
    factory = PROVIDERS.get(name)
    if factory is None:
        raise SsoError(f"Unknown SSO provider '{name}'.", code="sso_unknown_provider")
    provider = factory()
    if not provider.configured:
        raise SsoError(
            f"{name.title()} sign-in is not configured on this server.",
            code="sso_not_configured",
        )
    return provider


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def issue_state(provider_name: str) -> str:
    state = secrets.token_urlsafe(32)
    cache.set(f"{STATE_PREFIX}{state}", provider_name, timeout=settings.OAUTH_STATE_TTL_SECONDS)
    return state


def consume_state(state: str, provider_name: str) -> None:
    """Validate and burn a state value. Raises if it does not match."""
    key = f"{STATE_PREFIX}{state}"
    stored = cache.get(key)
    cache.delete(key)
    if stored is None:
        raise SsoError("That sign-in attempt expired. Please try again.", code="sso_state_expired")
    if stored != provider_name:
        raise SsoError("Invalid sign-in state.", code="sso_state_mismatch")


def build_authorize_url(provider: OAuthProvider, state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        # Ask for a fresh account choice rather than silently reusing whichever
        # account the browser last used.
        "prompt": "select_account",
    }
    if provider.name == "google":
        params["access_type"] = "offline"
    return f"{provider.authorize_url}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# code exchange
# --------------------------------------------------------------------------- #


def exchange_code(provider: OAuthProvider, code: str) -> dict:
    try:
        response = requests.post(
            provider.token_url,
            data={
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": provider.redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SsoError("Could not reach the identity provider.", code="sso_unreachable") from exc

    if response.status_code != 200:
        raise SsoError("The identity provider rejected that sign-in.", code="sso_token_rejected")
    return response.json()


def decode_id_token(id_token: str) -> dict:
    """Read the ID token payload. See the module docstring on verification."""
    try:
        payload_b64 = id_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (IndexError, ValueError, binascii.Error) as exc:
        raise SsoError("Malformed identity token.", code="sso_bad_token") from exc


def identity_from_token_response(provider: OAuthProvider, payload: dict) -> SsoIdentity:
    id_token = payload.get("id_token")
    if not id_token:
        raise SsoError("The provider returned no identity token.", code="sso_no_id_token")

    claims = decode_id_token(id_token)

    subject = claims.get("sub") or claims.get("oid") or ""
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    name = claims.get("name") or ""

    # Google states email_verified explicitly. Entra does not for work accounts,
    # where the tenant owns the address and it is verified by definition.
    if provider.name == "google":
        email_verified = bool(claims.get("email_verified"))
    else:
        email_verified = bool(email)

    if not subject:
        raise SsoError("The provider returned no subject claim.", code="sso_no_subject")

    return SsoIdentity(
        subject=subject,
        email=email,
        display_name=name or (email.split("@")[0] if email else "User"),
        email_verified=email_verified,
    )


def complete_sso_login(provider_name: str, code: str, state: str) -> SsoIdentity:
    """Full callback handling: validate state, exchange the code, read the identity."""
    provider = get_provider(provider_name)
    consume_state(state, provider_name)
    token_payload = exchange_code(provider, code)
    identity = identity_from_token_response(provider, token_payload)

    if not identity.email_verified:
        raise SsoError(
            "Your identity provider has not verified that email address.",
            code="sso_email_unverified",
        )
    return identity
