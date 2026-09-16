"""
Report builders (Phase 9.3-9.4).

Each builder returns a `Table`: a title, column headings and rows of plain
values. Rendering to Excel, CSV or PDF is someone else's job (`exports.py`),
so every format shows the same figures.

The estate detail report mirrors the legacy `BU details report`: one row per
cost code with its org context, the current plan version and status, its
coordinators, approval date, last test and exemption state.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.db.models import Max, Prefetch
from django.utils import timezone

from apps.accounts.scoping import ScopeResolver
from apps.calltree.models import CallTreeRun
from apps.exemptions.models import Exemption
from apps.organization.models import Estate
from apps.organization.querysets import CURRENT_STATUS
from apps.plans.models import CoordinatorAssignment, PlanVersion
from apps.reporting.metrics import build_dashboard, current_version_ids, scoped_cost_codes
from apps.reporting.models import ReportType
from apps.testing.models import Test


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    subtitle: str = ""


def _estate_label(estate_id) -> str:
    if estate_id is None:
        return "all estates in scope"
    estate = Estate.objects.filter(pk=estate_id).first()
    return estate.estate_name if estate else f"estate {estate_id}"


def _scope(user) -> ScopeResolver:
    return ScopeResolver(user)


def estate_detail(user, params: dict) -> Table:
    estate_id = params.get("estate_id")
    cost_codes = (
        scoped_cost_codes(_scope(user), estate_id)
        .select_related(
            "estate", "process", "subprocess", "region", "center", "location", "lob", "bu_lead"
        )
        .order_by("estate__estate_name", "cost_code")
    )
    version_ids = current_version_ids(cost_codes)
    versions = {
        v.pk: v
        for v in PlanVersion.objects.filter(pk__in=version_ids).prefetch_related(
            Prefetch(
                "coordinator_assignments",
                queryset=CoordinatorAssignment.objects.filter(active_flag=True).select_related(
                    "employee"
                ),
            )
        )
    }
    last_tests = {
        row["plan_version__plan__cost_code_id"]: row["last"]
        for row in Test.objects.filter(
            plan_version_id__in=version_ids, status=Test.Status.COMPLETED
        )
        .values("plan_version__plan__cost_code_id")
        .annotate(last=Max("outcomes__conducted_date"))
    }
    exemptions = {
        e.plan_version_id: e.status
        for e in Exemption.objects.filter(plan_version_id__in=version_ids).order_by("created_at")
    }

    table = Table(
        title="Estate detail report",
        subtitle=f"{_estate_label(estate_id)} - {timezone.now():%d %b %Y %H:%M}",
        columns=[
            "Estate",
            "Cost code",
            "Process",
            "Subprocess",
            "Region",
            "Centre",
            "Location",
            "LOB",
            "BU lead",
            "BU lead email",
            "Version",
            "BCP status",
            "Coordinators",
            "Approved on",
            "Approved by",
            "Last test",
            "Exemption",
        ],
    )
    for cc in cost_codes:
        version = versions.get(cc.current_plan_version_id)
        coordinators = (
            ", ".join(a.employee.full_name for a in version.coordinator_assignments.all())
            if version
            else ""
        )
        table.rows.append(
            [
                cc.estate.estate_name if cc.estate_id else "",
                cc.cost_code,
                cc.process.process_name if cc.process_id else "",
                cc.subprocess.subprocess_name if cc.subprocess_id else "",
                cc.region.region_name if cc.region_id else "",
                cc.center.center_name if cc.center_id else "",
                cc.location.location_name if cc.location_id else "",
                cc.lob.lob_name if cc.lob_id else "",
                cc.bu_lead.lead_name if cc.bu_lead_id else "",
                cc.bu_lead.email if cc.bu_lead_id else "",
                version.version_number if version else "",
                getattr(cc, CURRENT_STATUS),
                coordinators,
                version.approved_at.date() if version and version.approved_at else "",
                version.approved_by.display_name if version and version.approved_by_id else "",
                last_tests.get(cc.pk) or "",
                exemptions.get(cc.current_plan_version_id, ""),
            ]
        )
    return table


def coordinator_assignments(user, params: dict) -> Table:
    estate_id = params.get("estate_id")
    cost_codes = scoped_cost_codes(_scope(user), estate_id)
    version_ids = current_version_ids(cost_codes)
    rows = (
        CoordinatorAssignment.objects.filter(plan_version_id__in=version_ids, active_flag=True)
        .select_related(
            "employee",
            "plan_version__plan__cost_code__estate",
            "plan_version__plan__cost_code__process",
        )
        .order_by("plan_version__plan__cost_code__cost_code", "coordinator_type")
    )
    table = Table(
        title="Coordinator assignment report",
        subtitle=f"{_estate_label(estate_id)} - {timezone.now():%d %b %Y %H:%M}",
        columns=[
            "Estate",
            "Cost code",
            "Process",
            "Version",
            "BCP status",
            "Coordinator",
            "Employee no.",
            "Email",
            "Type",
            "Additional",
            "Assigned on",
        ],
    )
    for a in rows:
        cc = a.plan_version.plan.cost_code
        table.rows.append(
            [
                cc.estate.estate_name if cc.estate_id else "",
                cc.cost_code,
                cc.process.process_name if cc.process_id else "",
                a.plan_version.version_number,
                a.plan_version.status,
                a.employee.full_name,
                a.employee.employee_number,
                a.employee.email,
                a.coordinator_type,
                "Yes" if a.additional_user_flag else "No",
                a.created_at.date(),
            ]
        )
    return table


def call_tree_runs(user, params: dict) -> Table:
    estate_id = params.get("estate_id")
    cost_code_ids = list(
        scoped_cost_codes(_scope(user), estate_id).order_by().values_list("cost_code_id", flat=True)
    )
    runs = (
        CallTreeRun.objects.filter(cost_code_id__in=cost_code_ids)
        .select_related("cost_code", "initiated_by")
        .prefetch_related("members__attempts")
        .order_by("-started_at")
    )
    table = Table(
        title="Call tree run report",
        subtitle=f"{_estate_label(estate_id)} - {timezone.now():%d %b %Y %H:%M}",
        columns=[
            "Broadcast",
            "Cost code",
            "Type",
            "Mode",
            "Status",
            "Started",
            "Completed",
            "Initiated by",
            "Members",
            "Reached",
            "Response rate",
            "Voice",
            "Teams",
            "Email attempts",
        ],
    )
    for r in runs:
        members = list(r.members.all())
        reached = sum(1 for m in members if m.reached_flag)
        by_channel = {"VOICE": 0, "MS_TEAMS": 0, "EMAIL": 0}
        for m in members:
            for a in m.attempts.all():
                by_channel[a.channel] = by_channel.get(a.channel, 0) + 1
        table.rows.append(
            [
                r.broadcast_id,
                r.cost_code.cost_code if r.cost_code_id else "",
                r.call_tree_type,
                "Simulation" if r.simulation_flag else "Live",
                r.status,
                r.started_at,
                r.completed_at,
                r.initiated_by.display_name if r.initiated_by_id else "",
                len(members),
                reached,
                f"{100 * reached / len(members):.0f}%" if members else "",
                sum(1 for m in members if m.reached_flag and m.reached_channel == "VOICE"),
                sum(1 for m in members if m.reached_flag and m.reached_channel == "MS_TEAMS"),
                by_channel["EMAIL"],
            ]
        )
    return table


def exemption_register(user, params: dict) -> Table:
    estate_id = params.get("estate_id")
    version_ids = current_version_ids(scoped_cost_codes(_scope(user), estate_id))
    rows = (
        Exemption.objects.filter(plan_version_id__in=version_ids)
        .select_related(
            "plan_version__plan__cost_code__estate",
            "plan_version__plan__cost_code__process",
            "requested_by",
        )
        .order_by("-created_at")
    )
    table = Table(
        title="Exemption register",
        subtitle=f"{_estate_label(estate_id)} - {timezone.now():%d %b %Y %H:%M}",
        columns=[
            "Estate",
            "Cost code",
            "Process",
            "Version",
            "Status",
            "Reason",
            "Requested by",
            "Requested on",
            "Last updated",
        ],
    )
    for e in rows:
        cc = e.plan_version.plan.cost_code
        table.rows.append(
            [
                cc.estate.estate_name if cc.estate_id else "",
                cc.cost_code,
                cc.process.process_name if cc.process_id else "",
                e.plan_version.version_number,
                e.status,
                e.reason,
                e.requested_by.display_name if e.requested_by_id else "",
                e.created_at,
                e.updated_at,
            ]
        )
    return table


def dashboard_summary(user, params: dict) -> Table:
    estate_id = params.get("estate_id")
    data = build_dashboard(_scope(user), estate_id=estate_id)
    table = Table(
        title="Dashboard summary",
        subtitle=f"{_estate_label(estate_id)} - {timezone.now():%d %b %Y %H:%M}",
        columns=["Measure", "Value"],
    )
    t = data["totals"]
    table.rows += [["Cost codes", t["cost_codes"]]]
    table.rows += [[f"BCP status: {status}", count] for status, count in t["by_status"].items()]
    table.rows += [
        [
            "Approval rate",
            f"{100 * t['approval_rate']:.0f}%" if t["approval_rate"] is not None else "",
        ],
        ["Open versions", data["completion"]["open_versions"]],
        [
            "Section completion",
            (
                f"{data['completion']['percent']}%"
                if data["completion"]["percent"] is not None
                else ""
            ),
        ],
        ["Risks on current versions", data["risk"]["total"]],
    ]
    table.rows += [[f"Risks rated {level}", n] for level, n in data["risk"]["by_level"].items()]
    table.rows += [
        ["Open risk actions", data["risk"]["open_actions"]],
        ["Overdue risk actions", data["risk"]["overdue_actions"]],
        [
            f"Cost codes tested in the last {data['tests']['coverage']['months']} months",
            data["tests"]["coverage"]["tested_cost_codes"],
        ],
        [
            "Test coverage",
            (
                f"{data['tests']['coverage']['percent']}%"
                if data["tests"]["coverage"]["percent"] is not None
                else ""
            ),
        ],
    ]
    table.rows += [[f"Tests: {status}", n] for status, n in data["tests"]["by_status"].items()]
    table.rows += [
        ["Call tree runs (live)", data["call_tree"]["live_runs"]],
        ["Call tree runs (simulation)", data["call_tree"]["simulation_runs"]],
        [
            "Call tree members reached (live)",
            f"{data['call_tree']['reached']} of {data['call_tree']['members']}",
        ],
    ]
    table.rows += [
        [f"Exemptions: {status}", n] for status, n in data["exemptions"]["by_status"].items()
    ]
    return table


BUILDERS = {
    ReportType.ESTATE_DETAIL: estate_detail,
    ReportType.COORDINATOR_ASSIGNMENTS: coordinator_assignments,
    ReportType.CALL_TREE_RUNS: call_tree_runs,
    ReportType.EXEMPTION_REGISTER: exemption_register,
    ReportType.DASHBOARD_SUMMARY: dashboard_summary,
}


def build(report_type: str, user, params: dict) -> Table:
    return BUILDERS[report_type](user, params or {})


def as_of_date() -> dt.date:
    return timezone.now().date()
