"""Test scheduling, outcomes, reports and reminders (Phase 8)."""

import datetime as dt

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.crisis.models import CmscMember
from apps.documents.models import Document
from apps.notifications.models import NotificationLog
from apps.testing.models import Test
from apps.testing.reminders import send_test_reminders

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def test_manager_client(user_factory, org):
    from apps.accounts.models import UserEstateScope

    user = user_factory(email="tm@example.com", roles=["BCM_TEST_MANAGER"])
    UserEstateScope.objects.create(user=user, estate=org["estate"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def schedule(client, cost_code, **overrides):
    payload = {
        "test_type": "Tabletop Exercise",
        "scheduled_date": "2026-11-05",
        "scheduled_time": "14:00",
        **overrides,
    }
    return client.post(
        reverse("testing:cost-code-tests", args=[cost_code.pk]), payload, format="json"
    )


class TestScheduling:
    def test_schedule_reschedule_cancel(self, org, approved_version, coordinator_client):
        created = schedule(coordinator_client, org["cost_code"], comments="Quarterly drill")
        assert created.status_code == 201, created.data
        body = created.data
        assert body["status"] == "Scheduled" and body["version_number"] == 1
        assert body["cost_code_label"] == "CC-1001" and body["can_manage"] is True

        detail = reverse("testing:test-detail", args=[body["test_id"]])
        moved = coordinator_client.patch(detail, {"scheduled_date": "2026-11-12"}, format="json")
        assert moved.status_code == 200 and moved.data["scheduled_date"] == "2026-11-12"

        cancelled = coordinator_client.post(reverse("testing:test-cancel", args=[body["test_id"]]))
        assert cancelled.status_code == 200 and cancelled.data["status"] == "Cancelled"
        assert coordinator_client.patch(detail, {"comments": "x"}, format="json").status_code == 409

    def test_listing_is_scoped_and_filtered_by_date_window(
        self, org, approved_version, coordinator_client, admin_client, test_manager_client
    ):
        schedule(coordinator_client, org["cost_code"], scheduled_date="2026-11-05")
        schedule(
            test_manager_client,
            org["cost_code"],
            scheduled_date="2026-12-05",
            test_type="Walkthrough",
        )
        from apps.plans.models import Plan, PlanVersion

        other = Plan.objects.create(cost_code=org["other_cost_code"], process=org["process"])
        PlanVersion.objects.create(plan=other, version_number=1, status="Not Started")
        schedule(admin_client, org["other_cost_code"], scheduled_date="2026-11-20")

        november = coordinator_client.get(
            reverse("testing:test-list"), {"date_from": "2026-11-01", "date_to": "2026-11-30"}
        )
        assert [t["cost_code_label"] for t in november.data["results"]] == ["CC-1001"]
        everything = admin_client.get(reverse("testing:test-list"))
        assert len(everything.data["results"]) == 3
        walkthroughs = admin_client.get(reverse("testing:test-list"), {"test_type": "Walkthrough"})
        assert len(walkthroughs.data["results"]) == 1

    def test_viewer_cannot_schedule(self, org, approved_version, user_factory):
        from apps.accounts.models import UserEstateScope

        user = user_factory(email="viewer@example.com", roles=["BCM_VIEWER"])
        UserEstateScope.objects.create(user=user, estate=org["estate"])
        client = APIClient()
        client.force_authenticate(user=user)
        assert schedule(client, org["cost_code"]).status_code == 403


class TestOutcomes:
    def test_outcome_with_report_closes_the_test_and_the_report_downloads(
        self, org, approved_version, coordinator_client, api_client
    ):
        test_id = schedule(coordinator_client, org["cost_code"]).data["test_id"]
        report = SimpleUploadedFile(
            "drill-report.pdf", b"%PDF-1.4 fake report", content_type="application/pdf"
        )
        response = coordinator_client.post(
            reverse("testing:test-outcome", args=[test_id]),
            {
                "conducted_date": "2026-11-05",
                "conducted_time": "15:30",
                "result": "All steps completed",
                "final_status": "Passed",
                "report": report,
            },
            format="multipart",
        )
        assert response.status_code == 201, response.data
        assert response.data["status"] == "Completed"
        outcome = response.data["outcomes"][0]
        assert (
            outcome["final_status"] == "Passed"
            and outcome["report"]["file_name"] == "drill-report.pdf"
        )
        assert Document.objects.get(file_name="drill-report.pdf").storage_key.startswith("uploads/")

        link = coordinator_client.get(
            reverse("documents:document-link", args=[outcome["report"]["entity_document_id"]])
        )
        assert link.status_code == 200
        download = api_client.get(link.data["url"])
        assert download.status_code == 200
        assert b"".join(download.streaming_content) == b"%PDF-1.4 fake report"

        # Closed: no second outcome, no reschedule.
        assert (
            coordinator_client.post(
                reverse("testing:test-outcome", args=[test_id]),
                {"conducted_date": "2026-11-06", "final_status": "Failed"},
            ).status_code
            == 409
        )

    def test_report_link_is_scoped_through_the_test(
        self, org, approved_version, coordinator_client, user_factory
    ):
        test_id = schedule(coordinator_client, org["cost_code"]).data["test_id"]
        response = coordinator_client.post(
            reverse("testing:test-outcome", args=[test_id]),
            {
                "conducted_date": "2026-11-05",
                "final_status": "Partial",
                "report": SimpleUploadedFile("r.docx", b"x"),
            },
            format="multipart",
        )
        attachment_id = response.data["outcomes"][0]["report"]["entity_document_id"]
        outsider = APIClient()
        outsider.force_authenticate(
            user=user_factory(email="out@example.com", roles=["BCM_COORDINATOR"])
        )
        assert (
            outsider.get(reverse("documents:document-link", args=[attachment_id])).status_code
            == 404
        )

    def test_unacceptable_upload_is_rejected(self, org, approved_version, coordinator_client):
        test_id = schedule(coordinator_client, org["cost_code"]).data["test_id"]
        response = coordinator_client.post(
            reverse("testing:test-outcome", args=[test_id]),
            {
                "conducted_date": "2026-11-05",
                "final_status": "Passed",
                "report": SimpleUploadedFile("evil.exe", b"MZ"),
            },
            format="multipart",
        )
        assert response.status_code == 400 and response.data["code"] == "upload_rejected"
        assert Test.objects.get(pk=test_id).status == "Scheduled"


class TestCallTreeTests:
    def test_call_tree_test_runs_the_engine(self, org, approved_version, coordinator_client):
        CmscMember.objects.create(
            cost_code=org["cost_code"],
            member_name="Asha",
            member_email="asha@example.com",
            phone_number="9000000002",
        )
        test_id = schedule(coordinator_client, org["cost_code"], test_type="Call Tree Test").data[
            "test_id"
        ]
        started = coordinator_client.post(
            reverse("testing:test-start-call-tree", args=[test_id]),
            {"simulation": True},
            format="json",
        )
        assert started.status_code == 200, started.data
        assert started.data["status"] == "In Progress"
        assert started.data["call_tree_run"]["status"] == "COMPLETED"
        assert started.data["call_tree_run"]["call_tree_type"] == "Call Tree Test"

    def test_only_a_call_tree_test_can_start_one(self, org, approved_version, coordinator_client):
        test_id = schedule(coordinator_client, org["cost_code"], test_type="Walkthrough").data[
            "test_id"
        ]
        response = coordinator_client.post(
            reverse("testing:test-start-call-tree", args=[test_id]), {}, format="json"
        )
        assert response.status_code == 400 and response.data["code"] == "not_a_call_tree_test"


class TestReminders:
    def test_reminder_goes_to_coordinators_cc_lead_once_per_day(
        self, org, approved_version, coordinator_client, settings
    ):
        settings.TEST_REMINDER_DAYS = 2
        schedule(coordinator_client, org["cost_code"], scheduled_date="2026-11-05")
        schedule(coordinator_client, org["cost_code"], scheduled_date="2026-11-09")
        today = dt.date(2026, 11, 3)
        assert send_test_reminders(today) == 1
        log = NotificationLog.objects.get(event_type="TEST_REMINDER")
        assert (
            log.to_email == "arun.coordinator@example.com" and log.cc_emails == "priya@example.com"
        )
        assert log.context["scheduled_date"] == "2026-11-05"
        assert send_test_reminders(today) == 0
        assert NotificationLog.objects.filter(event_type="TEST_REMINDER").count() == 1
