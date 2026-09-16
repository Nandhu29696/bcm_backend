"""
Cost code edit and the plan version list (Phase 3.1, 3.3, 3.6) — journey step 4.
"""

import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.organization.models import Center, CostCode, Location, Process, Region, Subprocess
from apps.plans.models import Plan, PlanStatus, PlanVersion

pytestmark = pytest.mark.django_db


def detail_url(cost_code):
    return reverse("plans:cost-code-detail", args=[cost_code.cost_code_id])


def versions_url(cost_code):
    return reverse("plans:cost-code-plan-versions", args=[cost_code.cost_code_id])


# --------------------------------------------------------------------------- #
# Scope and permission
# --------------------------------------------------------------------------- #


def test_detail_requires_authentication(api_client, org):
    assert api_client.get(detail_url(org["cost_code"])).status_code == 401


def test_a_cost_code_outside_scope_is_404(coordinator_client, org):
    assert coordinator_client.get(detail_url(org["other_cost_code"])).status_code == 404


def test_detail_returns_the_full_shape(coordinator_client, org):
    response = coordinator_client.get(detail_url(org["cost_code"]))
    assert response.status_code == 200
    assert response.data["cost_code"] == "CC-1001"
    assert response.data["process"]["name"] == "Customer Support"
    assert response.data["estate"]["name"] == "Alpha Estate"


def test_a_viewer_cannot_edit(api_client, user_factory, org):
    from apps.accounts.models import UserEstateScope

    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    api_client.force_authenticate(user=viewer)

    response = api_client.patch(detail_url(org["cost_code"]), {"cost_code": "CC-NEW"})
    assert response.status_code == 403
    org["cost_code"].refresh_from_db()
    assert org["cost_code"].cost_code == "CC-1001"


def test_a_viewer_can_still_read(api_client, user_factory, org):
    from apps.accounts.models import UserEstateScope

    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    api_client.force_authenticate(user=viewer)

    assert api_client.get(detail_url(org["cost_code"])).status_code == 200


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #


def test_edit_updates_and_returns_the_detail_shape(coordinator_client, org):
    """The response must be the full shape — the drawer re-renders from it."""
    response = coordinator_client.patch(detail_url(org["cost_code"]), {"cost_code": "CC-1001-A"})
    assert response.status_code == 200
    assert response.data["cost_code"] == "CC-1001-A"
    # Untouched relations still come back labelled, not blank.
    assert response.data["process"]["name"] == "Customer Support"
    assert response.data["estate"]["name"] == "Alpha Estate"


def test_edit_writes_an_audit_entry_of_only_what_changed(coordinator_client, org, actor):
    coordinator_client.patch(detail_url(org["cost_code"]), {"cost_code": "CC-1001-B"})

    entry = AuditLog.objects.filter(entity_type="CostCode", entity_id=org["cost_code"].pk).latest(
        "created_at"
    )
    assert entry.action == AuditLog.Action.RECORD_UPDATED
    assert entry.actor_id == actor.pk
    assert entry.detail["changed"] == {"cost_code": {"from": "CC-1001", "to": "CC-1001-B"}}


def test_a_no_op_edit_writes_no_audit_entry(coordinator_client, org):
    """An audit trail full of empty saves hides the edits that mattered."""
    before = AuditLog.objects.filter(entity_type="CostCode").count()
    coordinator_client.patch(detail_url(org["cost_code"]), {"cost_code": "CC-1001"})
    assert AuditLog.objects.filter(entity_type="CostCode").count() == before


def test_the_estate_cannot_be_changed(coordinator_client, org):
    """Moving a cost code between estates is a migration, not an edit."""
    response = coordinator_client.patch(
        detail_url(org["cost_code"]), {"estate": org["other_estate"].estate_id}
    )
    assert response.status_code == 200
    org["cost_code"].refresh_from_db()
    assert org["cost_code"].estate_id == org["estate"].estate_id


def test_a_blank_cost_code_is_rejected(coordinator_client, org):
    response = coordinator_client.patch(detail_url(org["cost_code"]), {"cost_code": "  "})
    assert response.status_code == 400


def test_a_duplicate_cost_code_in_the_same_estate_is_rejected(coordinator_client, org):
    CostCode.objects.create(cost_code="CC-2002", estate=org["estate"])
    response = coordinator_client.patch(detail_url(org["cost_code"]), {"cost_code": "cc-2002"})
    assert response.status_code == 400
    assert "cost_code" in response.data["field_errors"]


def test_the_same_code_may_exist_in_another_estate(coordinator_client, org):
    CostCode.objects.create(cost_code="CC-SHARED", estate=org["other_estate"])
    response = coordinator_client.patch(detail_url(org["cost_code"]), {"cost_code": "CC-SHARED"})
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Referential validation — the combinations the schema cannot express
# --------------------------------------------------------------------------- #


def test_a_subprocess_from_another_process_is_rejected(coordinator_client, org):
    """Nothing in the schema forbids this, and the result is silently wrong."""
    other_process = Process.objects.create(process_name="Finance Operations")
    foreign = Subprocess.objects.create(subprocess_name="Accounts Payable", process=other_process)

    response = coordinator_client.patch(
        detail_url(org["cost_code"]), {"subprocess": foreign.subprocess_id}
    )
    assert response.status_code == 400
    assert "subprocess" in response.data["field_errors"]


def test_a_matching_subprocess_is_accepted(coordinator_client, org):
    sibling = Subprocess.objects.create(subprocess_name="Tier 2", process=org["process"])
    response = coordinator_client.patch(
        detail_url(org["cost_code"]), {"subprocess": sibling.subprocess_id}
    )
    assert response.status_code == 200
    assert response.data["subprocess"]["name"] == "Tier 2"


def test_changing_process_and_subprocess_together_is_validated_as_a_pair(coordinator_client, org):
    """The new subprocess is checked against the incoming process, not the stored one."""
    finance = Process.objects.create(process_name="Finance Operations")
    payables = Subprocess.objects.create(subprocess_name="Payables", process=finance)

    response = coordinator_client.patch(
        detail_url(org["cost_code"]),
        {"process": finance.process_id, "subprocess": payables.subprocess_id},
    )
    assert response.status_code == 200


def test_a_location_in_another_region_is_rejected(coordinator_client, org):
    south = Region.objects.create(region_name="South", geography="Asia")
    location = Location.objects.create(location_name="Chennai", region=south)

    response = coordinator_client.patch(
        detail_url(org["cost_code"]),
        {"region": org["region"].region_id, "location": location.location_id},
    )
    assert response.status_code == 400
    assert "location" in response.data["field_errors"]


def test_a_center_outside_the_location_is_rejected(coordinator_client, org):
    location = Location.objects.create(location_name="Bangalore", region=org["region"])
    elsewhere = Location.objects.create(location_name="Pune", region=org["region"])
    center = Center.objects.create(center_name="Pune Tech Park", location=elsewhere)

    response = coordinator_client.patch(
        detail_url(org["cost_code"]),
        {"location": location.location_id, "center": center.center_id},
    )
    assert response.status_code == 400
    assert "center" in response.data["field_errors"]


def test_clearing_a_relation_is_allowed(coordinator_client, org):
    response = coordinator_client.patch(
        detail_url(org["cost_code"]), {"subprocess": None}, format="json"
    )
    assert response.status_code == 200
    assert response.data["subprocess"] is None


# --------------------------------------------------------------------------- #
# Version list (3.3) and lazy creation (3.6)
# --------------------------------------------------------------------------- #


def test_a_cost_code_with_no_plan_lists_no_versions(coordinator_client, org):
    response = coordinator_client.get(versions_url(org["cost_code"]))
    assert response.status_code == 200
    assert response.data == {"plan_id": None, "versions": []}


def test_versions_are_listed_newest_first_with_current_marked(
    coordinator_client, approved_version, actor
):
    from apps.plans.versioning import copy_plan_version

    copy_plan_version(approved_version, actor=actor)

    response = coordinator_client.get(versions_url(approved_version.plan.cost_code))
    versions = response.data["versions"]

    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["is_current"] is True
    assert versions[1]["is_current"] is False


def test_the_current_version_is_the_highest_number_not_the_newest_row(
    coordinator_client, approved_version, actor
):
    """Derived from version_number, so a back-dated insert cannot confuse it."""
    PlanVersion.objects.create(
        plan=approved_version.plan, version_number=0, status=PlanStatus.APPROVED
    )
    response = coordinator_client.get(versions_url(approved_version.plan.cost_code))
    current = [v for v in response.data["versions"] if v["is_current"]]
    assert len(current) == 1
    assert current[0]["version_number"] == 1


def test_can_copy_is_false_while_an_open_version_exists(
    coordinator_client, approved_version, actor
):
    from apps.plans.versioning import copy_plan_version

    copy_plan_version(approved_version, actor=actor)

    response = coordinator_client.get(versions_url(approved_version.plan.cost_code))
    assert all(v["can_copy"] is False for v in response.data["versions"])


def test_the_version_list_names_its_coordinators(coordinator_client, approved_version, employee):
    response = coordinator_client.get(versions_url(approved_version.plan.cost_code))
    coordinators = response.data["versions"][0]["coordinators"]
    assert [c["name"] for c in coordinators] == ["Arun Coordinator"]


def test_posting_creates_the_plan_and_first_version(coordinator_client, org):
    response = coordinator_client.post(versions_url(org["cost_code"]))

    assert response.status_code == 201
    assert response.data["version_number"] == 1
    assert response.data["status"] == PlanStatus.NOT_STARTED
    assert Plan.objects.filter(cost_code=org["cost_code"]).exists()


def test_posting_twice_returns_the_same_version(coordinator_client, org):
    first = coordinator_client.post(versions_url(org["cost_code"]))
    second = coordinator_client.post(versions_url(org["cost_code"]))

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.data["plan_version_id"] == second.data["plan_version_id"]
    assert PlanVersion.objects.count() == 1


def test_a_cost_code_with_no_process_cannot_have_a_plan(coordinator_client, org):
    """`plans` requires a process, so this must be a stated 400, not an IntegrityError."""
    orphan = CostCode.objects.create(cost_code="CC-NOPROC", estate=org["estate"])
    response = coordinator_client.post(versions_url(orphan))
    assert response.status_code == 400
    assert response.data["code"] == "cost_code_has_no_process"


def test_a_viewer_cannot_create_a_plan_version(api_client, user_factory, org):
    from apps.accounts.models import UserEstateScope

    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    api_client.force_authenticate(user=viewer)

    assert api_client.post(versions_url(org["cost_code"])).status_code == 403
