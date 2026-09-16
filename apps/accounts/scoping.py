"""
Queryset-level data scoping (AD-3).

Role checks answer "may this role do this at all?". Scoping answers "on which
rows?". Both are required: a view that checks the role but returns an unscoped
queryset leaks every row in the table to its list endpoint, and that is exactly
the failure mode the cost code list (journey step 3) is exposed to.

Scope is applied in `get_queryset()`, not only in `has_object_permission()`.
Object permissions are consulted for detail routes; list routes never call them.

Resolution order, first match wins:

  1. Role grants all estates (BCM_ADMIN, BCM_AUDITOR)  -> everything
  2. Explicit rows in `user_estate_scopes`             -> those estates
  3. No rows                                           -> nothing

Then, within those estates, a second cut for the roles that own records rather
than read them: a coordinator sees the cost codes they are assigned to, a BU
lead or approver the ones they lead — not every plan in the estate. Viewers,
reviewers and the other read roles keep the whole estate. `narrowed_to_own` and
`own_cost_code_ids` below are that cut; `cost_code_scope_path` is how a view
opts into it.

A user with no roles and no scope rows sees an empty list, never an error. That
is the deliberate landing state for an SSO sign-in with no HR record yet: the
account works, it just has no data, which surfaces a provisioning gap instead of
disguising it as a failed login.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.accounts.roles import ALL_ESTATE_ROLES, ESTATE_WIDE_ROLES, OWN_RECORD_ROLES, RoleCode


class ScopeResolver:
    """Resolves what one user may see. Built once per request."""

    def __init__(self, user):
        self.user = user
        self._role_codes: set[str] | None = None
        self._estate_ids: frozenset[int] | None = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.user and self.user.is_authenticated)

    @property
    def role_codes(self) -> set[str]:
        if self._role_codes is None:
            from apps.accounts.permissions import user_role_codes

            self._role_codes = user_role_codes(self.user)
        return self._role_codes

    @property
    def sees_all_estates(self) -> bool:
        return bool(self.role_codes & ALL_ESTATE_ROLES)

    @property
    def estate_ids(self) -> frozenset[int]:
        """Explicitly granted estate ids. Meaningless when `sees_all_estates`."""
        if self._estate_ids is None:
            if not self.is_authenticated:
                self._estate_ids = frozenset()
            else:
                self._estate_ids = frozenset(
                    self.user.estate_scopes.filter(
                        active_flag=True, estate__active_flag=True
                    ).values_list("estate_id", flat=True)
                )
        return self._estate_ids

    @property
    def employee_id(self) -> int | None:
        """The linked HR record, if any. None for an SSO user awaiting linking."""
        return getattr(self.user, "employee_id", None)

    def has_role(self, *codes: str) -> bool:
        return bool(self.role_codes & set(codes))

    @property
    def narrowed_to_own(self) -> bool:
        """Does this user see only the cost codes they have a claim on?

        True for a coordinator, BU lead or approver who holds no estate-wide
        role. An administrator, or anyone who is also a viewer, is not narrowed.
        """
        if not self.is_authenticated or self.sees_all_estates:
            return False
        if self.role_codes & ESTATE_WIDE_ROLES:
            return False
        return bool(self.role_codes & OWN_RECORD_ROLES)

    def own_cost_code_ids(self) -> QuerySet:
        """The cost codes this user is assigned to or leads, as a subquery.

        A coordinator's claim is an active assignment on any version of the
        plan — once assigned, they see the cost code's earlier versions too,
        which is what "previous versions" on the cost code page needs. A BU
        lead's claim is the cost code's `bu_lead` email matching theirs (the
        confirmed approval rule, see `plans.access.is_bu_lead_for`).
        """
        from apps.organization.models import CostCode

        claim = Q(pk__in=[])
        if self.has_role(RoleCode.COORDINATOR) and self.employee_id is not None:
            from apps.plans.models import CoordinatorAssignment

            assigned = CoordinatorAssignment.objects.filter(
                employee_id=self.employee_id, active_flag=True
            ).values("plan_version_id")
            claim |= Q(plans__versions__pk__in=assigned)
        if self.has_role(RoleCode.BU_LEAD, RoleCode.APPROVER):
            emails = {(self.user.email or "").strip().lower()}
            employee = getattr(self.user, "employee", None)
            if employee is not None and employee.email:
                emails.add(employee.email.strip().lower())
            emails.discard("")
            for email in emails:
                claim |= Q(bu_lead__email__iexact=email)
        return CostCode.objects.filter(claim).values("pk")

    def narrow_to_own(self, queryset: QuerySet, cost_code_path: str) -> QuerySet:
        """Apply the own-record cut. `cost_code_path` reaches `cost_code_id`
        from the queryset's model; "" when the model is CostCode."""
        if not self.narrowed_to_own:
            return queryset
        field = f"{cost_code_path}__in" if cost_code_path else "pk__in"
        return queryset.filter(**{field: self.own_cost_code_ids()})

    def __repr__(self):  # pragma: no cover - debugging aid
        who = getattr(self.user, "email", "anonymous")
        scope = "ALL" if self.sees_all_estates else sorted(self.estate_ids)
        return f"<ScopeResolver {who} roles={sorted(self.role_codes)} estates={scope}>"


class ScopedQuerySetMixin:
    """Filter a viewset's queryset to what the caller may see.

    Declare how the model reaches an estate:

        class CostCodeViewSet(ScopedQuerySetMixin, ModelViewSet):
            estate_scope_path = "estate_id"

        class PlanVersionViewSet(ScopedQuerySetMixin, ModelViewSet):
            estate_scope_path = "plan__cost_code__estate_id"

    Set `estate_scope_path = None` to opt a viewset out — do that only for models
    with no estate dimension at all (the lookup catalogue, the question bank), and
    say why in a comment.
    """

    #: Lookup path from this model to `estate_id`. "" means the model *is* Estate.
    estate_scope_path: str | None = ""
    #: Lookup path from this model to `cost_code_id`, for the own-record cut
    #: (`ScopeResolver.narrowed_to_own`). "" means the model *is* CostCode; None
    #: (the default) means the view has no per-cost-code dimension to narrow.
    cost_code_scope_path: str | None = None

    def get_scope(self) -> ScopeResolver:
        if not hasattr(self, "_scope_resolver"):
            self._scope_resolver = ScopeResolver(self.request.user)
        return self._scope_resolver

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        return self.scope_queryset(queryset)

    def scope_queryset(self, queryset: QuerySet) -> QuerySet:
        scope = self.get_scope()

        if not scope.is_authenticated:
            return queryset.none()

        if self.estate_scope_path is None:
            # Model has no estate dimension. Role permissions still apply.
            return queryset

        if scope.sees_all_estates:
            return queryset

        estate_ids = scope.estate_ids
        if not estate_ids:
            return queryset.none()

        if self.estate_scope_path == "":
            queryset = queryset.filter(pk__in=estate_ids)
        else:
            queryset = queryset.filter(**{f"{self.estate_scope_path}__in": estate_ids})

        if self.cost_code_scope_path is not None:
            queryset = scope.narrow_to_own(queryset, self.cost_code_scope_path)
        return queryset
