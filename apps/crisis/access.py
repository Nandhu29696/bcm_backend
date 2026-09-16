"""
Who may run tests and crises for a cost code (Phase 8).

Journey step 8 hangs off the cost code, not a plan version: a crisis is
declared for a business unit, and its CMSC roster outlives any one plan. So
the claim is on the cost code:

    administrator                       always
    test manager                        any cost code in their estate scope
    the cost code's BU lead             (same email match as approval)
    a coordinator assigned to a version of the cost code's plan

Seeing the cost code (estate scope) is deliberately not enough, for the same
reason it is not enough to edit a plan.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.exceptions import APIException

from apps.accounts.roles import APPROVAL_ROLES, RoleCode
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.organization.models import CostCode
from apps.plans.access import is_bu_lead_for
from apps.plans.models import CoordinatorAssignment


class NotACostCodeManager(APIException):
    status_code = http_status.HTTP_403_FORBIDDEN
    default_detail = (
        "Only an assigned coordinator, the BU lead, a test manager or an administrator "
        "can do this for the cost code."
    )
    default_code = "not_a_cost_code_manager"


def caller_may_manage(scope, cost_code: CostCode) -> bool:
    if scope.has_role(RoleCode.ADMIN, RoleCode.TEST_MANAGER):
        return True
    if scope.has_role(*APPROVAL_ROLES) and is_bu_lead_for(scope.user, cost_code):
        return True
    if scope.has_role(RoleCode.COORDINATOR) and scope.employee_id is not None:
        return CoordinatorAssignment.objects.filter(
            plan_version__plan__cost_code=cost_code, employee_id=scope.employee_id, active_flag=True
        ).exists()
    return False


def manageable_cost_code_ids(scope, cost_codes) -> set[int]:
    """`caller_may_manage` for many cost codes in one query - for list serializers."""
    cost_codes = list(cost_codes)
    if scope.has_role(RoleCode.ADMIN, RoleCode.TEST_MANAGER):
        return {cc.pk for cc in cost_codes}
    allowed: set[int] = set()
    if scope.has_role(*APPROVAL_ROLES):
        allowed |= {cc.pk for cc in cost_codes if is_bu_lead_for(scope.user, cc)}
    if scope.has_role(RoleCode.COORDINATOR) and scope.employee_id is not None:
        allowed |= set(
            CoordinatorAssignment.objects.filter(
                plan_version__plan__cost_code_id__in=[cc.pk for cc in cost_codes],
                employee_id=scope.employee_id,
                active_flag=True,
            ).values_list("plan_version__plan__cost_code_id", flat=True)
        )
    return allowed


class CostCodeScopedMixin(ScopedQuerySetMixin):
    """Resolve the cost code named in the URL through the caller's estate scope."""

    estate_scope_path = "estate_id"

    def cost_code_queryset(self):
        return CostCode.objects.select_related("estate", "process", "bu_lead", "center", "region")

    def get_cost_code(self) -> CostCode:
        if not hasattr(self, "_cost_code"):
            self._cost_code = get_object_or_404(
                self.scope_queryset(self.cost_code_queryset()), pk=self.kwargs["cost_code_id"]
            )
        return self._cost_code

    def require_manager(self, cost_code: CostCode) -> None:
        if not caller_may_manage(self.get_scope(), cost_code):
            raise NotACostCodeManager()
