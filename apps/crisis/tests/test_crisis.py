"""Crisis events and the CMSC roster through the API (Phase 8)."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.calltree.models import RunStatus
from apps.crisis.models import CmscMember, CrisisEvent
from apps.notifications.models import NotificationLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def viewer_client(user_factory, org):
    from apps.accounts.models import UserEstateScope

    user = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def lead_client(user_factory, org):
    """A BU lead whose email matches the cost code's BU lead row."""
    from apps.accounts.models import UserEstateScope

    user = user_factory(email="priya@example.com", roles=["BCM_BU_LEAD"])
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class TestRoster:
    def test_coordinator_adds_edits_and_removes_a_member(
        self, org, approved_version, coordinator_client
    ):
        url = reverse("crisis:roster", args=[org["cost_code"].pk])
        listing = coordinator_client.get(url)
        assert listing.status_code == 200 and listing.data["can_manage"] is True
        assert [m["member_name"] for m in listing.data["results"]] == ["Ravi Crisis Lead"]

        created = coordinator_client.post(
            url,
            {
                "member_name": "Meera",
                "member_email": "meera@example.com",
                "country_code": "+91",
                "phone_number": "9000000001",
            },
        )
        assert created.status_code == 201
        member = CmscMember.objects.get(pk=created.data["cmsc_member_id"])
        assert (
            member.plan_version_id == approved_version.pk and member.bu_lead_id == org["bu_lead"].pk
        )

        detail = reverse("crisis:member-detail", args=[member.pk])
        edited = coordinator_client.patch(detail, {"phone_number": "9000000005"})
        assert edited.status_code == 200 and edited.data["phone_number"] == "9000000005"
        assert coordinator_client.delete(detail).status_code == 204
        assert not CmscMember.objects.filter(pk=member.pk).exists()
        assert CmscMember.all_objects.get(pk=member.pk).active_flag is False

    def test_a_member_needs_an_email_or_a_phone(self, org, approved_version, coordinator_client):
        response = coordinator_client.post(
            reverse("crisis:roster", args=[org["cost_code"].pk]), {"member_name": "Nobody"}
        )
        assert response.status_code == 400 and "member_email" in response.data["field_errors"]

    def test_viewer_sees_the_roster_but_cannot_change_it(
        self, org, approved_version, viewer_client
    ):
        url = reverse("crisis:roster", args=[org["cost_code"].pk])
        listing = viewer_client.get(url)
        assert listing.status_code == 200 and listing.data["can_manage"] is False
        refused = viewer_client.post(url, {"member_name": "X", "member_email": "x@example.com"})
        assert refused.status_code == 403 and refused.data["code"] == "not_a_cost_code_manager"

    def test_csv_upload_upserts_by_email_and_skips_padding(
        self, org, approved_version, coordinator_client
    ):
        csv = (
            "﻿ID,CMSC_Member,CMSC_Member_EmailID,Country_Code,Phone_Number,Reporting Manager's name,Reporting Manager's Email Id.,Center\n"
            "14,Ravi Crisis Lead,ravi@example.com,91,9833001207,Pravin S,pravin@example.com,Corporate Office\n"
            ",,,,,,,\n"
            "15,Shaina M,shaina@example.com,91,9833001208,Pravin S,pravin@example.com,Corporate Office\n"
            ",,,,,,,\n"
        )
        from django.core.files.uploadedfile import SimpleUploadedFile

        url = reverse("crisis:roster-upload", args=[org["cost_code"].pk])
        response = coordinator_client.post(
            url, {"file": SimpleUploadedFile("roster.csv", csv.encode("utf-8"))}, format="multipart"
        )
        assert response.status_code == 200, response.data
        assert response.data == {"created": 1, "updated": 1, "errors": []}
        ravi = CmscMember.objects.get(cost_code=org["cost_code"], member_email="ravi@example.com")
        assert (
            ravi.phone_number == "9833001207"
            and ravi.reporting_manager_email == "pravin@example.com"
        )
        assert CmscMember.objects.filter(cost_code=org["cost_code"]).count() == 2

        # Uploading the same file again changes nothing but the counters.
        again = coordinator_client.post(
            url, {"file": SimpleUploadedFile("roster.csv", csv.encode("utf-8"))}, format="multipart"
        )
        assert again.data == {"created": 0, "updated": 2, "errors": []}
        assert CmscMember.objects.filter(cost_code=org["cost_code"]).count() == 2

    def test_csv_with_a_bad_row_is_rejected_whole(self, org, approved_version, coordinator_client):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv = "member_name,member_email,phone_number\nGood,good@example.com,1\n,,\nNo Contact,,\nDupe,good@example.com,2\n"
        response = coordinator_client.post(
            reverse("crisis:roster-upload", args=[org["cost_code"].pk]),
            {"file": SimpleUploadedFile("roster.csv", csv.encode())},
            format="multipart",
        )
        assert response.status_code == 400
        assert [e["line"] for e in response.data["errors"]] == [4, 5]
        assert not CmscMember.objects.filter(member_email="good@example.com").exists()


class TestEvents:
    def test_declare_and_initiate_runs_the_call_tree_in_simulation(
        self, org, approved_version, coordinator_client
    ):
        CmscMember.objects.create(
            cost_code=org["cost_code"],
            member_name="Asha",
            member_email="asha@example.com",
            phone_number="9000000002",
        )
        url = reverse("crisis:cost-code-events", args=[org["cost_code"].pk])
        response = coordinator_client.post(
            url,
            {
                "event_type": "Call tree",
                "csd_ticket_number": "INC-42",
                "comments": "Power outage",
                "initiate": True,
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        body = response.data
        assert body["status"] == "In Progress" and body["initiated_flag"] is True
        run = body["call_tree_run"]
        assert run["status"] == RunStatus.COMPLETED and run["simulation_flag"] is True
        assert (run["members"], run["reached"]) == (2, 1)
        assert body["can_manage"] is True
        # Simulation: no email went out.
        assert not NotificationLog.objects.filter(event_type="CRISIS_INITIATED").exists()

        event = CrisisEvent.objects.get(pk=body["crisis_event_id"])
        assert event.plan_version_id == approved_version.pk and event.event_date is not None

    def test_a_planned_event_is_initiated_later_and_a_live_run_emails_the_lead(
        self, org, approved_version, coordinator_client, settings
    ):
        settings.TWILIO_ENABLED = False
        url = reverse("crisis:cost-code-events", args=[org["cost_code"].pk])
        created = coordinator_client.post(
            url,
            {"event_type": "Table Top", "event_date": "2026-10-01", "event_time": "10:30"},
            format="json",
        )
        assert (
            created.status_code == 201
            and created.data["status"] == "Planned"
            and created.data["call_tree_run"] is None
        )

        initiated = coordinator_client.post(
            reverse("crisis:event-initiate", args=[created.data["crisis_event_id"]]),
            {"simulation": False},
            format="json",
        )
        assert initiated.status_code == 200, initiated.data
        assert initiated.data["call_tree_run"]["simulation_flag"] is False
        lead_mail = NotificationLog.objects.get(event_type="CRISIS_INITIATED")
        assert lead_mail.to_email == "priya@example.com"
        assert "arun.coordinator@example.com" in lead_mail.cc_emails

        # Starting it again while nothing is running is allowed (a second drill); closing ends it.
        closed = coordinator_client.post(
            reverse("crisis:event-close", args=[created.data["crisis_event_id"]]),
            {"comments": "All accounted for"},
            format="json",
        )
        assert closed.status_code == 200 and closed.data["status"] == "Closed"
        assert "Closed: All accounted for" in closed.data["comments"]
        again = coordinator_client.post(
            reverse("crisis:event-close", args=[created.data["crisis_event_id"]]), {}, format="json"
        )
        assert again.status_code == 409

    def test_initiate_without_a_roster_is_a_clear_error(
        self, org, approved_version, coordinator_client
    ):
        CmscMember.objects.filter(cost_code=org["cost_code"]).delete()
        response = coordinator_client.post(
            reverse("crisis:cost-code-events", args=[org["cost_code"].pk]),
            {"event_type": "Call tree", "initiate": True},
            format="json",
        )
        assert response.status_code == 400 and response.data["code"] == "no_roster_members"
        assert not CrisisEvent.objects.exists()  # the whole declaration rolled back

    def test_bu_lead_may_manage_and_viewer_may_only_read(
        self, org, approved_version, lead_client, viewer_client, coordinator_client
    ):
        url = reverse("crisis:cost-code-events", args=[org["cost_code"].pk])
        assert (
            lead_client.post(url, {"event_type": "Walkthrough"}, format="json").status_code == 201
        )
        assert (
            viewer_client.post(url, {"event_type": "Walkthrough"}, format="json").status_code == 403
        )
        listing = viewer_client.get(reverse("crisis:event-list"))
        assert listing.status_code == 200 and len(listing.data["results"]) == 1
        assert listing.data["results"][0]["can_manage"] is False
        assert (
            viewer_client.post(
                reverse("crisis:event-cancel", args=[listing.data["results"][0]["crisis_event_id"]])
            ).status_code
            == 403
        )

    def test_events_are_scoped_and_filterable(
        self, org, approved_version, admin_client, coordinator_client
    ):
        from apps.plans.models import Plan, PlanVersion

        other_plan = Plan.objects.create(cost_code=org["other_cost_code"], process=org["process"])
        PlanVersion.objects.create(plan=other_plan, version_number=1, status="Not Started")
        admin_client.post(
            reverse("crisis:cost-code-events", args=[org["other_cost_code"].pk]),
            {"event_type": "Live Incident"},
            format="json",
        )
        admin_client.post(
            reverse("crisis:cost-code-events", args=[org["cost_code"].pk]),
            {"event_type": "Table Top"},
            format="json",
        )

        assert len(admin_client.get(reverse("crisis:event-list")).data["results"]) == 2
        mine = coordinator_client.get(reverse("crisis:event-list"))
        assert [e["event_type"] for e in mine.data["results"]] == ["Table Top"]
        assert (
            len(
                admin_client.get(
                    reverse("crisis:event-list"), {"event_type": "Live Incident"}
                ).data["results"]
            )
            == 1
        )
        assert (
            coordinator_client.get(
                reverse(
                    "crisis:event-detail",
                    args=[CrisisEvent.objects.get(event_type="Live Incident").pk],
                )
            ).status_code
            == 404
        )


class TestLegacyExport:
    def test_the_real_sharepoint_export_imports(self, org, approved_version, coordinator_client):
        """`tables/BCM_Crisis_Mangmnt_CMSC_Members.csv` is an actual export: 4,776 padded
        blank rows around one real member, legacy column names, dialling code without '+'."""
        from pathlib import Path

        from django.core.files.uploadedfile import SimpleUploadedFile

        export = (
            Path(__file__).resolve().parents[4] / "tables" / "BCM_Crisis_Mangmnt_CMSC_Members.csv"
        )
        response = coordinator_client.post(
            reverse("crisis:roster-upload", args=[org["cost_code"].pk]),
            {"file": SimpleUploadedFile("legacy.csv", export.read_bytes())},
            format="multipart",
        )
        assert response.status_code == 200, response.data
        assert response.data == {"created": 1, "updated": 0, "errors": []}
        member = CmscMember.objects.get(member_email="shaina.mendonca@sourcepointmortgage.com")
        assert member.member_name == "Shaina Mendonca"
        assert member.country_code == "+91" and member.phone_number == "9833001207"
        assert member.reporting_manager_email == "pravin.salunkhe@firstsource.com"
        assert member.center == "Corporate Office"

        from apps.calltree.engine import dial_number

        assert dial_number(member.country_code, member.phone_number) == "+919833001207"
        assert dial_number("91", "98330 01207") == "+919833001207"
        assert dial_number("", "+44 20 7946 0000") == "+442079460000"
