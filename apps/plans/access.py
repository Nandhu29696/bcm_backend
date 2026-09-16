"""
Resolving a plan version from a URL through the caller's estate scope (AD-3).

Shared by every view that hangs off `/plan-versions/{id}/` — the version list and
history in `plans`, the questionnaire and answers in `questionnaire`, and later
the risk register and review actions. One implementation, so a scoping mistake
cannot be made per app.
"""

from __future__ import annotations

from django.db.models import Max, Prefetch
from django.shortcuts import get_object_or_404

from apps.accounts.roles import APPROVAL_ROLES, RoleCode
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.plans.models import CoordinatorAssignment, Plan, PlanVersion
from apps.plans.versioning import OPEN_STATUSES


class PlanVersionScopedMixin(ScopedQuerySetMixin):
    """Resolves the plan version named in the URL through the caller's estate scope.

    `scope_plan_versions` is separate from the mixin's own `estate_scope_path` on
    purpose. Views that hang off a plan version do not all share its model — the
    coordinator endpoints list `CoordinatorAssignment` rows — so a single path
    attribute cannot serve both "find the parent safely" and "scope my own
    queryset". Conflating them silently applies a lookup path that does not exist
    on the view's model, or skips scoping on the parent.
    """

    estate_scope_path = "plan__cost_code__estate_id"

    def scope_plan_versions(self, queryset):
        """Scope a queryset *of plan versions*, whatever this view's own model is."""
        scope = self.get_scope()
        if not scope.is_authenticated:
            return queryset.none()
        if scope.sees_all_estates:
            return queryset
        if not scope.estate_ids:
            return queryset.none()
        queryset = queryset.filter(plan__cost_code__estate_id__in=scope.estate_ids)
        # A coordinator sees the plans they are assigned to, a BU lead the ones
        # they lead; a viewer the whole estate. Same cut as the cost code list.
        return scope.narrow_to_own(queryset, "plan__cost_code_id")

    def base_queryset(self):
        return PlanVersion.objects.select_related(
            "plan",
            "plan__cost_code",
            "plan__cost_code__estate",
            "plan__cost_code__process",
            "approved_by",
            "created_by",
        ).prefetch_related(
            Prefetch(
                "coordinator_assignments",
                queryset=CoordinatorAssignment.objects.select_related("employee"),
            )
        )

    def get_plan_version(self) -> PlanVersion:
        if not hasattr(self, "_plan_version"):
            self._plan_version = get_object_or_404(
                self.scope_plan_versions(self.base_queryset()),
                pk=self.kwargs["plan_version_id"],
            )
        return self._plan_version

    def caller_may_approve(self, version: PlanVersion) -> bool:
        return caller_may_approve(self.get_scope(), version)

    def caller_may_author(self, version: PlanVersion) -> bool:
        """May the caller change this version's content?

        Authoring needs an authoring role *and* a claim on the version: admins
        always; a coordinator only when actively assigned to it. Being able to
        see a plan (estate scope) is deliberately not enough to edit it — a
        coordinator granted an estate still edits only the plans they own.
        """
        scope = self.get_scope()
        if scope.has_role(RoleCode.ADMIN):
            return True
        if not scope.has_role(RoleCode.COORDINATOR):
            return False
        employee_id = scope.employee_id
        if employee_id is None:
            return False
        return CoordinatorAssignment.objects.filter(
            plan_version=version, employee_id=employee_id, active_flag=True
        ).exists()


def version_list_context(plan: Plan) -> dict:
    """Shared context so `is_current` and `can_copy` cost no extra queries."""
    highest = PlanVersion.objects.filter(plan=plan).aggregate(highest=Max("version_number"))[
        "highest"
    ]
    has_open = PlanVersion.objects.filter(plan=plan, status__in=OPEN_STATUSES).exists()
    return {"current_version_number": highest, "plan_has_open_version": has_open}


def is_bu_lead_for(user, cost_code) -> bool:
    """Is this user the BU lead of this cost code?

    The schema has no foreign key from a login to a `bu_leads` row; the link
    the legacy data offers is the BU lead's email. So: the cost code's BU lead
    email must match the user's login email or their employee record's email
    (case-insensitively). Confirmed rule: only the cost code's own BU lead
    approves, administrators have full access. Email rather than name because
    names are neither unique nor stable.
    """
    lead = getattr(cost_code, "bu_lead", None)
    lead_email = (getattr(lead, "email", "") or "").strip().lower()
    if not lead_email:
        return False
    candidates = {(user.email or "").strip().lower()}
    employee = getattr(user, "employee", None)
    if employee is not None and employee.email:
        candidates.add(employee.email.strip().lower())
    return lead_email in candidates


def caller_may_approve(scope, version: PlanVersion) -> bool:
    """May the caller approve or send back this version (journey step 7)?

    Needs an approval role and a claim: administrators always; a BU lead or
    approver only for cost codes whose BU lead they are. A BU lead granted an
    estate still reviews only their own plans.
    """
    if scope.has_role(RoleCode.ADMIN):
        return True
    if not scope.has_role(*APPROVAL_ROLES):
        return False
    return is_bu_lead_for(scope.user, version.plan.cost_code)
