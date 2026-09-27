"""Cookie-based JWT authentication (BUG-21).

Tokens live in HttpOnly cookies instead of being read from an `Authorization`
header, so the browser attaches them to every request automatically --
including cross-site ones. That is exactly the CSRF exposure a bearer token
in a header never had (a header is never sent by the browser on its own), so
this class enforces Django's CSRF check itself once it has authenticated a
cookie. Nothing else in DRF does that for a non-session authenticator.
"""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import CSRFCheck
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import JWTAuthentication


def enforce_csrf(request: Request) -> None:
    # Neither callable is ever invoked by CsrfViewMiddleware -- it only checks
    # the token -- but the signature wants something request-shaped.
    check = CSRFCheck(lambda r: None)  # type: ignore[arg-type]
    check.process_request(request)
    reason = check.process_view(request, None, (), {})  # type: ignore[arg-type]
    if reason:
        raise exceptions.PermissionDenied(f"CSRF Failed: {reason}")


class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request: Request):
        raw_token = request.COOKIES.get(settings.JWT_AUTH_COOKIE)
        if raw_token is None:
            return None
        validated_token = self.get_validated_token(raw_token.encode())
        enforce_csrf(request)
        return self.get_user(validated_token), validated_token


class CookieJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """Documents the cookie scheme in /api/docs/ -- drf-spectacular only knows
    the stock JWTAuthentication out of the box, and would otherwise silently
    drop the security requirement for every endpoint."""

    target_class = CookieJWTAuthentication
    name = "cookieAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": settings.JWT_AUTH_COOKIE,
            "description": (
                "HttpOnly cookie set by /auth/login/, /auth/otp/verify/, "
                "/auth/refresh/ or an SSO callback. State-changing requests "
                "also need an X-CSRFToken header carrying the csrftoken "
                "cookie's value."
            ),
        }
