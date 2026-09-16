"""Fixtures for plan versioning, coordinator assignment and history."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Employee, UserEstateScope
from apps.assessments.models import (
    AnswerContext,
    BiaCriticalContact,
    BiaServiceDescription,
    NetworkRequirement,
    QuestionAnswer,
    QuestionComment,
    RequirementType,
    SectionStatus,
    SectionStatusValue,
)
from apps.crisis.models import CmscMember
from apps.organization.models import BuLead, CostCode, Estate, Process, Region, Subprocess
from apps.plans.models import (
    CoordinatorAssignment,
    Plan,
    PlanStatus,
    PlanStatusHistory,
    PlanVersion,
)
from apps.questionnaire.models import AnswerType, Question, Section
from apps.risk.models import ActionType, RecoveryStrategy, Risk, RiskAction


@pytest.fixture
def org(db):
    estate = Estate.objects.create(estate_name="Alpha Estate")
    other_estate = Estate.objects.create(estate_name="Beta Estate")
    region = Region.objects.create(region_name="North", geography="Americas")
    process = Process.objects.create(process_name="Customer Support")
    subprocess = Subprocess.objects.create(subprocess_name="Tier 1", process=process)
    bu_lead = BuLead.objects.create(lead_name="Priya Lead", email="priya@example.com")

    cost_code = CostCode.objects.create(
        cost_code="CC-1001",
        estate=estate,
        process=process,
        subprocess=subprocess,
        region=region,
        bu_lead=bu_lead,
    )
    other_cost_code = CostCode.objects.create(
        cost_code="CC-9001", estate=other_estate, process=process
    )
    return {
        "estate": estate,
        "other_estate": other_estate,
        "region": region,
        "process": process,
        "subprocess": subprocess,
        "bu_lead": bu_lead,
        "cost_code": cost_code,
        "other_cost_code": other_cost_code,
    }


@pytest.fixture
def employee(db):
    return Employee.objects.create(
        employee_number="1100002",
        full_name="Arun Coordinator",
        email="arun.coordinator@example.com",
    )


@pytest.fixture
def actor(user_factory, employee):
    user = user_factory(email="arun.coordinator@example.com", roles=["BCM_COORDINATOR"])
    user.employee = employee
    user.save(update_fields=["employee"])
    return user


@pytest.fixture
def question(db):
    section = Section.objects.create(section_name="Basic Questions", display_order=1)
    return Question.objects.create(
        section=section,
        question_text="Dedicated Client ODC for this process?",
        answer_type=AnswerType.SINGLE_CHOICE,
        display_order=1,
    )


@pytest.fixture
def approved_version(org, actor, employee, question):
    """An Approved version populated across every carried-forward table.

    Also carries one row in each table that must NOT be copied, so the copy tests
    can assert both directions rather than only the happy one.
    """
    plan = Plan.objects.create(cost_code=org["cost_code"], process=org["process"])
    version = PlanVersion.objects.create(
        plan=plan,
        version_number=1,
        status=PlanStatus.APPROVED,
        plan_mode="Standard",
        review_mode="Annual",
        published_flag=True,
        approved_by=actor,
        created_by=actor,
    )

    # --- carried forward ---
    QuestionAnswer.objects.create(
        plan_version=version,
        question=question,
        respondent_user=actor,
        answer_context=AnswerContext.BCP,
        answer_json={"value": "Yes"},
        comments="Confirmed with the client.",
    )
    SectionStatus.objects.create(
        plan_version=version,
        section=question.section,
        status=SectionStatusValue.COMPLETED,
    )
    BiaServiceDescription.objects.create(
        plan_version=version,
        process=org["process"],
        subprocess=org["subprocess"],
        cost_code=org["cost_code"],
        owner_employee=employee,
        process_description="Handles inbound customer contact.",
        mao="24",
        mbco="60",
        rto="8",
        rpo="4",
    )
    BiaCriticalContact.objects.create(
        plan_version=version,
        employee=employee,
        contact_type="Primary",
        primary_phone="+91 99999 11111",
        seat_count=12,
        voice_non_voice="Voice",
    )
    NetworkRequirement.objects.create(
        plan_version=version,
        employee=employee,
        requirement_type=RequirementType.BCP_PLAN,
        source_ip="10.0.0.1",
        destination_ip="10.0.1.1",
        port_number="443",
        connectivity_type="MPLS",
    )
    RecoveryStrategy.objects.create(
        plan_version=version,
        owner_employee=employee,
        core_strategy="Shift to the Chennai centre.",
        tactical_strategy="Work from home for 48 hours.",
    )
    CoordinatorAssignment.objects.create(
        plan_version=version,
        employee=employee,
        coordinator_type="Primary",
        estate=org["estate"],
    )
    risk = Risk.objects.create(
        plan_version=version,
        owner_employee=employee,
        risk_name="Power failure at primary site",
        description="Grid instability during monsoon.",
        likelihood_rating=3,
        severity_rating=4,
        residual_risk_score=8,
        risk_level="High",
    )
    RiskAction.objects.create(
        risk=risk,
        action_type=ActionType.MITIGATION,
        description="Service the generators quarterly.",
        status="Open",
    )
    RiskAction.objects.create(
        risk=risk,
        action_type=ActionType.CONTINGENCY,
        description="Fail over to Chennai.",
        status="Open",
    )

    # --- deliberately not carried forward ---
    PlanStatusHistory.objects.create(
        plan_version=version,
        status=PlanStatus.APPROVED,
        comments="Approved.",
        changed_by=actor,
    )
    QuestionComment.objects.create(
        plan_version=version,
        question=question,
        author=actor,
        comment="Please expand the RTO justification.",
    )
    CmscMember.objects.create(
        plan_version=version,
        cost_code=org["cost_code"],
        member_name="Ravi Crisis Lead",
        member_email="ravi@example.com",
    )
    return version


@pytest.fixture
def coordinator_client(actor, org):
    """Arun, with estate-wide sight.

    The viewer role is what lets these tests reach a cost code Arun is not yet
    assigned to (editing it, starting its first version). A bare coordinator
    sees only their assigned cost codes — proven in test_own_record_scope — so
    in practice that first version is started by an administrator or the BU
    lead, who then assigns the coordinator.
    """
    from apps.accounts.models import Role, UserRole

    viewer, _ = Role.objects.get_or_create(role_code="BCM_VIEWER", defaults={"role_name": "Viewer"})
    UserRole.objects.get_or_create(user=actor, role=viewer)
    UserEstateScope.objects.create(user=actor, estate=org["estate"])
    api_client = APIClient()
    api_client.force_authenticate(user=actor)
    return api_client


@pytest.fixture
def admin_client(user_factory):
    api_client = APIClient()
    api_client.force_authenticate(user=user_factory(email="admin@example.com", roles=["BCM_ADMIN"]))
    return api_client


def rows(response):
    data = response.data
    return data["results"] if isinstance(data, dict) and "results" in data else data
