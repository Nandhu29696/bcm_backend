"""
The plan's overview — what the editor shows between the questionnaire tabs and
the BIA / RA / Plan parts.

    GET /api/v1/plan-versions/{id}/overview/

    objectives   the recovery objectives, read straight from the questionnaire
                 answers (RTO, MBCO, RPO, MAO); nothing is stored twice
    stages       BIA, RA (risk assessment) and Plan, each with a derived status
    people       who is behind the cost code: its employees (with the headcount
                 the MBCO percentage works out to), BU leads and coordinators

Every status here is derived on read, like section completion (Phase 4.6):
there is no column to drift out of step with the rows it summarises.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from apps.accounts.models import Employee
from apps.assessments.models import (
    AnswerContext,
    BiaCriticalContact,
    NetworkRequirement,
    SectionStatusValue,
)
from apps.documents.attachments import attachments_for_version
from apps.plans.models import CoordinatorAssignment, PlanVersion
from apps.questionnaire.evidence import evidence_by_question
from apps.questionnaire.models import SectionGroup
from apps.questionnaire.services import (
    answers_by_question,
    ordered_questions,
    summarise_sections,
)
from apps.questionnaire.visibility import compute_visibility
from apps.risk.models import RecoveryStrategy, Risk

#: Question codes behind each recovery objective (see `seed_questionnaire`).
OBJECTIVE_QUESTIONS = {
    "rto_hours": "RTO-001",
    "mbco_percent": "MBCO-001",
    "rpo_in_contract": "RPO-001",
    "rpo_hours": "RPO-002",
    "mao_hours": "MAO-001",
}


def _scalar(answer_json) -> object | None:
    if not isinstance(answer_json, dict):
        return None
    value = answer_json.get("value")
    return value if value not in ("", None, []) else None


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_number(value: Decimal | None):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def build_overview(version: PlanVersion) -> dict:
    questions = ordered_questions()
    answers = answers_by_question(version, AnswerContext.BCP)
    visibility = compute_visibility(questions, {qid: a.answer_json for qid, a in answers.items()})
    summaries = summarise_sections(
        questions, answers, visibility, set(evidence_by_question(version))
    )

    by_code = {q.question_code: answers.get(q.question_id) for q in questions}
    raw = {
        key: _scalar(by_code[code].answer_json) if by_code.get(code) else None
        for key, code in OBJECTIVE_QUESTIONS.items()
    }
    mbco_percent = _number(raw["mbco_percent"])

    objectives = {
        "rto_hours": _json_number(_number(raw["rto_hours"])),
        "mbco_percent": _json_number(mbco_percent),
        "rpo_in_contract": raw["rpo_in_contract"],
        # RPO is asked only when there is one in the contract; a stale number
        # behind a "No" must not surface as an objective.
        "rpo_hours": (
            _json_number(_number(raw["rpo_hours"])) if raw["rpo_in_contract"] == "YES" else None
        ),
        "mao_hours": _json_number(_number(raw["mao_hours"])),
    }

    return {
        "plan_version_id": version.pk,
        "objectives": objectives,
        "stages": {
            "bia": _bia_stage(version, summaries),
            "ra": _ra_stage(version),
            "plan": _plan_stage(version),
        },
        "people": _people(version, mbco_percent),
    }


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


def _bia_stage(version: PlanVersion, summaries) -> dict:
    """The BIA questions decide the status; the lists are reported alongside."""
    bia = [s for s in summaries if s.section.group == SectionGroup.BIA]
    required_visible = sum(s.required_visible for s in bia)
    required_answered = sum(s.required_answered for s in bia)
    statuses = {s.status for s in bia}
    contacts = BiaCriticalContact.objects.filter(plan_version=version).count()
    network = NetworkRequirement.objects.filter(plan_version=version).count()

    if bia and statuses == {SectionStatusValue.COMPLETED}:
        status = SectionStatusValue.COMPLETED
    elif required_answered or contacts or network:
        status = SectionStatusValue.IN_PROGRESS
    else:
        status = SectionStatusValue.NOT_STARTED
    return {
        "status": status,
        "required_visible": required_visible,
        "required_answered": required_answered,
        "critical_resources": contacts,
        "network_requirements": network,
    }


def _ra_stage(version: PlanVersion) -> dict:
    """Complete once every risk carries a level — i.e. all three ratings are in."""
    risks = list(Risk.objects.filter(plan_version=version).values_list("risk_level", flat=True))
    unrated = sum(1 for level in risks if not level)
    if not risks:
        status = SectionStatusValue.NOT_STARTED
    elif unrated:
        status = SectionStatusValue.IN_PROGRESS
    else:
        status = SectionStatusValue.COMPLETED
    return {"status": status, "risks": len(risks), "unrated": unrated}


def _plan_stage(version: PlanVersion) -> dict:
    """A plan is a strategy; the diagram and the copied lists support it."""
    strategies = RecoveryStrategy.objects.filter(plan_version=version).count()
    diagrams = attachments_for_version(version, "NETWORK_DIAGRAM").count()
    if strategies:
        status = SectionStatusValue.COMPLETED
    elif diagrams:
        status = SectionStatusValue.IN_PROGRESS
    else:
        status = SectionStatusValue.NOT_STARTED
    return {"status": status, "strategies": strategies, "network_diagrams": diagrams}


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #


def _people(version: PlanVersion, mbco_percent: Decimal | None) -> dict:
    cost_code = version.plan.cost_code
    employees = list(
        Employee.objects.filter(cost_code=cost_code)
        .select_related("bu_lead")
        .order_by("full_name", "employee_number")
    )
    headcount = len(employees)
    required = None
    if mbco_percent is not None and headcount:
        # Round up: 60% of 7 people is 5 people, not 4.2.
        required = min(headcount, math.ceil(headcount * float(mbco_percent) / 100))

    # The cost code's own BU lead first; then any other lead the employees
    # report to (an "add-on" lead, where a team is shared across units).
    leads: dict[int, dict] = {}
    if cost_code.bu_lead_id:
        leads[cost_code.bu_lead_id] = _lead_payload(cost_code.bu_lead, primary=True)
    for employee in employees:
        lead = employee.bu_lead
        if lead is not None and lead.pk not in leads:
            leads[lead.pk] = _lead_payload(lead, primary=False)

    coordinators = (
        CoordinatorAssignment.objects.filter(plan_version=version, active_flag=True)
        .select_related("employee")
        .order_by("coordinator_type", "employee__full_name")
    )
    return {
        "headcount": headcount,
        "mbco_percent": _json_number(mbco_percent),
        "mbco_required": required,
        "employees": [
            {
                "employee_id": e.pk,
                "employee_number": e.employee_number,
                "full_name": e.full_name,
                "email": e.email,
                "designation": e.designation,
                "bu_lead": getattr(e.bu_lead, "lead_name", ""),
            }
            for e in employees
        ],
        "bu_leads": list(leads.values()),
        "coordinators": [
            {
                "coordinator_assignment_id": a.pk,
                "employee_id": a.employee_id,
                "full_name": a.employee.full_name,
                "email": a.employee.email,
                "coordinator_type": a.coordinator_type,
            }
            for a in coordinators
        ],
    }


def _lead_payload(lead, *, primary: bool) -> dict:
    return {
        "bu_lead_id": lead.pk,
        "lead_name": lead.lead_name,
        "email": lead.email,
        "primary": primary,
    }
