"""
Plan version endpoints: copy, history and coordinator assignment (3.2, 3.4, 3.5).
"""

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import Employee
from apps.core.models import AuditLog
from apps.notifications.models import NotificationEvent, NotificationLog
from apps.plans.models import CoordinatorAssignment, PlanStatus, PlanStatusHistory

pytestmark = pytest.mark.django_db


def copy_url(version):
    return reverse("plans:plan-version-copy", args=[version.plan_version_id])


def history_url(version):
    return reverse("plans:plan-version-history", args=[version.plan_version_id])


def coordinators_url(version):
    return reverse("plans:plan-version-coordinators", args=[version.plan_version_id])


def coordinator_detail_url(version, assignment):
    return reverse(
        "plans:plan-version-coordinator-detail",
        args=[version.plan_version_id, assignment.pk],
    )


def detail_url(version):
    return reverse("plans:plan-version-detail", args=[version.plan_version_id])


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


def test_a_plan_version_outside_scope_is_404(api_client, user_factory, approved_version):
    """The user has a role but no scope covering this estate."""
    stranger = user_factory(email="stranger@example.com", roles=["BCM_COORDINATOR"])
    api_client.force_authenticate(user=stranger)
    assert api_client.get(detail_url(approved_version)).status_code == 404


def test_copying_a_version_outside_scope_is_404(api_client, user_factory, approved_version):
    stranger = user_factory(email="stranger@example.com", roles=["BCM_COORDINATOR"])
    api_client.force_authenticate(user=stranger)
    assert api_client.post(copy_url(approved_version)).status_code == 404


def test_history_outside_scope_is_404(api_client, user_factory, approved_version):
    stranger = user_factory(email="stranger@example.com", roles=["BCM_COORDINATOR"])
    api_client.force_authenticate(user=stranger)
    assert api_client.get(history_url(approved_version)).status_code == 404


def test_coordinators_outside_scope_is_404(api_client, user_factory, approved_version):
    stranger = user_factory(email="stranger@example.com", roles=["BCM_COORDINATOR"])
    api_client.force_authenticate(user=stranger)
    assert api_client.get(coordinators_url(approved_version)).status_code == 404


# --------------------------------------------------------------------------- #
# Copy (3.4)
# --------------------------------------------------------------------------- #


def test_copy_returns_the_new_version(coordinator_client, approved_version):
    response = coordinator_client.post(copy_url(approved_version), {"comments": "Annual refresh"})

    assert response.status_code == 201
    assert response.data["version_number"] == 2
    assert response.data["status"] == PlanStatus.WORK_IN_PROGRESS
    assert response.data["is_current"] is True
    assert response.data["copied_flag"] is True


def test_the_copy_comment_lands_on_the_history(coordinator_client, approved_version):
    response = coordinator_client.post(copy_url(approved_version), {"comments": "Annual refresh"})
    entry = PlanStatusHistory.objects.get(plan_version_id=response.data["plan_version_id"])
    assert entry.comments == "Annual refresh"


def test_copying_an_in_flight_version_is_a_400_not_a_500(coordinator_client, approved_version):
    approved_version.status = PlanStatus.WORK_IN_PROGRESS
    approved_version.save(update_fields=["status"])

    response = coordinator_client.post(copy_url(approved_version))
    assert response.status_code == 400
    assert response.data["code"] == "source_not_copyable"


def test_a_second_copy_is_refused_with_a_readable_reason(coordinator_client, approved_version):
    coordinator_client.post(copy_url(approved_version))
    response = coordinator_client.post(copy_url(approved_version))

    assert response.status_code == 400
    assert response.data["code"] == "open_version_exists"
    assert "still open" in response.data["detail"]


def test_a_viewer_cannot_copy(api_client, user_factory, approved_version, org):
    from apps.accounts.models import UserEstateScope

    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    api_client.force_authenticate(user=viewer)

    assert api_client.post(copy_url(approved_version)).status_code == 403


# --------------------------------------------------------------------------- #
# History (3.5)
# --------------------------------------------------------------------------- #


def test_history_is_oldest_first(coordinator_client, approved_version, actor):
    PlanStatusHistory.objects.create(
        plan_version=approved_version,
        status=PlanStatus.REWORK,
        comments="Sent back.",
        changed_by=actor,
    )
    response = coordinator_client.get(history_url(approved_version))

    assert [entry["status"] for entry in response.data] == [
        PlanStatus.APPROVED,
        PlanStatus.REWORK,
    ]


def test_history_names_who_changed_it(coordinator_client, approved_version, actor):
    response = coordinator_client.get(history_url(approved_version))
    assert response.data[0]["changed_by_name"] == actor.display_name


def test_history_survives_a_deleted_actor(coordinator_client, approved_version):
    """`changed_by` is SET_NULL. An append-only trail must still render."""
    entry = PlanStatusHistory.objects.get(plan_version=approved_version)
    entry.changed_by = None
    entry.save(update_fields=["changed_by"])

    response = coordinator_client.get(history_url(approved_version))
    assert response.data[0]["changed_by_name"] == "System"


def test_history_is_scoped_to_one_version(coordinator_client, approved_version, actor):
    """The exit criterion: history matches the status trail exactly, per version."""
    from apps.plans.versioning import copy_plan_version

    copied = copy_plan_version(approved_version, actor=actor)

    original = coordinator_client.get(history_url(approved_version)).data
    new = coordinator_client.get(history_url(copied)).data

    assert [e["status"] for e in original] == [PlanStatus.APPROVED]
    assert [e["status"] for e in new] == [PlanStatus.WORK_IN_PROGRESS]


# --------------------------------------------------------------------------- #
# Coordinator assignment (3.2)
# --------------------------------------------------------------------------- #


@pytest.fixture
def other_employee(db):
    return Employee.objects.create(
        employee_number="1100099",
        full_name="Meera Analyst",
        email="meera.analyst@example.com",
    )


@pytest.fixture
def assign(coordinator_client, django_capture_on_commit_callbacks):
    """POST an assignment and actually run the post-commit email.

    The notification is dispatched from `transaction.on_commit`, so that a
    rollback cannot leave a coordinator holding mail about an assignment that
    does not exist. A pytest `django_db` test never commits, so without this the
    callback never fires — and every "no email was sent" assertion would pass
    whether or not the code was correct.
    """

    def _assign(version, employee, **extra):
        with django_capture_on_commit_callbacks(execute=True):
            return coordinator_client.post(
                coordinators_url(version), {"employee": employee.pk, **extra}
            )

    return _assign


def test_assignment_creates_the_row(assign, approved_version, other_employee):
    response = assign(approved_version, other_employee, coordinator_type="Backup")

    assert response.status_code == 201
    assert response.data["name"] == "Meera Analyst"
    assert CoordinatorAssignment.objects.filter(
        plan_version=approved_version, employee=other_employee
    ).exists()


def test_assignment_emails_the_coordinator(assign, approved_version, other_employee):
    mail.outbox.clear()
    assign(approved_version, other_employee, coordinator_type="Backup")

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["meera.analyst@example.com"]
    assert "CC-1001" in message.body
    # The link must reach the plan, not just the app.
    assert f"/plan-versions/{approved_version.plan_version_id}" in message.body


def test_assignment_is_logged(assign, approved_version, other_employee):
    assign(approved_version, other_employee)

    log = NotificationLog.objects.get(event_type=NotificationEvent.COORDINATOR_ASSIGNED)
    assert log.to_email == "meera.analyst@example.com"


def test_assignment_writes_an_audit_entry(assign, approved_version, other_employee, actor):
    assign(approved_version, other_employee)

    entry = AuditLog.objects.filter(entity_type="CoordinatorAssignment").latest("created_at")
    assert entry.actor_id == actor.pk
    assert entry.detail["employee_id"] == other_employee.pk


def test_reassigning_the_same_person_does_not_send_a_second_email(
    assign, approved_version, other_employee
):
    """Pressing Save twice must not mail someone twice."""
    assign(approved_version, other_employee)
    assert len(mail.outbox) >= 1  # the first one really did send
    mail.outbox.clear()

    response = assign(approved_version, other_employee)
    assert response.status_code == 201
    assert len(mail.outbox) == 0
    assert (
        CoordinatorAssignment.objects.filter(
            plan_version=approved_version, employee=other_employee
        ).count()
        == 1
    )


def test_a_coordinator_with_no_email_is_skipped_not_an_error(assign, approved_version):
    """A missing address is a data gap, not a reason to refuse the assignment."""
    nameless = Employee.objects.create(
        employee_number="1100100", full_name="No Email Person", email=""
    )
    mail.outbox.clear()

    response = assign(approved_version, nameless)
    assert response.status_code == 201
    assert len(mail.outbox) == 0


def test_removing_a_coordinator_soft_deletes(
    coordinator_client, assign, approved_version, other_employee
):
    created = assign(approved_version, other_employee)
    assignment = CoordinatorAssignment.objects.get(pk=created.data["coordinator_assignment_id"])

    response = coordinator_client.delete(coordinator_detail_url(approved_version, assignment))
    assert response.status_code == 204

    assignment.refresh_from_db()
    assert assignment.active_flag is False
    # The row survives — history must still show the assignment existed.
    assert CoordinatorAssignment.all_objects.filter(pk=assignment.pk).exists()


def test_reassigning_after_removal_reactivates_and_re_notifies(
    coordinator_client, assign, approved_version, other_employee
):
    created = assign(approved_version, other_employee)
    assignment = CoordinatorAssignment.objects.get(pk=created.data["coordinator_assignment_id"])
    coordinator_client.delete(coordinator_detail_url(approved_version, assignment))
    mail.outbox.clear()

    response = assign(approved_version, other_employee)

    assert response.status_code == 201
    # Being re-assigned after a removal is a real event; they are told again.
    assert len(mail.outbox) == 1
    assignment.refresh_from_db()
    assert assignment.active_flag is True
    assert (
        CoordinatorAssignment.all_objects.filter(
            plan_version=approved_version, employee=other_employee
        ).count()
        == 1
    )


def test_the_list_hides_removed_coordinators(coordinator_client, approved_version, other_employee):
    created = coordinator_client.post(
        coordinators_url(approved_version), {"employee": other_employee.pk}
    )
    assignment = CoordinatorAssignment.objects.get(pk=created.data["coordinator_assignment_id"])
    coordinator_client.delete(coordinator_detail_url(approved_version, assignment))

    names = [row["name"] for row in coordinator_client.get(coordinators_url(approved_version)).data]
    assert "Meera Analyst" not in names


def test_a_viewer_cannot_assign(api_client, user_factory, approved_version, org, other_employee):
    from apps.accounts.models import UserEstateScope

    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    api_client.force_authenticate(user=viewer)

    response = api_client.post(coordinators_url(approved_version), {"employee": other_employee.pk})
    assert response.status_code == 403


def test_a_viewer_can_read_the_coordinator_list(api_client, user_factory, approved_version, org):
    from apps.accounts.models import UserEstateScope

    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    api_client.force_authenticate(user=viewer)

    assert api_client.get(coordinators_url(approved_version)).status_code == 200
