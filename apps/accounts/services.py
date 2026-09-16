"""
Authentication services.

All multi-step auth state changes live here rather than in views, so the same
rules apply whether a request arrives through the API, an SSO callback or a
management command.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import OtpChallenge, Role, UserAccount, UserRole
from apps.core.models import AuditLog

# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def record_audit(
    *,
    action: str,
    actor: UserAccount | None = None,
    entity_type: str = "",
    entity_id: int | None = None,
    detail: dict | None = None,
    request=None,
) -> AuditLog:
    """Write an audit entry. Never raises — auditing must not break the flow."""
    ip_address = None
    user_agent = ""
    if request is not None:
        ip_address = _client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:400]

    return AuditLog.objects.create(
        actor=actor if (actor and actor.pk) else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _client_ip(request) -> str | None:
    # X-Forwarded-For is only trustworthy behind a proxy that sets it. In
    # production the deployment must strip client-supplied values.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


# --------------------------------------------------------------------------- #
# Login throttling / lockout
# --------------------------------------------------------------------------- #

_LOCKOUT_PREFIX = "bcm:login:fail:"


def _lockout_key(email: str) -> str:
    return f"{_LOCKOUT_PREFIX}{email.strip().lower()}"


def failed_login_count(email: str) -> int:
    return cache.get(_lockout_key(email), 0)


def is_locked_out(email: str) -> bool:
    return failed_login_count(email) >= settings.LOGIN_MAX_FAILED_ATTEMPTS


def register_failed_login(email: str, *, request=None) -> int:
    """Increment the failure counter and audit a lockout when it trips."""
    key = _lockout_key(email)
    # add() only sets when absent, so the TTL is anchored to the first failure and
    # an attacker cannot extend the window by continuing to guess.
    cache.add(key, 0, timeout=settings.LOGIN_LOCKOUT_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:
        # The key expired between add() and incr().
        cache.set(key, 1, timeout=settings.LOGIN_LOCKOUT_SECONDS)
        count = 1

    if count == settings.LOGIN_MAX_FAILED_ATTEMPTS:
        record_audit(
            action=AuditLog.Action.LOGIN_LOCKOUT,
            entity_type="UserAccount",
            detail={"email": email, "attempts": count},
            request=request,
        )
    return count


def clear_failed_logins(email: str) -> None:
    cache.delete(_lockout_key(email))


# --------------------------------------------------------------------------- #
# OTP
# --------------------------------------------------------------------------- #


def _hash_otp(code: str, user_id: int) -> str:
    """Hash an OTP for storage.

    Salted with the user id and keyed with SECRET_KEY so a leaked
    `otp_challenges` table cannot be brute-forced offline against a six-digit
    space, which would otherwise take milliseconds.
    """
    message = f"{user_id}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _generate_code() -> str:
    length = settings.OTP_LENGTH
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


class OtpError(Exception):
    """Raised for OTP problems that should surface as a 400."""

    def __init__(self, message: str, code: str = "otp_invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def issue_otp(user: UserAccount, purpose: str, *, request=None) -> tuple[OtpChallenge, str]:
    """Create a challenge and return it with the plaintext code.

    The plaintext is returned so the caller can mail it; it is never stored.
    """
    recent = (
        OtpChallenge.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if recent:
        cooldown = timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS)
        elapsed = timezone.now() - recent.created_at
        if elapsed < cooldown:
            wait = int((cooldown - elapsed).total_seconds())
            raise OtpError(
                f"Please wait {wait} seconds before requesting another code.",
                code="otp_cooldown",
            )

    # Any outstanding challenge for this purpose is void once a new one is issued.
    OtpChallenge.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )

    code = _generate_code()
    challenge = OtpChallenge.objects.create(
        user=user,
        code_hash=_hash_otp(code, user.pk),
        purpose=purpose,
        expires_at=timezone.now() + timedelta(seconds=settings.OTP_EXPIRY_SECONDS),
    )
    return challenge, code


def verify_otp(user: UserAccount, purpose: str, code: str) -> OtpChallenge:
    """Consume a challenge, or raise OtpError.

    Single-use: the row is marked consumed on success, so a replayed code fails.
    """
    challenge = (
        OtpChallenge.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if challenge is None:
        raise OtpError("No active code. Request a new one.", code="otp_not_found")

    if challenge.is_expired:
        challenge.consumed_at = timezone.now()
        challenge.save(update_fields=["consumed_at"])
        raise OtpError("That code has expired. Request a new one.", code="otp_expired")

    if challenge.attempts >= settings.OTP_MAX_ATTEMPTS:
        challenge.consumed_at = timezone.now()
        challenge.save(update_fields=["consumed_at"])
        raise OtpError("Too many attempts. Request a new code.", code="otp_attempts")

    expected = _hash_otp(code or "", user.pk)
    if not hmac.compare_digest(expected, challenge.code_hash):
        challenge.attempts += 1
        challenge.save(update_fields=["attempts"])
        remaining = settings.OTP_MAX_ATTEMPTS - challenge.attempts
        raise OtpError(
            f"Incorrect code. {max(remaining, 0)} attempts remaining.",
            code="otp_invalid",
        )

    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["consumed_at"])
    return challenge


# --------------------------------------------------------------------------- #
# SSO provisioning (1.5)
# --------------------------------------------------------------------------- #


@transaction.atomic
def provision_sso_user(
    *, email: str, provider: str, subject: str, display_name: str = "", request=None
) -> tuple[UserAccount, bool]:
    """Find or create the account behind an SSO sign-in.

    Policy (plan task 1.5): an SSO identity with no matching `Employee` still gets
    an account — created PENDING, with no roles, resolving to an empty scope.
    Refusing the sign-in would be worse: it presents a provisioning gap as "wrong
    password", which is the single most expensive kind of support ticket.

    Matching is by `provider_subject` first — the IdP's stable subject claim —
    falling back to a verified email. Email alone is not sufficient identity: an
    address can be reassigned to a new person after someone leaves.
    """
    from apps.accounts.models import Employee

    email = email.strip().lower()

    user = UserAccount.objects.filter(auth_provider=provider, provider_subject=subject).first()
    created = False

    if user is None and email:
        user = UserAccount.objects.filter(email__iexact=email).first()
        if user is not None:
            # Bind this identity to the existing account on first SSO sign-in.
            user.auth_provider = provider
            user.provider_subject = subject
            user.save(update_fields=["auth_provider", "provider_subject"])

    if user is None:
        employee = Employee.objects.filter(email__iexact=email).first() if email else None
        user = UserAccount.objects.create(
            email=email,
            display_name=display_name or (email.split("@")[0] if email else "User"),
            username=email.split("@")[0] if email else "",
            employee=employee,
            auth_provider=provider,
            provider_subject=subject,
            # An account linked to a known employee is usable immediately;
            # an unknown one waits for an administrator.
            user_status=(UserAccount.Status.ACTIVE if employee else UserAccount.Status.PENDING),
            is_active=True,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        created = True

        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=user,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={
                "reason": "sso_provisioning",
                "provider": provider,
                "linked_employee": bool(employee),
                "status": user.user_status,
            },
            request=request,
        )

    # Late linking: the HR record may have appeared since first sign-in.
    if user.employee_id is None and email:
        employee = Employee.objects.filter(email__iexact=email).first()
        if employee is not None:
            user.employee = employee
            user.user_status = UserAccount.Status.ACTIVE
            user.save(update_fields=["employee", "user_status"])

    return user, created


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


def assign_role(
    *, user: UserAccount, role: Role, assigned_by: UserAccount | None = None, request=None
) -> UserRole:
    user_role, created = UserRole.objects.get_or_create(
        user=user, role=role, defaults={"assigned_by": assigned_by}
    )
    if created:
        record_audit(
            action=AuditLog.Action.ROLE_CHANGE,
            actor=assigned_by,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={"granted": role.role_code},
            request=request,
        )
    return user_role


def revoke_role(
    *, user: UserAccount, role: Role, actor: UserAccount | None = None, request=None
) -> None:
    deleted, _ = UserRole.objects.filter(user=user, role=role).delete()
    if deleted:
        record_audit(
            action=AuditLog.Action.ROLE_CHANGE,
            actor=actor,
            entity_type="UserAccount",
            entity_id=user.pk,
            detail={"revoked": role.role_code},
            request=request,
        )
