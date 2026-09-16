"""
Role-based permission classes (AD-3).

These answer "may this role perform this action at all?". They are only half the
story — the other half is scope ("on *this* object?"), which lives in
`apps.accounts.scoping`. A view that checks a role but does not scope its queryset
will happily list rows the user may not see.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.accounts.roles import RoleCode


def user_role_codes(user) -> set[str]:
    """Role codes for a user, cached on the request-scoped user instance.

    Permission classes are consulted repeatedly per request (once for the view,
    once per object), so this must not re-query each time.
    """
    if not user or not user.is_authenticated:
        return set()
    cached = getattr(user, "_cached_role_codes", None)
    if cached is None:
        cached = user.role_codes
        user._cached_role_codes = cached
    return cached


class HasAnyRole(BasePermission):
    """Grant access when the user holds at least one of `required_roles`.

    Subclass rather than instantiate, so the role set is visible in the view:

        class IsApprover(HasAnyRole):
            required_roles = APPROVAL_ROLES
    """

    required_roles: frozenset[str] | set[str] | tuple[str, ...] = frozenset()
    message = "Your role does not permit this action."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        # A view-level override wins, so one permission class can serve several
        # viewsets without a subclass each.
        required = set(getattr(view, "required_roles", None) or self.required_roles)
        if not required:
            return True
        return bool(user_role_codes(user) & required)


def role_required(*roles: str) -> type[HasAnyRole]:
    """Build a permission class for an ad-hoc role set.

    permission_classes = [role_required(RoleCode.ADMIN)]
    """
    role_set = frozenset(roles)

    class _RoleRequired(HasAnyRole):
        required_roles = role_set

    _RoleRequired.__name__ = f"RoleRequired_{'_'.join(sorted(role_set))}"
    return _RoleRequired


class IsAdmin(HasAnyRole):
    required_roles = frozenset({RoleCode.ADMIN})
    message = "Administrator role required."


class IsActiveUser(BasePermission):
    """Reject accounts that are not in an active domain state.

    Distinct from Django's `is_active`: a PENDING user (an SSO sign-in with no HR
    record yet) can authenticate and read their own profile, but cannot act. That
    is deliberate — refusing the login outright disguises a provisioning problem
    as a bad password.
    """

    message = "Your account is pending activation."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        from apps.accounts.models import UserAccount

        return user.user_status == UserAccount.Status.ACTIVE


class ReadOnly(BasePermission):
    """Allow safe methods only. Compose with a role class for read-only roles."""

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
