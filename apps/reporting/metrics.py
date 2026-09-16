"""
Dashboard figures (Phase 9.2).

One function, one payload, built from the caller's scope. Every figure is a
grouped query over the same base sets the list screens use - cost codes with
their resolved current status, the current version of each cost code - so the
dashboard cannot disagree with the screens. The reconciliation tests compare
each figure against a hand-written SQL statement over the same tables.

"Current version" means the newest version of the cost code's active plan,
the same rule as the cost code list (`with_current_status`). Risks, section
completion and exemptions are counted on current versions only: a risk on a
superseded version is history, not exposure.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import Count, Q
from django.utils import timezone

from apps.calltree.models import CallTreeRun, Channel, RunStatus
from apps.exemptions.models import Exemption, ExemptionStatus
from apps.organization.models import CostCode
from apps.organization.querysets import CURRENT_STATUS, with_current_status
from apps.plans.models import PlanStatus, PlanVersion
from apps.plans.versioning import OPEN_STATUSES
from apps.questionnaire.models import Section
from apps.risk.models import Risk, RiskAction
from apps.risk.scoring import LEVEL_HIGH, LEVEL_LOW, LEVEL_MODERATE, rating_options
from apps.risk.serializers import CLOSED_ACTION_STATUSES
from apps.testing.models import Test

STATUSES = [s for s, _ in PlanStatus.choices]
TEST_COVERAGE_MONTHS = 12


def scoped_cost_codes(scope, estate_id: int | None = None):
    rows = CostCode.objects.all()
    if not scope.sees_all_estates:
        rows = rows.filter(estate_id__in=scope.estate_ids)
    if estate_id is not None:
        rows = rows.filter(estate_id=estate_id)
    return with_current_status(rows)


def current_version_ids(cost_codes) -> list[int]:
    return [
        pk
        for pk in cost_codes.order_by().values_list("current_plan_version_id", flat=True)
        if pk is not None
    ]


def _status_rollup(cost_codes, *, key: str, label: str) -> list[dict]:
    """Cost codes per (dimension, resolved status), every status present."""
    rows = (
        cost_codes.order_by()
        .values(key, label, CURRENT_STATUS)
        .annotate(total=Count("cost_code_id"))
    )
    buckets: dict[tuple, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(
            (row[key], row[label]),
            {
                "id": row[key],
                "name": row[label] or "Unassigned",
                "total": 0,
                **dict.fromkeys(STATUSES, 0),
            },
        )
        bucket[row[CURRENT_STATUS]] += row["total"]
        bucket["total"] += row["total"]
    return sorted(buckets.values(), key=lambda b: (b["name"] or ""))


def _totals(cost_codes) -> dict:
    counts = dict.fromkeys(STATUSES, 0)
    for row in cost_codes.order_by().values(CURRENT_STATUS).annotate(total=Count("cost_code_id")):
        counts[row[CURRENT_STATUS]] = row["total"]
    total = sum(counts.values())
    in_flight = (
        counts[PlanStatus.WORK_IN_PROGRESS]
        + counts[PlanStatus.PENDING_BU_LEAD_REVIEW]
        + counts[PlanStatus.REWORK]
    )
    return {
        "cost_codes": total,
        "by_status": counts,
        "approved": counts[PlanStatus.APPROVED],
        "in_flight": in_flight,
        "not_started": counts[PlanStatus.NOT_STARTED],
        "exempted": counts[PlanStatus.EXEMPTED],
        "approval_rate": round(counts[PlanStatus.APPROVED] / total, 3) if total else None,
    }


def _completion(version_ids: list[int]) -> dict:
    """Section completion across open current versions."""
    open_ids = list(
        PlanVersion.objects.filter(pk__in=version_ids, status__in=OPEN_STATUSES).values_list(
            "pk", flat=True
        )
    )
    section_count = Section.objects.count()
    completed = 0
    if open_ids:
        from apps.assessments.models import SectionStatus, SectionStatusValue

        completed = SectionStatus.objects.filter(
            plan_version_id__in=open_ids, status=SectionStatusValue.COMPLETED
        ).count()
    total = len(open_ids) * section_count
    return {
        "open_versions": len(open_ids),
        "sections_total": total,
        "sections_completed": completed,
        "percent": round(100 * completed / total, 1) if total else None,
    }


def _risk(version_ids: list[int], today: dt.date) -> dict:
    risks = Risk.objects.filter(plan_version_id__in=version_ids)
    likelihood = [(float(o.points), o.label) for o in rating_options("Likelihood")]
    severity = [(float(o.points), o.label) for o in rating_options("Severity Rating")]
    cells = {
        (float(r["likelihood_rating"]), float(r["severity_rating"])): r["total"]
        for r in risks.exclude(likelihood_rating=None)
        .exclude(severity_rating=None)
        .order_by()
        .values("likelihood_rating", "severity_rating")
        .annotate(total=Count("risk_id"))
    }
    heat_map = {
        "likelihood": [{"points": p, "label": lbl} for p, lbl in likelihood],
        "severity": [{"points": p, "label": lbl} for p, lbl in severity],
        "cells": [[cells.get((lp, sp), 0) for sp, _ in severity] for lp, _ in likelihood],
    }
    by_level = {LEVEL_LOW: 0, LEVEL_MODERATE: 0, LEVEL_HIGH: 0}
    for row in risks.order_by().values("risk_level").annotate(total=Count("risk_id")):
        if row["risk_level"] in by_level:
            by_level[row["risk_level"]] = row["total"]

    actions = RiskAction.objects.filter(risk__plan_version_id__in=version_ids)
    open_actions = actions.exclude(status__in=CLOSED_ACTION_STATUSES)
    overdue = (
        open_actions.filter(target_date__lt=today)
        .select_related("risk__plan_version__plan__cost_code", "risk__owner_employee")
        .order_by("target_date")
    )
    return {
        "total": risks.count(),
        "by_level": by_level,
        "heat_map": heat_map,
        "open_actions": open_actions.count(),
        "overdue_actions": overdue.count(),
        "overdue": [
            {
                "risk_action_id": a.pk,
                "risk_name": a.risk.risk_name,
                "cost_code": a.risk.plan_version.plan.cost_code.cost_code,
                "cost_code_id": a.risk.plan_version.plan.cost_code_id,
                "plan_version_id": a.risk.plan_version_id,
                "action_type": a.action_type,
                "status": a.status,
                "owner": a.risk.owner_employee.full_name if a.risk.owner_employee_id else "",
                "target_date": a.target_date.isoformat(),
                "days_overdue": (today - a.target_date).days,
            }
            for a in overdue[:10]
        ],
    }


def _tests(cost_codes, today: dt.date) -> dict:
    cost_code_ids = list(cost_codes.order_by().values_list("cost_code_id", flat=True))
    tests = Test.objects.filter(plan_version__plan__cost_code_id__in=cost_code_ids)
    by_status = dict.fromkeys([s for s, _ in Test.Status.choices], 0)
    for row in tests.order_by().values("status").annotate(total=Count("test_id")):
        by_status[row["status"]] = row["total"]
    by_type = {t: 0 for t, _ in Test.TestType.choices}
    for row in tests.order_by().values("test_type").annotate(total=Count("test_id")):
        by_type[row["test_type"]] = row["total"]

    since = today - dt.timedelta(days=30 * TEST_COVERAGE_MONTHS)
    tested = (
        tests.filter(status=Test.Status.COMPLETED, outcomes__conducted_date__gte=since)
        .values_list("plan_version__plan__cost_code_id", flat=True)
        .distinct()
    )
    tested_count = len(set(tested))
    upcoming = (
        tests.filter(status=Test.Status.SCHEDULED, scheduled_date__gte=today)
        .select_related("plan_version__plan__cost_code")
        .order_by("scheduled_date", "scheduled_time")[:5]
    )
    return {
        "by_status": by_status,
        "by_type": by_type,
        "coverage": {
            "months": TEST_COVERAGE_MONTHS,
            "tested_cost_codes": tested_count,
            "cost_codes": len(cost_code_ids),
            "percent": round(100 * tested_count / len(cost_code_ids), 1) if cost_code_ids else None,
        },
        "upcoming": [
            {
                "test_id": t.pk,
                "test_type": t.test_type,
                "cost_code": t.plan_version.plan.cost_code.cost_code,
                "scheduled_date": t.scheduled_date.isoformat() if t.scheduled_date else None,
            }
            for t in upcoming
        ],
    }


def _call_tree(cost_codes) -> dict:
    cost_code_ids = list(cost_codes.order_by().values_list("cost_code_id", flat=True))
    runs = CallTreeRun.objects.filter(cost_code_id__in=cost_code_ids, status=RunStatus.COMPLETED)
    live = runs.filter(simulation_flag=False)
    members = live.aggregate(
        total=Count("members"), reached=Count("members", filter=Q(members__reached_flag=True))
    )
    by_channel = {
        c: live.aggregate(
            n=Count("members", filter=Q(members__reached_flag=True, members__reached_channel=c))
        )["n"]
        for c in Channel.values
    }
    recent = (
        runs.select_related("cost_code").prefetch_related("members").order_by("-started_at")[:5]
    )
    return {
        "runs": runs.count(),
        "live_runs": live.count(),
        "simulation_runs": runs.filter(simulation_flag=True).count(),
        "members": members["total"],
        "reached": members["reached"],
        "response_rate": (
            round(members["reached"] / members["total"], 3) if members["total"] else None
        ),
        "reached_by_channel": by_channel,
        "recent": [
            {
                "call_tree_run_id": r.pk,
                "broadcast_id": r.broadcast_id,
                "cost_code": r.cost_code.cost_code if r.cost_code_id else "",
                "simulation_flag": r.simulation_flag,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "members": len(r.members.all()),
                "reached": sum(1 for m in r.members.all() if m.reached_flag),
            }
            for r in recent
        ],
    }


def _exemptions(version_ids: list[int]) -> dict:
    rows = Exemption.objects.filter(plan_version_id__in=version_ids)
    by_status = dict.fromkeys([s for s, _ in ExemptionStatus.choices], 0)
    for row in rows.order_by().values("status").annotate(total=Count("exemption_id")):
        by_status[row["status"]] = row["total"]
    register = rows.select_related("plan_version__plan__cost_code", "requested_by").order_by(
        "-created_at"
    )[:20]
    return {
        "by_status": by_status,
        "total": rows.count(),
        "register": [
            {
                "exemption_id": e.pk,
                "cost_code": e.plan_version.plan.cost_code.cost_code,
                "cost_code_id": e.plan_version.plan.cost_code_id,
                "plan_version_id": e.plan_version_id,
                "status": e.status,
                "reason": e.reason[:200],
                "requested_by": e.requested_by.display_name if e.requested_by_id else "",
                "created_at": e.created_at.isoformat(),
            }
            for e in register
        ],
    }


def build_dashboard(scope, *, estate_id: int | None = None, today: dt.date | None = None) -> dict:
    today = today or timezone.now().date()
    cost_codes = scoped_cost_codes(scope, estate_id)
    version_ids = current_version_ids(cost_codes)
    return {
        "generated_at": timezone.now().isoformat(),
        "estate_id": estate_id,
        "totals": _totals(cost_codes),
        "status_by_estate": _status_rollup(
            cost_codes, key="estate_id", label="estate__estate_name"
        ),
        "status_by_region": _status_rollup(
            cost_codes, key="region_id", label="region__region_name"
        ),
        "status_by_lob": _status_rollup(cost_codes, key="lob_id", label="lob__lob_name"),
        "completion": _completion(version_ids),
        "risk": _risk(version_ids, today),
        "tests": _tests(cost_codes, today),
        "call_tree": _call_tree(cost_codes),
        "exemptions": _exemptions(version_ids),
    }
