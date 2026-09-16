"""Authentication and access-control endpoints."""

from __future__ import annotations

import contextlib

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import (
    Employee,
    OtpChallenge,
    Role,
    UserAccount,
    UserEstateScope,
    UserRole,
)
from apps.accounts.permissions import IsActiveUser, IsAdmin
from apps.accounts.serializers import (
    CurrentUserSerializer,
    EmployeeSummarySerializer,
    LoginSerializer,
    LogoutSerializer,
    OtpResendSerializer,
    OtpVerifySerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RefreshSerializer,
    RegistrationSerializer,
    RoleSerializer,
    UserAdminSerializer,
    UserEstateScopeSerializer,
    UserRoleSerializer,
)
from apps.accounts.services import (
    OtpError,
    clear_failed_logins,
    is_locked_out,
    issue_otp,
    record_audit,
    register_failed_login,
    verify_otp,
)
from apps.core.models import AuditLog
from apps.notifications.models import NotificationEvent
from apps.notifications.services import build_idempotency_key, send_notification


def issue_tokens(user: UserAccount) -> dict:
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class LoginThrottle(ScopedRateThrottle):
    scope = "login"


class LoginView(APIView):
    """Step one of local login: verify the password.

    Returns tokens directly when OTP is disabled, otherwise emails a code and
    returns `otp_required`. The OTP challenge is only ever created after the
    password has been verified, so the second factor cannot be used to bypass the
    first.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"
    serializer_class = LoginSerializer

    @extend_schema(
        request=LoginSerializer,
        responses={200: OpenApiResponse(description="Tokens, or otp_required")},
        summary="Log in with email and password",
    )
    def post(self, request):
        email = str(request.data.get("email", "")).strip().lower()

        if email and is_locked_out(email):
            record_audit(
                action=AuditLog.Action.LOGIN_FAILURE,
                entity_type="UserAccount",
                detail={"email": email, "reason": "locked_out"},
                request=request,
            )
            return Response(
                {
                    "detail": "Too many failed attempts. Try again later.",
                    "code": "account_locked",
                    "field_errors": {},
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = self.serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            if email:
                register_failed_login(email, request=request)
            record_audit(
                action=AuditLog.Action.LOGIN_FAILURE,
                entity_type="UserAccount",
                detail={"email": email},
                request=request,
            )
            return Response(
                {
                    "detail": "Incorrect email or password.",
                    "code": "invalid_credentials",
                    "field_errors": {},
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = serializer.validated_data["user"]
        clear_failed_logins(email)

        # The second factor applies when the environment has it on AND the
        # administrator has not switched it off for this user.
        if not (settings.REQUIRE_OTP_FOR_LOGIN and user.mfa_enabled):
            record_audit(
                action=AuditLog.Action.LOGIN_SUCCESS,
                actor=user,
                entity_type="UserAccount",
                entity_id=user.pk,
                detail={
                    "mfa": False,
                    "reason": (
                        "disabled_for_user"
                        if settings.REQUIRE_OTP_FOR_LOGIN
                        else "disabled_globally"
                    ),
                },
                request=request,
            )
            return Response({"otp_required": False, **issue_tokens(user)})

        try:
            challenge, code = issue_otp(user, OtpChallenge.Purpose.LOGIN_2FA, request=request)
        except OtpError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, "field_errors": {}},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        _send_otp_email(user, code, challenge)

        return Response(
            {
                "otp_required": True,
                "email": user.email,
                "expires_in": settings.OTP_EXPIRY_SECONDS,
            }
        )


def _send_otp_email(user: UserAccount, code: str, challenge: OtpChallenge):
    send_notification(
        event_type=NotificationEvent.OTP_CODE,
        to_email=user.email,
        subject="Your BCM verification code",
        template_name="otp_code",
        context={
            "display_name": user.display_name,
            "code": code,
            "expiry_minutes": max(settings.OTP_EXPIRY_SECONDS // 60, 1),
        },
        recipient_user=user,
        entity_type="OtpChallenge",
        entity_id=challenge.pk,
        # Salted per challenge so a resend is a distinct notification rather than
        # being suppressed as a duplicate.
        idempotency_key=build_idempotency_key(NotificationEvent.OTP_CODE, challenge.pk, user.email),
    )


class OtpVerifyView(APIView):
    """Step two of local login: exchange a valid code for tokens."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=OtpVerifySerializer, summary="Verify a login OTP")
    def post(self, request):
        serializer = OtpVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()

        user = UserAccount.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            # Same shape as a wrong code — do not reveal whether the account exists.
            return Response(
                {"detail": "Incorrect code.", "code": "otp_invalid", "field_errors": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            verify_otp(user, OtpChallenge.Purpose.LOGIN_2FA, serializer.validated_data["code"])
        except OtpError as exc:
            record_audit(
                action=AuditLog.Action.LOGIN_FAILURE,
                actor=user,
                entity_type="UserAccount",
                entity_id=user.pk,
                detail={"reason": exc.code},
                request=request,
            )
            return Response(
                {"detail": exc.message, "code": exc.code, "field_errors": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        clear_failed_logins(email)
        record_audit(
            action=AuditLog.Action.LOGIN_SUCCESS,
            actor=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={"mfa": True},
            request=request,
        )
        return Response(issue_tokens(user))


class OtpResendView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=OtpResendSerializer, summary="Resend a login OTP")
    def post(self, request):
        serializer = OtpResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()

        user = UserAccount.objects.filter(email__iexact=email, is_active=True).first()
        # Always report success: whether an address has an account is not
        # something an unauthenticated caller should be able to probe.
        if user is None:
            return Response({"detail": "If that account exists, a code has been sent."})

        # Only resend when a challenge is already outstanding, so this endpoint
        # cannot be used to mail someone who never attempted to log in.
        has_pending = OtpChallenge.objects.filter(
            user=user, purpose=OtpChallenge.Purpose.LOGIN_2FA, consumed_at__isnull=True
        ).exists()
        if not has_pending:
            return Response({"detail": "If that account exists, a code has been sent."})

        try:
            challenge, code = issue_otp(user, OtpChallenge.Purpose.LOGIN_2FA, request=request)
        except OtpError as exc:
            return Response(
                {"detail": exc.message, "code": exc.code, "field_errors": {}},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        _send_otp_email(user, code, challenge)
        return Response({"detail": "If that account exists, a code has been sent."})


class RefreshView(APIView):
    """Rotate a refresh token.

    Rotation plus blacklisting is enabled, so the presented token is invalidated
    here. The frontend must serialise concurrent refreshes or it will burn its
    own token.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(request=RefreshSerializer, summary="Refresh an access token")
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            refresh = RefreshToken(serializer.validated_data["refresh"])
            access = str(refresh.access_token)
            if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS"):
                if settings.SIMPLE_JWT.get("BLACKLIST_AFTER_ROTATION"):
                    refresh.blacklist()
                user = UserAccount.objects.filter(pk=refresh["user_id"]).first()
                if user is None or not user.is_active:
                    raise TokenError("User is no longer active.")
                return Response(issue_tokens(user))
            return Response({"access": access})
        except TokenError as exc:
            return Response(
                {"detail": str(exc), "code": "token_invalid", "field_errors": {}},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=LogoutSerializer, summary="Log out and revoke a refresh token")
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Already expired or blacklisted? Logging out twice is not an error.
        with contextlib.suppress(TokenError):
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        record_audit(
            action=AuditLog.Action.LOGOUT,
            actor=request.user,
            entity_type="UserAccount",
            entity_id=request.user.pk,
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=CurrentUserSerializer, summary="The current user")
    def get(self, request):
        return Response(CurrentUserSerializer(request.user).data)

    @extend_schema(summary="Update my profile")
    def patch(self, request):
        from apps.accounts.profile import ProfileUpdateSerializer

        body = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        body.save()
        record_audit(
            action=AuditLog.Action.RECORD_UPDATED,
            actor=request.user,
            entity_type="UserAccount",
            entity_id=request.user.pk,
            detail={"profile": sorted(body.validated_data.keys())},
            request=request,
        )
        return Response(CurrentUserSerializer(request.user).data)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=RegistrationSerializer, summary="Register an account")
    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={"reason": "self_registration", "status": user.user_status},
            request=request,
        )
        return Response(
            {
                "detail": (
                    "Account created. An administrator must assign your role "
                    "before you can access plan data."
                ),
                "user_status": user.user_status,
            },
            status=status.HTTP_201_CREATED,
        )


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=PasswordResetRequestSerializer, summary="Request a password reset")
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        generic = {"detail": "If that account exists, a reset link has been sent."}

        user = UserAccount.objects.filter(email__iexact=email, is_active=True).first()
        if user is None or not user.has_usable_password():
            # SSO-only accounts have no password to reset; same generic reply.
            return Response(generic)

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        reset_url = f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}"

        send_notification(
            event_type=NotificationEvent.PASSWORD_RESET,
            to_email=user.email,
            subject="Reset your BCM password",
            template_name="password_reset",
            context={
                "display_name": user.display_name,
                "reset_url": reset_url,
                "expiry_minutes": max(settings.PASSWORD_RESET_TIMEOUT // 60, 1),
            },
            recipient_user=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            idempotency_key=build_idempotency_key(
                NotificationEvent.PASSWORD_RESET, user.pk, user.email, salt=token[:16]
            ),
        )
        return Response(generic)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=PasswordResetConfirmSerializer, summary="Complete a password reset")
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        invalid = Response(
            {
                "detail": "That reset link is invalid or has expired.",
                "code": "reset_invalid",
                "field_errors": {},
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

        try:
            user_id = force_str(urlsafe_base64_decode(data["uid"]))
        except (TypeError, ValueError, OverflowError):
            return invalid

        user = UserAccount.objects.filter(pk=user_id, is_active=True).first()
        if user is None or not default_token_generator.check_token(user, data["token"]):
            return invalid

        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        clear_failed_logins(user.email)
        record_audit(
            action=AuditLog.Action.PASSWORD_RESET,
            actor=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            request=request,
        )
        return Response({"detail": "Password updated. You can now sign in."})


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=PasswordChangeSerializer, summary="Change your password")
    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        record_audit(
            action=AuditLog.Action.PASSWORD_RESET,
            actor=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={"self_service": True},
            request=request,
        )
        return Response({"detail": "Password updated."})


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #


class RoleViewSet(viewsets.ReadOnlyModelViewSet):
    """Roles are seeded, not created through the API.

    Permission classes reference these codes directly, so inventing one at
    runtime would produce a role that grants nothing.
    """

    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated]


class UserAdminViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = UserAccount.objects.select_related("employee").order_by("display_name")
    serializer_class = UserAdminSerializer
    permission_classes = [IsAuthenticated, IsAdmin]
    filterset_fields = ["user_status", "auth_provider", "is_active"]
    search_fields = ["email", "display_name"]

    def perform_update(self, serializer):
        before = {
            "user_status": serializer.instance.user_status,
            "is_active": serializer.instance.is_active,
            "mfa_enabled": serializer.instance.mfa_enabled,
            "employee_id": serializer.instance.employee_id,
            "role_codes": sorted(serializer.instance.role_codes),
        }
        user = serializer.save()
        record_audit(
            action=AuditLog.Action.RECORD_UPDATED,
            actor=self.request.user,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={
                "before": before,
                "after": {
                    "user_status": user.user_status,
                    "is_active": user.is_active,
                    "mfa_enabled": user.mfa_enabled,
                    "employee_id": user.employee_id,
                    "role_codes": sorted(user.role_codes),
                },
                "changed": sorted(serializer.validated_data.keys()),
            },
            request=self.request,
        )

    @extend_schema(summary="Roles held by this user")
    @action(detail=True, methods=["get"])
    def roles(self, request, pk=None):
        user = self.get_object()
        return Response(UserRoleSerializer(user.user_roles.select_related("role"), many=True).data)


class UserRoleViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    queryset = UserRole.objects.select_related("user", "role")
    serializer_class = UserRoleSerializer
    permission_classes = [IsAuthenticated, IsAdmin]
    filterset_fields = ["user", "role"]

    def perform_create(self, serializer):
        user_role = serializer.save(assigned_by=self.request.user)
        record_audit(
            action=AuditLog.Action.ROLE_CHANGE,
            actor=self.request.user,
            entity_type="UserAccount",
            entity_id=user_role.user_id,
            detail={"granted": user_role.role.role_code},
            request=self.request,
        )

    def perform_destroy(self, instance):
        record_audit(
            action=AuditLog.Action.ROLE_CHANGE,
            actor=self.request.user,
            entity_type="UserAccount",
            entity_id=instance.user_id,
            detail={"revoked": instance.role.role_code},
            request=self.request,
        )
        instance.delete()


class UserEstateScopeViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Grants a user visibility of an estate (AD-3).

    Admins and auditors see every estate by role and need no rows here.
    """

    queryset = UserEstateScope.objects.select_related("user", "estate")
    serializer_class = UserEstateScopeSerializer
    permission_classes = [IsAuthenticated, IsAdmin]
    filterset_fields = ["user", "estate", "active_flag"]

    def perform_create(self, serializer):
        scope = serializer.save(granted_by=self.request.user)
        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=self.request.user,
            entity_type="UserEstateScope",
            entity_id=scope.pk,
            detail={"user_id": scope.user_id, "estate_id": scope.estate_id},
            request=self.request,
        )


class EmployeeLookupViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Employee directory, for pickers such as coordinator assignment (Phase 3.2).

    Read-only and deliberately thin: it returns a name, number and work email so a
    user can find the right colleague, and nothing else. Employee records are HR
    data — a picker is not a reason to expose grade, manager chain or contact
    numbers to every authenticated user.

    Not estate-scoped: a coordinator may legitimately be someone outside the
    estates the assigner can see, and hiding them would make the assignment
    impossible rather than merely awkward. Assignment itself is still gated by
    role and by the plan version's own scope.
    """

    queryset = Employee.objects.order_by("full_name")
    serializer_class = EmployeeSummarySerializer
    permission_classes = [IsAuthenticated, IsActiveUser]
    search_fields = ["full_name", "email", "employee_number"]
    filterset_fields = ["employee_number"]
