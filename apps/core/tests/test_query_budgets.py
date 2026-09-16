"""
Query budgets for the list and dashboard endpoints (Phase 10.2).

Each test loads the endpoint with a small population and again with a larger
one and asserts the query count does not grow with the rows. A per-row query
(an N+1) fails here before it fails in production.
"""

import datetime as dt

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from apps.crisis.models import CmscMember, CrisisEvent
from apps.exemptions.models import Exemption
from apps.helpcenter.models import HelpResource
from apps.organization.models import CostCode
from apps.plans.models import CoordinatorAssignment, Plan, PlanStatus, PlanVersion
from apps.risk.models import Risk, RiskAction
from apps.testing.models import Test, TestOutcome

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


def count_queries(client, url, params=None):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url, params or {})
    assert response.status_code == 200, getattr(response, "data", response.content)
    return len(ctx.captured_queries)


def add_cost_codes(org, actor, employee, n, *, start):
    """n more cost codes in Alpha, each with an approved version, a coordinator, a test, a risk and an event."""
    for i in range(start, start + n):
        cc = CostCode.objects.create(
            cost_code=f"CC-2{i:03d}",
            estate=org["estate"],
            process=org["process"],
            region=org["region"],
            bu_lead=org["bu_lead"],
        )
        plan = Plan.objects.create(cost_code=cc, process=org["process"])
        version = PlanVersion.objects.create(
            plan=plan,
            version_number=1,
            status=PlanStatus.APPROVED,
            created_by=actor,
            approved_by=actor,
        )
        CoordinatorAssignment.objects.create(
            plan_version=version,
            employee=employee,
            coordinator_type="Primary",
            estate=org["estate"],
        )
        test = Test.objects.create(
            plan_version=version,
            test_type="Walkthrough",
            scheduled_date=dt.date(2026, 10, 1),
            status="Completed",
            initiated_by=actor,
        )
        TestOutcome.objects.create(
            test=test, conducted_date=dt.date(2026, 10, 1), final_status="Passed"
        )
        risk = Risk.objects.create(
            plan_version=version,
            risk_name=f"Risk {i}",
            likelihood_rating=2,
            severity_rating=2,
            control_effectiveness_rating=1,
            inherent_risk_score=4,
            residual_risk_score=3,
            risk_level="Low",
        )
        RiskAction.objects.create(
            risk=risk, action_type="MITIGATION", status="Open", target_date=dt.date(2026, 1, 1)
        )
        Exemption.objects.create(
            plan_version=version, status="Pending", reason="x", requested_by=actor
        )
        CrisisEvent.objects.create(
            cost_code=cc, event_type="Table Top", status="Planned", created_by=actor
        )
        CmscMember.objects.create(
            cost_code=cc, member_name=f"M{i}", member_email=f"m{i}@example.com"
        )


def assert_flat(client, url, org, actor, employee, *, params=None, ceiling):
    add_cost_codes(org, actor, employee, 3, start=0)
    small = count_queries(client, url, params)
    add_cost_codes(org, actor, employee, 6, start=10)
    large = count_queries(client, url, params)
    # Not "equal": warm caches (roles, lookups) can make the second call cheaper.
    assert large <= small, f"{url}: {small} queries for 3 rows, {large} for 9"
    assert small <= ceiling, f"{url}: {small} queries, ceiling {ceiling}"


class TestBudgets:
    def test_dashboard(self, org, actor, employee, approved_version, admin_client):
        assert_flat(admin_client, reverse("reporting:dashboard"), org, actor, employee, ceiling=40)

    def test_tests_list_as_coordinator(
        self, org, actor, employee, approved_version, coordinator_client
    ):
        assert_flat(
            coordinator_client, reverse("testing:test-list"), org, actor, employee, ceiling=14
        )

    def test_crisis_events_as_coordinator(
        self, org, actor, employee, approved_version, coordinator_client
    ):
        assert_flat(
            coordinator_client, reverse("crisis:event-list"), org, actor, employee, ceiling=12
        )

    def test_review_queue_as_admin(self, org, actor, employee, approved_version, admin_client):
        for i in range(3):
            PlanVersion.objects.filter(plan__cost_code__cost_code=f"CC-2{i:03d}").update(
                status=PlanStatus.PENDING_BU_LEAD_REVIEW
            )
        assert_flat(admin_client, reverse("plans:review-queue"), org, actor, employee, ceiling=12)

    def test_help_library(
        self, org, actor, employee, approved_version, coordinator_client, user_factory
    ):
        from apps.documents.models import EntityDocument
        from apps.documents.uploads import attach, store_upload

        def add(n, start):
            for i in range(start, start + n):
                document = store_upload(
                    SimpleUploadedFile(f"g{i}.pdf", f"%PDF {i}".encode()), actor=actor
                )
                resource = HelpResource.objects.create(
                    title=f"Guide {i}", category="Guides", document=document, created_by=actor
                )
                attach(
                    document,
                    entity_type=EntityDocument.EntityType.HELP_RESOURCE,
                    entity_id=resource.pk,
                    document_type="HELP_DOCUMENT",
                )

        url = reverse("helpcenter:help-list")
        add(3, 0)
        small = count_queries(coordinator_client, url)
        add(6, 10)
        assert count_queries(coordinator_client, url) <= small <= 8

    def test_report_requests(self, org, actor, employee, approved_version, coordinator_client):
        from apps.reporting import services

        url = reverse("reporting:report-requests")

        def add(n):
            for _ in range(n):
                services.create_request(
                    user=actor,
                    report_type="EXEMPTION_REGISTER",
                    report_format="csv",
                    parameters={},
                    schedule="ONCE",
                )

        add(3)
        small = count_queries(coordinator_client, url)
        add(6)
        assert count_queries(coordinator_client, url) <= small <= 8

    def test_cost_code_list_stays_flat(
        self, org, actor, employee, approved_version, coordinator_client
    ):
        url = reverse("organization:estate-cost-codes", args=[org["estate"].pk])
        assert_flat(coordinator_client, url, org, actor, employee, ceiling=8)

    def test_call_tree_run_detail(self, org, actor, approved_version, coordinator_client):
        from apps.calltree import engine

        for i in range(6):
            CmscMember.objects.create(
                cost_code=org["cost_code"],
                member_name=f"P{i}",
                member_email=f"p{i}@example.com",
                phone_number=f"900000000{i}",
            )
        run = engine.start_run(
            cost_code=org["cost_code"],
            initiated_by=actor,
            simulation=True,
            call_tree_type="Call tree",
        )
        url = reverse("calltree:run-detail", args=[run.pk])
        assert count_queries(coordinator_client, url) <= 10


class TestUnauthenticated:
    def test_every_api_route_refuses_anonymous_callers(self):
        """Walk the URL map: nothing under /api/v1/ answers an anonymous GET except the allowlist."""
        from django.urls import URLPattern, URLResolver, get_resolver

        allow = {
            "health/",
            "ready/",
            "schema/",
            "docs/",
            "auth/login/",
            "auth/refresh/",
            "auth/register/",
            "auth/password-reset/",
            "auth/password-reset/confirm/",
            "auth/otp/verify/",
            "auth/otp/resend/",
            "webhooks/twilio/twiml/",
            "auth/sso/",
            "metrics/",
            "redoc/",
        }

        def walk(patterns, prefix=""):
            for entry in patterns:
                if isinstance(entry, URLResolver):
                    yield from walk(entry.url_patterns, prefix + str(entry.pattern))
                elif isinstance(entry, URLPattern):
                    yield prefix + str(entry.pattern)

        client = APIClient()
        offenders = []
        for route in walk(get_resolver().url_patterns):
            if not route.startswith("api/v1/"):
                continue
            path = route.removeprefix("api/v1/")
            if any(path.startswith(a) for a in allow) or path.startswith("auth/"):
                continue
            concrete = "/api/v1/" + path
            for token in ("<int:", "<str:", "<slug:", "<uuid:", "<"):
                while token in concrete:
                    start = concrete.index(token)
                    end = concrete.index(">", start)
                    concrete = concrete[:start] + "1" + concrete[end + 1 :]
            response = client.get(concrete)
            if response.status_code not in (401, 403, 404, 405, 410):
                offenders.append((concrete, response.status_code))
        assert offenders == []
