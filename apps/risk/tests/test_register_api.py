"""Risk register CRUD, actions, overdue reminders and recovery strategy (5.4-5.6)."""

import datetime as dt

import pytest
from django.core import mail
from django.urls import reverse

from apps.notifications.models import NotificationLog
from apps.plans.models import PlanStatus
from apps.risk.models import Risk, RiskAction
from apps.risk.reminders import send_overdue_reminders
from apps.risk.serializers import is_overdue

pytestmark = pytest.mark.django_db


def risks_url(version):
    return reverse("risk:risk-list", args=[version.plan_version_id])


def risk_url(version, risk_id):
    return reverse("risk:risk-detail", args=[version.plan_version_id, risk_id])


def actions_url(version, risk_id):
    return reverse("risk:risk-action-list", args=[version.plan_version_id, risk_id])


def action_url(version, risk_id, action_id):
    return reverse("risk:risk-action-detail", args=[version.plan_version_id, risk_id, action_id])


def strategies_url(version):
    return reverse("risk:recovery-strategy-list", args=[version.plan_version_id])


def options_url(version):
    return reverse("risk:risk-options", args=[version.plan_version_id])


@pytest.fixture
def risk(author_client, version, owner):
    response = author_client.post(
        risks_url(version),
        {
            "risk_name": "Power failure at primary site",
            "resource_type": "Facilities",
            "owner_employee": owner.pk,
            "likelihood_rating": "2",
            "severity_rating": "3",
            "control_effectiveness_rating": "2",
            "target_closure_date": "2026-12-31",
        },
    )
    assert response.status_code == 201, response.data
    return Risk.objects.get(pk=response.data["risk_id"])


# --------------------------------------------------------------------------- #
# Scope, authoring, immutability — the three shared rules
# --------------------------------------------------------------------------- #


def test_register_requires_authentication(api_client, version):
    assert api_client.get(risks_url(version)).status_code == 401


def test_register_outside_scope_is_404(api_client, user_factory, version):
    api_client.force_authenticate(
        user=user_factory(email="s@example.com", roles=["BCM_COORDINATOR"])
    )
    assert api_client.get(risks_url(version)).status_code == 404


def test_a_viewer_can_read_but_not_write(viewer_client, version, risk):
    assert viewer_client.get(risks_url(version)).status_code == 200
    response = viewer_client.post(risks_url(version), {"risk_name": "x"})
    assert response.status_code == 403
    assert response.data["code"] == "not_an_author"


def test_an_unassigned_coordinator_cannot_write(onlooker_client, version):
    assert onlooker_client.post(risks_url(version), {"risk_name": "x"}).status_code == 403


@pytest.mark.parametrize("status", [PlanStatus.APPROVED, PlanStatus.EXEMPTED])
def test_a_closed_version_refuses_writes(author_client, version, risk, status):
    version.status = status
    version.save(update_fields=["status"])
    response = author_client.patch(risk_url(version, risk.pk), {"risk_name": "renamed"})
    assert response.status_code == 409
    assert response.data["code"] == "plan_not_editable"
    assert author_client.delete(risk_url(version, risk.pk)).status_code == 409
    assert author_client.get(risks_url(version)).status_code == 200


def test_a_risk_from_another_version_is_not_reachable(
    author_client, version, risk, estate, employee
):
    from apps.plans.models import CoordinatorAssignment, PlanVersion

    other = PlanVersion.objects.create(
        plan=version.plan, version_number=2, status=PlanStatus.WORK_IN_PROGRESS
    )
    CoordinatorAssignment.objects.create(plan_version=other, employee=employee, estate=estate)
    assert author_client.get(risk_url(other, risk.pk)).status_code == 404


# --------------------------------------------------------------------------- #
# Register rows
# --------------------------------------------------------------------------- #


def test_a_risk_row_renders_its_owner_and_labels(author_client, version, risk):
    row = author_client.get(risks_url(version)).data[0]
    assert row["owner_employee"] == {
        "id": risk.owner_employee_id,
        "name": "Sneha Risk Analyst",
        "email": "sneha@example.com",
    }
    assert row["severity_label"] == "High (3)"
    assert row["actions"] == []
    assert row["open_action_count"] == 0


def test_resource_type_must_come_from_the_catalogue(author_client, version):
    response = author_client.post(
        risks_url(version), {"risk_name": "x", "resource_type": "Unicorns"}
    )
    assert response.status_code == 400
    assert "resource_type" in response.data["field_errors"]


def test_a_blank_name_is_refused(author_client, version):
    assert author_client.post(risks_url(version), {"risk_name": "   "}).status_code == 400


def test_delete_removes_the_risk_and_its_actions(author_client, version, risk):
    author_client.post(
        actions_url(version, risk.pk), {"action_type": "MITIGATION", "description": "Fix"}
    )
    assert author_client.delete(risk_url(version, risk.pk)).status_code == 204
    assert not Risk.objects.filter(pk=risk.pk).exists()
    assert not RiskAction.objects.filter(risk_id=risk.pk).exists()


# --------------------------------------------------------------------------- #
# Actions (5.5)
# --------------------------------------------------------------------------- #


def test_an_action_is_created_under_its_risk(author_client, version, risk):
    response = author_client.post(
        actions_url(version, risk.pk),
        {
            "action_type": "MITIGATION",
            "description": "Service generators quarterly",
            "status": "In Process",
            "target_date": "2026-10-01",
        },
    )
    assert response.status_code == 201, response.data
    assert response.data["risk_id"] == risk.pk
    assert response.data["is_overdue"] is False
    row = author_client.get(risks_url(version)).data[0]
    assert row["open_action_count"] == 1


def test_action_status_must_match_its_type(author_client, version, risk):
    """Mitigation and contingency have different status catalogues."""
    response = author_client.post(
        actions_url(version, risk.pk),
        {"action_type": "CONTINGENCY", "description": "x", "status": "Mitigated"},
    )
    assert response.status_code == 400
    assert "status" in response.data["field_errors"]

    ok = author_client.post(
        actions_url(version, risk.pk),
        {"action_type": "CONTINGENCY", "description": "x", "status": "Contingency Plan Created"},
    )
    assert ok.status_code == 201


def test_overdue_is_past_target_and_not_closed(risk):
    today = dt.date(2026, 9, 14)
    open_late = RiskAction(
        risk=risk, action_type="MITIGATION", status="In Process", target_date=dt.date(2026, 9, 1)
    )
    closed_late = RiskAction(
        risk=risk, action_type="MITIGATION", status="Mitigated", target_date=dt.date(2026, 9, 1)
    )
    open_future = RiskAction(
        risk=risk, action_type="MITIGATION", status="In Process", target_date=dt.date(2026, 10, 1)
    )
    undated = RiskAction(risk=risk, action_type="MITIGATION", status="In Process")
    assert is_overdue(open_late, today) is True
    assert is_overdue(closed_late, today) is False
    assert is_overdue(open_future, today) is False
    assert is_overdue(undated, today) is False


def test_overdue_reminder_goes_to_the_risk_owner_once_per_day(author_client, version, risk):
    RiskAction.objects.create(
        risk=risk,
        action_type="MITIGATION",
        description="Service generators",
        status="In Process",
        target_date=dt.date(2026, 9, 1),
    )
    mail.outbox.clear()
    today = dt.date(2026, 9, 14)

    assert send_overdue_reminders(today) == 1
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["sneha@example.com"]
    assert "13 days" in message.body
    assert f"/plan-versions/{version.plan_version_id}" in message.body

    # Same day again: nothing.
    assert send_overdue_reminders(today) == 0
    assert len(mail.outbox) == 1
    # Next day: reminded again.
    assert send_overdue_reminders(today + dt.timedelta(days=1)) == 1


def test_an_ownerless_risk_reminds_the_coordinators(author_client, version, risk):
    risk.owner_employee = None
    risk.save(update_fields=["owner_employee"])
    RiskAction.objects.create(
        risk=risk, action_type="CONTINGENCY", status="In Progress", target_date=dt.date(2026, 9, 1)
    )
    mail.outbox.clear()

    send_overdue_reminders(dt.date(2026, 9, 14))
    assert [m.to for m in mail.outbox] == [["arun@example.com"]]


def test_closed_actions_are_not_reminded(author_client, version, risk):
    RiskAction.objects.create(
        risk=risk, action_type="MITIGATION", status="Mitigated", target_date=dt.date(2026, 9, 1)
    )
    mail.outbox.clear()
    assert send_overdue_reminders(dt.date(2026, 9, 14)) == 0
    assert NotificationLog.objects.count() == 0


def test_reminder_command_runs(author_client, version, risk):
    from django.core.management import call_command

    RiskAction.objects.create(
        risk=risk, action_type="MITIGATION", status="In Process", target_date=dt.date(2026, 9, 1)
    )
    mail.outbox.clear()
    call_command("send_risk_action_reminders", as_of="2026-09-14")
    assert len(mail.outbox) == 1


# --------------------------------------------------------------------------- #
# Recovery strategy (5.6)
# --------------------------------------------------------------------------- #


def test_strategies_come_from_the_catalogue(author_client, version, owner):
    response = author_client.post(
        strategies_url(version),
        {
            "core_strategy": "Work from Home",
            "tactical_strategy": "Intercity",
            "owner_employee": owner.pk,
        },
    )
    assert response.status_code == 201, response.data
    assert response.data["owner_employee"]["name"] == "Sneha Risk Analyst"


def test_a_made_up_strategy_is_refused(author_client, version):
    response = author_client.post(strategies_url(version), {"core_strategy": "Hope"})
    assert response.status_code == 400
    assert "core_strategy" in response.data["field_errors"]


def test_a_strategy_needs_at_least_one_choice(author_client, version):
    assert author_client.post(strategies_url(version), {}).status_code == 400


# --------------------------------------------------------------------------- #
# Options for the editor
# --------------------------------------------------------------------------- #


def test_options_payload(author_client, version):
    payload = author_client.get(options_url(version)).data
    assert [o["label"] for o in payload["ratings"]["likelihood_rating"]] == [
        "Rare (1)",
        "Possible (2)",
        "Frequent (3)",
    ]
    assert "Facilities" in payload["resource_types"]
    assert payload["action_statuses"]["MITIGATION"] == ["In Process", "Mitigated", "No Mitigation"]
    assert "Work from Home" in payload["core_strategies"]


def test_options_outside_scope_is_404(api_client, user_factory, version):
    api_client.force_authenticate(
        user=user_factory(email="s@example.com", roles=["BCM_COORDINATOR"])
    )
    assert api_client.get(options_url(version)).status_code == 404
