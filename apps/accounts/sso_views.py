"""SSO endpoints (Phase 1.6, 1.7)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import UserAccount
from apps.accounts.services import provision_sso_user, record_audit
from apps.accounts.sso import (
    PROVIDERS,
    SsoError,
    build_authorize_url,
    complete_sso_login,
    get_provider,
    issue_state,
)
from apps.accounts.views import issue_tokens
from apps.core.models import AuditLog
from apps.notifications.models import NotificationEvent
from apps.notifications.services import send_notification


class SsoCallbackSerializer(serializers.Serializer):
    code = serializers.CharField()
    state = serializers.CharField()


def _sso_error_response(exc: SsoError, http_status=status.HTTP_400_BAD_REQUEST):
    return Response(
        {"detail": exc.message, "code": exc.code, "field_errors": {}}, status=http_status
    )


class SsoProvidersView(APIView):
    """Which providers this deployment can actually use.

    The frontend renders sign-in buttons from this, so an unconfigured provider
    never shows a button that leads to a dead end.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(summary="List configured SSO providers")
    def get(self, request):
        providers = []
        for name, factory in PROVIDERS.items():
            provider = factory()
            providers.append({"name": name, "configured": provider.configured})
        return Response({"providers": providers})


class SsoAuthorizeView(APIView):
    """Start the flow: return the provider URL for the browser to visit."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="Begin SSO sign-in",
        parameters=[OpenApiParameter("provider", str, OpenApiParameter.PATH, enum=list(PROVIDERS))],
    )
    def get(self, request, provider: str):
        try:
            oauth_provider = get_provider(provider)
        except SsoError as exc:
            return _sso_error_response(exc)

        state = issue_state(provider)
        return Response(
            {"authorize_url": build_authorize_url(oauth_provider, state), "state": state}
        )


class SsoCallbackView(APIView):
    """Finish the flow: exchange the code and return BCM tokens.

    An identity with no matching employee record still gets an account and a
    working token — PENDING, with no roles, resolving to an empty scope (plan
    task 1.5). It logs in, sees nothing, and an administrator is told. Refusing
    the sign-in would present a provisioning gap as a credentials failure.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="Complete SSO sign-in",
        request=SsoCallbackSerializer,
        parameters=[OpenApiParameter("provider", str, OpenApiParameter.PATH, enum=list(PROVIDERS))],
    )
    def post(self, request, provider: str):
        serializer = SsoCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            identity = complete_sso_login(
                provider,
                serializer.validated_data["code"],
                serializer.validated_data["state"],
            )
        except SsoError as exc:
            record_audit(
                action=AuditLog.Action.LOGIN_FAILURE,
                entity_type="UserAccount",
                detail={"provider": provider, "reason": exc.code},
                request=request,
            )
            return _sso_error_response(exc, status.HTTP_401_UNAUTHORIZED)

        user, created = provision_sso_user(
            email=identity.email,
            provider=provider,
            subject=identity.subject,
            display_name=identity.display_name,
            request=request,
        )

        if not user.is_active:
            return Response(
                {
                    "detail": "This account is disabled.",
                    "code": "account_disabled",
                    "field_errors": {},
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if created and user.user_status == UserAccount.Status.PENDING:
            send_notification(
                event_type=NotificationEvent.OTP_CODE,
                to_email=user.email,
                subject="Your BCM account is awaiting activation",
                template_name="account_pending",
                context={"display_name": user.display_name, "provider": provider.title()},
                recipient_user=user,
                entity_type="UserAccount",
                entity_id=user.pk,
            )

        record_audit(
            action=AuditLog.Action.LOGIN_SUCCESS,
            actor=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={"provider": provider, "new_account": created},
            request=request,
        )

        return Response(
            {
                **issue_tokens(user),
                "user_status": user.user_status,
                "new_account": created,
            }
        )
