"""
Role codes — the permission primitive (AD-3).

These strings are referenced by permission classes and by the scoping mixin, so
they are constants here rather than string literals scattered through views. They
must match the `roles.role_code` values seeded by `seed_reference_data`.
"""

from django.db import models


class RoleCode(models.TextChoices):
    ADMIN = "BCM_ADMIN", "BCM Administrator"
    COORDINATOR = "BCM_COORDINATOR", "BCM Coordinator"
    REVIEWER = "BCM_REVIEWER", "BCM Reviewer"
    VIEWER = "BCM_VIEWER", "BCM Viewer"
    APPROVER = "BCM_APPROVER", "BCM Approver"
    BU_LEAD = "BCM_BU_LEAD", "Business Unit Lead"
    RISK_OWNER = "BCM_RISK_OWNER", "Risk Owner"
    TEST_MANAGER = "BCM_TEST_MANAGER", "Test Manager"
    DOCUMENT_CONTROLLER = "BCM_DOCUMENT_CONTROLLER", "Document Controller"
    AUDITOR = "BCM_AUDITOR", "Auditor"


#: Roles that see every estate without needing a `user_estate_scopes` row.
#: Deliberately narrow — widening this silently widens data access everywhere,
#: because the scoping mixin short-circuits for these roles.
ALL_ESTATE_ROLES = frozenset({RoleCode.ADMIN, RoleCode.AUDITOR})

#: Roles permitted to approve or send back a plan version (journey step 7).
APPROVAL_ROLES = frozenset({RoleCode.BU_LEAD, RoleCode.APPROVER, RoleCode.ADMIN})

#: Roles permitted to author plan content (journey step 5).
AUTHORING_ROLES = frozenset({RoleCode.COORDINATOR, RoleCode.ADMIN})

#: Roles that may only read.
READ_ONLY_ROLES = frozenset({RoleCode.VIEWER, RoleCode.AUDITOR})

#: Roles that see only the cost codes they have a claim on — a coordinator the
#: ones they are assigned to, a BU lead / approver the ones they lead — rather
#: than every cost code in their estates. See `ScopeResolver.narrowed_to_own`.
OWN_RECORD_ROLES = frozenset({RoleCode.COORDINATOR, RoleCode.BU_LEAD, RoleCode.APPROVER})

#: Roles whose estate grant alone is a reason to read every plan in the estate.
#: Holding one of these alongside an own-record role widens back to the estate.
ESTATE_WIDE_ROLES = frozenset(
    {
        RoleCode.ADMIN,
        RoleCode.AUDITOR,
        RoleCode.VIEWER,
        RoleCode.REVIEWER,
        RoleCode.RISK_OWNER,
        RoleCode.TEST_MANAGER,
        RoleCode.DOCUMENT_CONTROLLER,
    }
)
