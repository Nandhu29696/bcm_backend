"""Exemption requests and decisions (Phase 7.5)."""

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import UserEstateScope
from apps.exemptions.models import Exemption, ExemptionStatus
from apps.plans.models import PlanStatus, PlanStatusHistory, PlanVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def open_version(org, actor, employee):
    from apps.plans.models import CoordinatorAssignment, Plan

    plan = Plan.objects.create(cost_code=org["cost_code"], process=org["process"])
    version = PlanVersion.objects.create(
        plan=plan, version_number=1, status=PlanStatus.NOT_STARTED, created_by=actor
    )
    CoordinatorAssignment.objects.create(
        plan_version=version, employee=employee, coordinator_type="Primary", estate=org["estate"]
    )
    return version


@pytest.fixture
def bu_lead_client(user_factory, org):
    user = user_factory(email="priya@example.com", roles=["BCM_BU_LEAD"], display_name="Priya Lead")
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def other_lead_client(user_factory, org):
    # Viewer as well, so the version is visible and the decision gate is what fails.
    user = user_factory(email="other.lead@example.com", roles=["BCM_BU_LEAD", "BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def list_url(version):
    return reverse("exemptions:version-exemptions", args=[version.plan_version_id])


def decision_url(name, exemption_id):
    return reverse(f"exemptions:exemption-{name}", args=[exemption_id])


@pytest.fixture
def requested(coordinator_client, open_version):
    mail.outbox.clear()
    response = coordinator_client.post(
        list_url(open_version),
        {"reason": "Process is being decommissioned in Q4.", "answer_1": "No clients affected."},
    )
    assert response.status_code == 201, response.data
    return Exemption.objects.get(pk=response.data["exemption_id"])


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


def test_request_creates_a_pending_exemption_and_mails_the_lead(requested, open_version):
    assert requested.status == ExemptionStatus.PENDING
    assert requested.answer_1 == "No clients affected."
    assert [c.comment for c in requested.comments.all()] == [
        "Process is being decommissioned in Q4."
    ]
    assert [(m.to, m.cc) for m in mail.outbox] == [
        (["priya@example.com"], ["arun.coordinator@example.com"])
    ]
    assert "decommissioned" in mail.outbox[0].body


def test_a_reason_is_required(coordinator_client, open_version):
    response = coordinator_client.post(list_url(open_version), {"reason": "  "})
    assert response.status_code == 400


def test_only_one_open_request_per_version(coordinator_client, requested, open_version):
    response = coordinator_client.post(list_url(open_version), {"reason": "Again."})
    assert response.status_code == 400
    assert response.data["code"] == "exemption_not_allowed"


def test_a_closed_version_cannot_be_exempted(coordinator_client, open_version):
    open_version.status = PlanStatus.APPROVED
    open_version.save(update_fields=["status"])
    response = coordinator_client.post(list_url(open_version), {"reason": "x"})
    assert response.status_code == 400
    assert response.data["code"] == "exemption_not_allowed"


def test_a_viewer_cannot_request(user_factory, org, open_version):
    viewer = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=viewer, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=viewer)
    assert client.post(list_url(open_version), {"reason": "x"}).status_code == 403
    assert client.get(list_url(open_version)).status_code == 200


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


def test_approval_exempts_the_plan_version_and_mails_the_requester(
    bu_lead_client, requested, open_version
):
    mail.outbox.clear()
    response = bu_lead_client.post(decision_url("approve", requested.pk), {"comment": "Agreed."})
    assert response.status_code == 200, response.data
    assert response.data["status"] == ExemptionStatus.APPROVED
    assert [c["comment_type"] for c in response.data["comments"]] == ["GENERAL", "APPROVAL"]

    open_version.refresh_from_db()
    assert open_version.status == PlanStatus.EXEMPTED
    trail = list(
        PlanStatusHistory.objects.filter(plan_version=open_version).values_list("status", flat=True)
    )
    assert trail == [PlanStatus.WORK_IN_PROGRESS, PlanStatus.EXEMPTED]  # routed through WIP

    assert mail.outbox[0].to == ["arun.coordinator@example.com"]
    assert "Approved" in mail.outbox[0].body


def test_rejection_leaves_the_plan_as_it_was(bu_lead_client, requested, open_version):
    response = bu_lead_client.post(
        decision_url("reject", requested.pk), {"comment": "Still in service."}
    )
    assert response.status_code == 200
    open_version.refresh_from_db()
    assert open_version.status == PlanStatus.NOT_STARTED


def test_rework_needs_a_comment_and_allows_resubmission(
    bu_lead_client, coordinator_client, requested
):
    assert (
        bu_lead_client.post(decision_url("rework", requested.pk), {"comment": " "}).status_code
        == 400
    )
    sent_back = bu_lead_client.post(
        decision_url("rework", requested.pk), {"comment": "Name the replacement process."}
    )
    assert sent_back.data["status"] == ExemptionStatus.REWORK

    resubmitted = coordinator_client.post(
        decision_url("resubmit", requested.pk), {"reason": "Replaced by Process X."}
    )
    assert resubmitted.status_code == 200, resubmitted.data
    assert resubmitted.data["status"] == ExemptionStatus.PENDING
    assert resubmitted.data["reason"] == "Replaced by Process X."
    assert [c["status"] for c in resubmitted.data["comments"]] == ["Pending", "Rework", "Pending"]


def test_a_decided_exemption_cannot_be_decided_again(bu_lead_client, requested):
    bu_lead_client.post(decision_url("reject", requested.pk))
    response = bu_lead_client.post(decision_url("approve", requested.pk))
    assert response.status_code == 409


def test_only_this_cost_codes_lead_or_an_admin_decides(
    coordinator_client, other_lead_client, admin_client, requested
):
    assert coordinator_client.post(decision_url("approve", requested.pk)).status_code == 403
    assert other_lead_client.post(decision_url("approve", requested.pk)).status_code == 403
    assert admin_client.post(decision_url("approve", requested.pk)).status_code == 200


def test_exemption_outside_scope_is_404(user_factory, requested):
    stranger = user_factory(email="s@example.com", roles=["BCM_BU_LEAD"])
    client = APIClient()
    client.force_authenticate(user=stranger)
    assert (
        client.get(reverse("exemptions:exemption-detail", args=[requested.pk])).status_code == 404
    )
    assert client.post(decision_url("approve", requested.pk)).status_code == 404
