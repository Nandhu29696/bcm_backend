"""
Dashboard reconciliation (Phase 9 exit criterion): every headline figure is
recomputed here with hand-written SQL over the same tables and must match
the ORM-built payload exactly.
"""

import datetime as dt

import pytest
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import UserEstateScope
from apps.accounts.scoping import ScopeResolver
from apps.calltree.models import CallAttempt, CallTreeMember, CallTreeRun, RunStatus
from apps.exemptions.models import Exemption, ExemptionStatus
from apps.organization.models import CostCode, Lob
from apps.plans.models import Plan, PlanStatus, PlanVersion
from apps.reporting.metrics import build_dashboard
from apps.risk.models import Risk, RiskAction
from apps.testing.models import Test, TestOutcome

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 9, 14)


def sql(query, params=()):
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def scalar(query, params=()):
    return sql(query, params)[0][0]


@pytest.fixture
def population(org, actor, employee, approved_version):
    """A spread of cost codes, versions, risks, actions, tests, runs and exemptions across two estates."""
    lob = Lob.objects.create(lob_name="Operations")
    CostCode.objects.filter(pk=org["cost_code"].pk).update(lob=lob)
    # cost_code (Alpha): approved v1 (fixture) with risk + action; add a WIP v2 -> current is WIP.
    v2 = PlanVersion.objects.create(
        plan=approved_version.plan,
        version_number=2,
        status=PlanStatus.WORK_IN_PROGRESS,
        created_by=actor,
    )
    risk = Risk.objects.create(
        plan_version=v2,
        risk_name="Power",
        likelihood_rating=3,
        severity_rating=2,
        control_effectiveness_rating=1,
        inherent_risk_score=6,
        residual_risk_score=5,
        risk_level="Moderate",
        owner_employee=employee,
    )
    RiskAction.objects.create(
        risk=risk, action_type="MITIGATION", status="Open", target_date=TODAY - dt.timedelta(days=3)
    )
    RiskAction.objects.create(
        risk=risk,
        action_type="CONTINGENCY",
        status="Mitigated",
        target_date=TODAY - dt.timedelta(days=30),
    )
    RiskAction.objects.create(
        risk=risk,
        action_type="MITIGATION",
        status="Open",
        target_date=TODAY + dt.timedelta(days=10),
    )
    Risk.objects.create(
        plan_version=v2,
        risk_name="Flood",
        likelihood_rating=1,
        severity_rating=3,
        control_effectiveness_rating=2,
        inherent_risk_score=3,
        residual_risk_score=1,
        risk_level="Low",
    )
    Exemption.objects.create(
        plan_version=v2, status=ExemptionStatus.REJECTED, reason="No", requested_by=actor
    )

    # Two more cost codes in Alpha: one approved, one with no plan at all.
    cc2 = CostCode.objects.create(
        cost_code="CC-1002",
        estate=org["estate"],
        process=org["process"],
        region=org["region"],
        lob=lob,
    )
    plan2 = Plan.objects.create(cost_code=cc2, process=org["process"])
    approved2 = PlanVersion.objects.create(
        plan=plan2,
        version_number=1,
        status=PlanStatus.APPROVED,
        created_by=actor,
        approved_by=actor,
    )
    CostCode.objects.create(cost_code="CC-1003", estate=org["estate"], process=org["process"])

    # Tests: one completed recently on cc2, one scheduled ahead, one old completed on cost_code (older than 12 months).
    done = Test.objects.create(
        plan_version=approved2,
        test_type="Tabletop Exercise",
        scheduled_date=TODAY - dt.timedelta(days=5),
        status="Completed",
    )
    TestOutcome.objects.create(
        test=done, conducted_date=TODAY - dt.timedelta(days=5), final_status="Passed"
    )
    Test.objects.create(
        plan_version=approved2,
        test_type="Walkthrough",
        scheduled_date=TODAY + dt.timedelta(days=9),
        status="Scheduled",
    )
    old = Test.objects.create(
        plan_version=approved_version,
        test_type="Call Tree Test",
        scheduled_date=TODAY - dt.timedelta(days=400),
        status="Completed",
    )
    TestOutcome.objects.create(
        test=old, conducted_date=TODAY - dt.timedelta(days=400), final_status="Partial"
    )

    # Call tree: one live run 2/3 reached, one simulation run.
    live = CallTreeRun.objects.create(
        cost_code=org["cost_code"],
        status=RunStatus.COMPLETED,
        simulation_flag=False,
        started_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    for name, reached, channel in (("a", True, "VOICE"), ("b", True, "EMAIL"), ("c", False, "")):
        m = CallTreeMember.objects.create(
            call_tree_run=live, member_name=name, reached_flag=reached, reached_channel=channel
        )
        CallAttempt.objects.create(
            call_tree_member=m, channel="VOICE", attempt_number=1, attempt_status="x"
        )
    sim = CallTreeRun.objects.create(
        cost_code=cc2,
        status=RunStatus.COMPLETED,
        simulation_flag=True,
        started_at=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
    )
    CallTreeMember.objects.create(
        call_tree_run=sim, member_name="s", reached_flag=True, reached_channel="VOICE"
    )

    # Beta estate (out of the coordinator's scope): one approved cost code with a risk.
    other_plan = Plan.objects.create(cost_code=org["other_cost_code"], process=org["process"])
    other_v = PlanVersion.objects.create(
        plan=other_plan, version_number=1, status=PlanStatus.APPROVED, created_by=actor
    )
    Risk.objects.create(
        plan_version=other_v,
        risk_name="Other",
        likelihood_rating=3,
        severity_rating=3,
        control_effectiveness_rating=1,
        inherent_risk_score=9,
        residual_risk_score=8,
        risk_level="High",
    )
    Exemption.objects.create(
        plan_version=other_v, status=ExemptionStatus.PENDING, reason="Later", requested_by=actor
    )
    return {"v2": v2, "cc2": cc2}


CURRENT_VERSION_SQL = """
    SELECT pv.plan_version_id, pv.status, cc.cost_code_id, cc.estate_id
    FROM cost_codes cc
    JOIN plans p ON p.cost_code_id = cc.cost_code_id AND p.active_flag = 1
    JOIN plan_versions pv ON pv.plan_id = p.plan_id
    WHERE cc.active_flag = 1
      AND pv.plan_version_id = (
        SELECT pv2.plan_version_id FROM plan_versions pv2
        JOIN plans p2 ON p2.plan_id = pv2.plan_id
        WHERE p2.cost_code_id = cc.cost_code_id AND p2.active_flag = 1
        ORDER BY pv2.version_number DESC, pv2.plan_version_id DESC LIMIT 1)
"""


class TestReconciliation:
    def test_status_totals_match_sql_for_admin(self, population, user_factory):
        admin = user_factory(email="dash-admin@example.com", roles=["BCM_ADMIN"])
        data = build_dashboard(ScopeResolver(admin), today=TODAY)

        total = scalar("SELECT COUNT(*) FROM cost_codes WHERE active_flag = 1")
        assert data["totals"]["cost_codes"] == total == 4
        by_status = dict(
            sql(
                """
                SELECT COALESCE(cur.status, 'Not Started') AS s, COUNT(*)
                FROM cost_codes cc
                LEFT JOIN ("""
                + CURRENT_VERSION_SQL
                + """) cur ON cur.cost_code_id = cc.cost_code_id
                WHERE cc.active_flag = 1
                GROUP BY s
                """
            )
        )
        for status, count in data["totals"]["by_status"].items():
            assert count == by_status.get(status, 0), status
        assert data["totals"]["by_status"] == {
            "Not Started": 1,
            "Work in Progress": 1,
            "Pending BU Lead Review": 0,
            "Approved": 2,
            "Rework": 0,
            "Exempted": 0,
        }

    def test_status_by_estate_and_lob_match_sql(self, population, user_factory):
        admin = user_factory(email="dash-admin@example.com", roles=["BCM_ADMIN"])
        data = build_dashboard(ScopeResolver(admin), today=TODAY)
        for row in data["status_by_estate"]:
            approved = scalar(
                "SELECT COUNT(*) FROM ("
                + CURRENT_VERSION_SQL
                + ") cur WHERE cur.estate_id = %s AND cur.status = 'Approved'",
                [row["id"]],
            )
            assert row["Approved"] == approved, row["name"]
            assert row["total"] == scalar(
                "SELECT COUNT(*) FROM cost_codes WHERE active_flag = 1 AND estate_id = %s",
                [row["id"]],
            )
        lob_rows = {r["name"]: r for r in data["status_by_lob"]}
        assert lob_rows["Operations"]["total"] == 2 and lob_rows["Unassigned"]["total"] == 2

    def test_risk_actions_tests_and_exemptions_match_sql(self, population, user_factory):
        admin = user_factory(email="dash-admin@example.com", roles=["BCM_ADMIN"])
        data = build_dashboard(ScopeResolver(admin), today=TODAY)

        risks = scalar(
            "SELECT COUNT(*) FROM risks r WHERE r.plan_version_id IN (SELECT plan_version_id FROM ("
            + CURRENT_VERSION_SQL
            + ") cur)"
        )
        assert data["risk"]["total"] == risks == 3
        overdue = scalar(
            """
            SELECT COUNT(*) FROM risk_actions ra JOIN risks r ON r.risk_id = ra.risk_id
            WHERE r.plan_version_id IN (SELECT plan_version_id FROM ("""
            + CURRENT_VERSION_SQL
            + """) cur)
              AND ra.target_date < %s AND ra.status NOT IN ('Mitigated', 'Contingency Plan Created')
            """,
            [TODAY],
        )
        assert data["risk"]["overdue_actions"] == overdue == 1
        assert data["risk"]["open_actions"] == 2
        assert data["risk"]["overdue"][0]["days_overdue"] == 3
        heat = data["risk"]["heat_map"]
        assert sum(sum(row) for row in heat["cells"]) == 3
        assert heat["cells"][2][1] == 1  # likelihood 3, severity 2
        assert data["risk"]["by_level"] == {"Low": 1, "Moderate": 1, "High": 1}

        tested = scalar(
            """
            SELECT COUNT(DISTINCT p.cost_code_id) FROM tests t
            JOIN test_outcomes o ON o.test_id = t.test_id
            JOIN plan_versions pv ON pv.plan_version_id = t.plan_version_id
            JOIN plans p ON p.plan_id = pv.plan_id
            WHERE t.status = 'Completed' AND o.conducted_date >= %s
            """,
            [TODAY - dt.timedelta(days=360)],
        )
        assert data["tests"]["coverage"]["tested_cost_codes"] == tested == 1
        assert data["tests"]["coverage"]["percent"] == 25.0
        assert (
            data["tests"]["by_status"]["Scheduled"] == 1
            and data["tests"]["by_status"]["Completed"] == 2
        )
        assert [u["test_type"] for u in data["tests"]["upcoming"]] == ["Walkthrough"]

        reached, members = sql(
            "SELECT SUM(m.reached_flag), COUNT(*) FROM call_tree_members m JOIN call_tree_runs r ON r.call_tree_run_id = m.call_tree_run_id "
            "WHERE r.simulation_flag = 0 AND r.status = 'COMPLETED'"
        )[0]
        assert (
            (data["call_tree"]["reached"], data["call_tree"]["members"])
            == (int(reached), members)
            == (2, 3)
        )
        assert data["call_tree"]["response_rate"] == 0.667
        assert data["call_tree"]["simulation_runs"] == 1
        assert data["call_tree"]["reached_by_channel"] == {"VOICE": 1, "MS_TEAMS": 0, "EMAIL": 1}

        exemptions = dict(
            sql(
                "SELECT e.status, COUNT(*) FROM exemptions e WHERE e.plan_version_id IN (SELECT plan_version_id FROM ("
                + CURRENT_VERSION_SQL
                + ") cur) GROUP BY e.status"
            )
        )
        assert data["exemptions"]["by_status"]["Rejected"] == exemptions["Rejected"] == 1
        assert data["exemptions"]["by_status"]["Pending"] == exemptions["Pending"] == 1

    def test_completion_counts_sections_on_open_current_versions(
        self, population, user_factory, question
    ):
        from apps.assessments.models import SectionStatus, SectionStatusValue

        SectionStatus.objects.create(
            plan_version=population["v2"],
            section=question.section,
            status=SectionStatusValue.COMPLETED,
        )
        admin = user_factory(email="dash-admin@example.com", roles=["BCM_ADMIN"])
        data = build_dashboard(ScopeResolver(admin), today=TODAY)
        from apps.questionnaire.models import Section

        # Other suites may leave the real questionnaire seeded; count what is there.
        sections = Section.objects.count()
        assert data["completion"] == {
            "open_versions": 1,
            "sections_total": sections,
            "sections_completed": 1,
            "percent": round(100 / sections, 1),
        }

    def test_scope_narrows_every_figure(self, population, actor, org):
        UserEstateScope.objects.create(user=actor, estate=org["estate"])
        data = build_dashboard(ScopeResolver(actor), today=TODAY)
        assert data["totals"]["cost_codes"] == 3
        assert [r["name"] for r in data["status_by_estate"]] == ["Alpha Estate"]
        assert data["risk"]["total"] == 2 and data["risk"]["by_level"]["High"] == 0
        assert data["exemptions"]["by_status"]["Pending"] == 0
        filtered = build_dashboard(
            ScopeResolver(actor), estate_id=org["other_estate"].pk, today=TODAY
        )
        assert filtered["totals"]["cost_codes"] == 0

    def test_dashboard_endpoint(self, population, coordinator_client, org):
        response = coordinator_client.get(
            reverse("reporting:dashboard"), {"estate": org["estate"].pk}
        )
        assert response.status_code == 200
        assert (
            response.data["estate_id"] == org["estate"].pk
            and response.data["totals"]["cost_codes"] == 3
        )
        # An estate outside scope is ignored, not leaked.
        outside = coordinator_client.get(
            reverse("reporting:dashboard"), {"estate": org["other_estate"].pk}
        )
        assert outside.data["estate_id"] is None and outside.data["totals"]["cost_codes"] == 3

    def test_empty_scope_is_all_zeros(self, population, user_factory):
        nobody = user_factory(email="nobody@example.com", roles=["BCM_VIEWER"])
        client = APIClient()
        client.force_authenticate(user=nobody)
        data = client.get(reverse("reporting:dashboard")).data
        assert data["totals"]["cost_codes"] == 0 and data["totals"]["approval_rate"] is None
        assert data["risk"]["total"] == 0 and data["tests"]["coverage"]["percent"] is None
