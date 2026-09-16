"""
Submission and review (Phase 6) - journey steps 6 and 7.

Exit criteria, each its own test:
  * the full submit -> review -> rework -> resubmit -> approve cycle, with the
    correct recipients on every email and a complete, ordered history
  * a non-BU-lead cannot approve
(the retry-without-double-send criterion lives in the notifications tests)
"""

import datetime as dt

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import UserEstateScope
from apps.plans import workflow
from apps.plans.models import PlanStatus, PlanStatusHistory, PlanVersion
from apps.questionnaire.models import Question

pytestmark = pytest.mark.django_db


def url(name, version):
    return reverse(f"plans:plan-version-{name}", args=[version.plan_version_id])


# --------------------------------------------------------------------------- #
# Fixtures: a complete plan, its coordinator, and its BU lead as a login
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _question_bank(db):
    if not Question.objects.exists():
        call_command("seed_reference_data", verbosity=0)
        call_command("seed_questionnaire", verbosity=0)


@pytest.fixture
def bu_lead_user(user_factory, org):
    """A login whose email is the cost code's BU lead email."""
    user = user_factory(email="priya@example.com", roles=["BCM_BU_LEAD"], display_name="Priya Lead")
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    return user


@pytest.fixture
def bu_lead_client(bu_lead_user):
    client = APIClient()
    client.force_authenticate(user=bu_lead_user)
    return client


@pytest.fixture
def other_lead_client(user_factory, org):
    """Holds the BU lead role and estate scope, but is NOT this cost code's lead.

    Viewer as well, so the plan is visible: a BU lead sees only the cost codes
    they lead (the own-record cut), and a 404 would not prove the approval gate.
    """
    user = user_factory(email="someone.else@example.com", roles=["BCM_BU_LEAD", "BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def complete_the_plan(client, version):
    """Answer every required question so the plan can be submitted."""
    from apps.questionnaire.answers import lookup_options

    q = {x.question_code: x for x in Question.objects.all()}
    answers = {
        "BASIC-001": {"value": "NO"},
        # "Yes" would need the penalty clause uploaded as evidence.
        "BASIC-004": {"value": "NO"},
        "RTO-001": {"value": 8},
        "MBCO-001": {"value": 60},
        "RPO-001": {"value": "NO"},
        "BIA-001": {"value": lookup_options("Primary sites")[0].code},
        "BIA-002": {"value": "NO"},
        "BIA-003": {"value": "NO"},
        "BIA-005": {"value": "NO"},
        "BIA-006": {"value": "NO"},
        "MAO-001": {"value": 48},
    }
    for code, answer in answers.items():
        response = client.put(
            reverse("questionnaire:answer", args=[version.plan_version_id, q[code].pk]),
            {"answer": answer},
            format="json",
        )
        assert response.status_code == 200, (code, response.data)


@pytest.fixture
def wip_version(org, actor, employee):
    """A Work in Progress version with Arun assigned; created through the API path."""
    from apps.plans.models import CoordinatorAssignment, Plan

    plan = Plan.objects.create(cost_code=org["cost_code"], process=org["process"])
    version = PlanVersion.objects.create(
        plan=plan, version_number=1, status=PlanStatus.NOT_STARTED, created_by=actor
    )
    CoordinatorAssignment.objects.create(
        plan_version=version, employee=employee, coordinator_type="Primary", estate=org["estate"]
    )
    PlanStatusHistory.objects.create(
        plan_version=version,
        status=PlanStatus.NOT_STARTED,
        comments="Plan version created.",
        changed_by=actor,
    )
    return version


def statuses(version):
    return list(
        PlanStatusHistory.objects.filter(plan_version=version)
        .order_by("changed_at", "pk")
        .values_list("status", flat=True)
    )


def sent_to():
    return [(m.to, m.cc) for m in mail.outbox]


# --------------------------------------------------------------------------- #
# The full cycle - the first exit criterion
# --------------------------------------------------------------------------- #


def test_the_full_cycle_with_correct_recipients_and_ordered_history(
    coordinator_client, bu_lead_client, wip_version, org
):
    version = wip_version
    mail.outbox.clear()

    # First edit moves Not Started -> Work in Progress.
    complete_the_plan(coordinator_client, version)
    version.refresh_from_db()
    assert version.status == PlanStatus.WORK_IN_PROGRESS

    # Submit: TO the BU lead, CC the coordinator.
    mail.outbox.clear()
    response = coordinator_client.post(url("submit", version), {"comments": "Ready for you."})
    assert response.status_code == 200, response.data
    assert response.data["status"] == PlanStatus.PENDING_BU_LEAD_REVIEW
    assert sent_to() == [(["priya@example.com"], ["arun.coordinator@example.com"])]
    assert "Ready for you." in mail.outbox[0].body

    # Rework: TO the coordinator, CC the BU lead, comment carried.
    mail.outbox.clear()
    response = bu_lead_client.post(url("rework", version), {"comments": "RTO needs justification."})
    assert response.status_code == 200, response.data
    assert response.data["status"] == PlanStatus.REWORK
    assert response.data["is_editable"] is True
    assert sent_to() == [(["arun.coordinator@example.com"], ["priya@example.com"])]
    assert "RTO needs justification." in mail.outbox[0].body

    # Resubmit straight from Rework: passes through Work in Progress, mails the lead again.
    mail.outbox.clear()
    response = coordinator_client.post(url("submit", version))
    assert response.status_code == 200, response.data
    assert sent_to() == [(["priya@example.com"], ["arun.coordinator@example.com"])]

    # Approve: TO the coordinator, CC the lead; the version locks.
    mail.outbox.clear()
    response = bu_lead_client.post(url("approve", version), {"comments": "Good."})
    assert response.status_code == 200, response.data
    assert response.data["status"] == PlanStatus.APPROVED
    assert response.data["is_editable"] is False
    assert response.data["approved_by_name"] == "Priya Lead"
    assert sent_to() == [(["arun.coordinator@example.com"], ["priya@example.com"])]

    # The history is the complete, ordered trail of everything above.
    assert statuses(version) == [
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.REWORK,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.APPROVED,
    ]
    comments = list(
        PlanStatusHistory.objects.filter(plan_version=version)
        .order_by("changed_at", "pk")
        .values_list("comments", flat=True)
    )
    assert "RTO needs justification." in comments
    assert "Rework started." in comments


def test_approval_generates_the_documents(coordinator_client, bu_lead_client, wip_version):
    """Approve -> the Phase 7 task runs (eagerly here) and attaches docx + pdf."""
    from apps.documents.generation import documents_for_version

    complete_the_plan(coordinator_client, wip_version)
    coordinator_client.post(url("submit", wip_version))
    bu_lead_client.post(url("approve", wip_version))

    attached = list(documents_for_version(wip_version))
    assert sorted(a.document_type for a in attached) == [
        "GENERATED_PLAN_DOCX",
        "GENERATED_PLAN_PDF",
    ]


# --------------------------------------------------------------------------- #
# Submission rules
# --------------------------------------------------------------------------- #


def test_an_incomplete_plan_cannot_be_submitted_and_says_why(coordinator_client, wip_version):
    response = coordinator_client.post(url("submit", wip_version))
    assert response.status_code == 400
    assert response.data["code"] == "plan_incomplete"
    names = {s["section_name"] for s in response.data["incomplete_sections"]}
    assert "RTO" in names and "Basic Questions" in names
    wip_version.refresh_from_db()
    assert wip_version.status == PlanStatus.NOT_STARTED  # nothing moved


def test_readiness_reports_blockers(coordinator_client, wip_version):
    payload = coordinator_client.get(url("readiness", wip_version)).data
    assert payload["can_submit"] is False
    assert len(payload["incomplete_sections"]) == 6
    complete_the_plan(coordinator_client, wip_version)
    payload = coordinator_client.get(url("readiness", wip_version)).data
    assert payload["can_submit"] is True
    assert payload["incomplete_sections"] == []


def test_a_viewer_cannot_submit(viewer_client_for, wip_version):
    assert viewer_client_for.post(url("submit", wip_version)).status_code == 403


def test_a_pending_plan_is_read_only_for_the_coordinator(coordinator_client, wip_version):
    complete_the_plan(coordinator_client, wip_version)
    coordinator_client.post(url("submit", wip_version))
    q = Question.objects.get(question_code="RTO-001")
    response = coordinator_client.put(
        reverse("questionnaire:answer", args=[wip_version.plan_version_id, q.pk]),
        {"answer": {"value": 9}},
        format="json",
    )
    assert response.status_code == 409


def test_submitting_twice_is_refused(coordinator_client, wip_version):
    complete_the_plan(coordinator_client, wip_version)
    assert coordinator_client.post(url("submit", wip_version)).status_code == 200
    response = coordinator_client.post(url("submit", wip_version))
    assert response.status_code == 409
    assert response.data["code"] == "invalid_state_transition"


# --------------------------------------------------------------------------- #
# Who may approve - the second exit criterion
# --------------------------------------------------------------------------- #


@pytest.fixture
def pending_version(coordinator_client, wip_version):
    complete_the_plan(coordinator_client, wip_version)
    assert coordinator_client.post(url("submit", wip_version)).status_code == 200
    wip_version.refresh_from_db()
    return wip_version


def test_the_coordinator_cannot_approve_their_own_plan(coordinator_client, pending_version):
    response = coordinator_client.post(url("approve", pending_version))
    assert response.status_code == 403
    assert response.data["code"] == "not_an_approver"
    pending_version.refresh_from_db()
    assert pending_version.status == PlanStatus.PENDING_BU_LEAD_REVIEW


def test_a_bu_lead_of_a_different_cost_code_cannot_approve(other_lead_client, pending_version):
    """Holding the role is not enough; you must be THIS cost code's lead."""
    assert other_lead_client.post(url("approve", pending_version)).status_code == 403
    assert (
        other_lead_client.post(url("rework", pending_version), {"comments": "x"}).status_code == 403
    )


def test_an_admin_can_approve(admin_client, pending_version):
    assert admin_client.post(url("approve", pending_version)).status_code == 200


def test_rework_requires_a_comment(bu_lead_client, pending_version):
    response = bu_lead_client.post(url("rework", pending_version), {"comments": "   "})
    assert response.status_code == 400
    assert response.data["code"] == "comment_required"


def test_approving_a_plan_that_is_not_pending_is_refused(bu_lead_client, wip_version):
    response = bu_lead_client.post(url("approve", wip_version))
    assert response.status_code == 409


def test_a_double_click_on_approve_does_not_double_send(bu_lead_client, pending_version):
    mail.outbox.clear()
    assert bu_lead_client.post(url("approve", pending_version)).status_code == 200
    assert bu_lead_client.post(url("approve", pending_version)).status_code == 409
    assert len(mail.outbox) == 1


# --------------------------------------------------------------------------- #
# Review queue (6.2)
# --------------------------------------------------------------------------- #


def test_the_queue_shows_the_lead_their_pending_plans(
    bu_lead_client, other_lead_client, admin_client, pending_version
):
    mine = bu_lead_client.get(reverse("plans:review-queue")).data
    assert [row["plan_version_id"] for row in mine] == [pending_version.pk]
    assert mine[0]["can_review"] is True
    assert mine[0]["cost_code"] == "CC-1001"

    assert other_lead_client.get(reverse("plans:review-queue")).data == []
    assert len(admin_client.get(reverse("plans:review-queue")).data) == 1


def test_the_queue_shows_the_coordinator_where_their_plan_is(coordinator_client, pending_version):
    rows = coordinator_client.get(reverse("plans:review-queue")).data
    assert [r["plan_version_id"] for r in rows] == [pending_version.pk]
    assert rows[0]["can_review"] is False


# --------------------------------------------------------------------------- #
# Escalation reminders (6.7)
# --------------------------------------------------------------------------- #


def test_a_plan_pending_too_long_reminds_the_lead_once_a_day(pending_version, settings):
    from apps.plans.reminders import send_review_reminders

    settings.REVIEW_REMINDER_DAYS = 5
    PlanVersion.objects.filter(pk=pending_version.pk).update(
        updated_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    mail.outbox.clear()

    assert send_review_reminders(dt.date(2026, 9, 14)) == 1
    assert mail.outbox[0].to == ["priya@example.com"]
    assert mail.outbox[0].cc == ["arun.coordinator@example.com"]
    assert "13 days" in mail.outbox[0].body
    assert send_review_reminders(dt.date(2026, 9, 14)) == 0
    assert send_review_reminders(dt.date(2026, 9, 15)) == 1


def test_a_recently_submitted_plan_is_not_reminded(pending_version):
    from apps.plans.reminders import send_review_reminders

    mail.outbox.clear()
    assert send_review_reminders() == 0


def test_the_reminder_command_runs(pending_version):
    PlanVersion.objects.filter(pk=pending_version.pk).update(
        updated_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    mail.outbox.clear()
    call_command("send_review_reminders", as_of="2026-09-14")
    assert len(mail.outbox) == 1


# --------------------------------------------------------------------------- #
# Service-level guard: status is never set except through the workflow
# --------------------------------------------------------------------------- #


def test_transitions_follow_the_table(wip_version, actor):
    from apps.core.exceptions import InvalidStateTransition

    with pytest.raises(InvalidStateTransition):
        workflow.approve(wip_version, actor=actor)
    with pytest.raises(InvalidStateTransition):
        workflow.rework(wip_version, actor=actor, comments="x")


@pytest.fixture
def viewer_client_for(user_factory, org):
    user = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client
