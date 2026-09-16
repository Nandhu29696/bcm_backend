"""
Copy-on-write versioning (Phase 3.4) — the highest-risk logic in the system.

The Phase 3 exit criterion is specific: copying an Approved version must reproduce
every child record and leave the original untouched. Those are two separate
properties and they fail independently, so they are two separate tests
(`test_the_source_version_is_untouched` and the per-table copy tests below).

There is a test per carried-forward table because a deep copy fails
*partially* — one relation silently missing looks exactly like a successful copy
in the UI, and is only discovered when a coordinator opens the new version and
finds a section blank.
"""

import pytest
from django.db import models

from apps.assessments.models import (
    BiaCriticalContact,
    BiaServiceDescription,
    NetworkRequirement,
    QuestionAnswer,
    QuestionComment,
    SectionStatus,
    SectionStatusValue,
)
from apps.crisis.models import CmscMember
from apps.plans.models import (
    CoordinatorAssignment,
    Plan,
    PlanStatus,
    PlanStatusHistory,
    PlanVersion,
)
from apps.plans.versioning import (
    CARRIED_RELATIONS,
    NOT_CARRIED,
    PlanVersionError,
    copy_plan_version,
    ensure_current_version,
)
from apps.risk.models import RecoveryStrategy, Risk, RiskAction

pytestmark = pytest.mark.django_db


@pytest.fixture
def copied(approved_version, actor):
    return copy_plan_version(approved_version, actor=actor)


# --------------------------------------------------------------------------- #
# The new version itself
# --------------------------------------------------------------------------- #


def test_the_copy_is_a_new_editable_version(approved_version, copied):
    assert copied.pk != approved_version.pk
    assert copied.plan_id == approved_version.plan_id
    assert copied.version_number == approved_version.version_number + 1
    assert copied.status == PlanStatus.WORK_IN_PROGRESS
    assert copied.is_editable is True
    assert copied.copied_flag is True


def test_the_copy_does_not_inherit_the_approval(approved_version, copied):
    """An approval belongs to what was approved, never to a copy of it.

    Carrying `approved_by` over would make an unreviewed version look signed off.
    """
    assert approved_version.approved_by_id is not None
    assert copied.approved_by_id is None
    assert copied.approved_at is None
    assert copied.published_flag is False


def test_the_copy_records_its_own_history_entry(copied, approved_version):
    entry = PlanStatusHistory.objects.get(plan_version=copied)
    assert entry.status == PlanStatus.WORK_IN_PROGRESS
    assert f"version {approved_version.version_number}" in entry.comments


def test_plan_mode_and_review_mode_follow_the_copy(approved_version, copied):
    assert copied.plan_mode == approved_version.plan_mode == "Standard"
    assert copied.review_mode == approved_version.review_mode == "Annual"


# --------------------------------------------------------------------------- #
# One test per carried-forward table
# --------------------------------------------------------------------------- #


def test_answers_are_copied(approved_version, copied):
    original = QuestionAnswer.objects.get(plan_version=approved_version)
    clone = QuestionAnswer.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.question_id == original.question_id
    assert clone.answer_json == {"value": "Yes"}
    assert clone.answer_context == original.answer_context
    assert clone.comments == original.comments
    assert clone.respondent_user_id == original.respondent_user_id


def test_section_statuses_are_copied(approved_version, copied):
    clone = SectionStatus.objects.get(plan_version=copied)
    assert clone.section_id == SectionStatus.objects.get(plan_version=approved_version).section_id
    assert clone.status == SectionStatusValue.COMPLETED


def test_service_descriptions_are_copied(approved_version, copied):
    original = BiaServiceDescription.objects.get(plan_version=approved_version)
    clone = BiaServiceDescription.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.process_description == original.process_description
    assert (clone.mao, clone.mbco, clone.rto, clone.rpo) == ("24", "60", "8", "4")
    assert clone.owner_employee_id == original.owner_employee_id


def test_critical_contacts_are_copied(approved_version, copied):
    original = BiaCriticalContact.objects.get(plan_version=approved_version)
    clone = BiaCriticalContact.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.primary_phone == "+91 99999 11111"
    assert clone.seat_count == 12
    assert clone.employee_id == original.employee_id


def test_network_requirements_are_copied(approved_version, copied):
    original = NetworkRequirement.objects.get(plan_version=approved_version)
    clone = NetworkRequirement.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.source_ip == "10.0.0.1"
    assert clone.destination_ip == "10.0.1.1"
    assert clone.port_number == "443"
    assert clone.requirement_type == original.requirement_type


def test_recovery_strategies_are_copied(approved_version, copied):
    original = RecoveryStrategy.objects.get(plan_version=approved_version)
    clone = RecoveryStrategy.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.core_strategy == original.core_strategy
    assert clone.tactical_strategy == original.tactical_strategy


def test_coordinator_assignments_are_copied(approved_version, copied):
    """Without this the new version has nobody assigned and no route back to one."""
    original = CoordinatorAssignment.objects.get(plan_version=approved_version)
    clone = CoordinatorAssignment.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.employee_id == original.employee_id
    assert clone.coordinator_type == "Primary"
    assert clone.estate_id == original.estate_id


def test_risks_are_copied(approved_version, copied):
    original = Risk.objects.get(plan_version=approved_version)
    clone = Risk.objects.get(plan_version=copied)

    assert clone.pk != original.pk
    assert clone.risk_name == original.risk_name
    assert clone.likelihood_rating == original.likelihood_rating
    assert clone.residual_risk_score == original.residual_risk_score
    assert clone.risk_level == "High"


def test_risk_actions_follow_their_risk(approved_version, copied):
    """The grandchild case: actions must re-parent onto the COPIED risk.

    Getting this wrong is the subtle failure — the actions are created, so the
    counts look right, but they still point at the old version's risk and editing
    one silently mutates an approved, immutable version.
    """
    original_risk = Risk.objects.get(plan_version=approved_version)
    cloned_risk = Risk.objects.get(plan_version=copied)

    cloned_actions = RiskAction.objects.filter(risk=cloned_risk)
    assert cloned_actions.count() == 2
    assert set(cloned_actions.values_list("action_type", flat=True)) == {
        "MITIGATION",
        "CONTINGENCY",
    }
    assert RiskAction.objects.filter(risk=original_risk).count() == 2
    # No action may be shared between the two risks.
    assert not set(cloned_actions.values_list("pk", flat=True)) & set(
        RiskAction.objects.filter(risk=original_risk).values_list("pk", flat=True)
    )


# --------------------------------------------------------------------------- #
# What must NOT be copied
# --------------------------------------------------------------------------- #


def test_the_previous_versions_history_does_not_follow(approved_version, copied):
    """Copying it would fabricate an approval the new version never received."""
    statuses = list(
        PlanStatusHistory.objects.filter(plan_version=copied).values_list("status", flat=True)
    )
    assert statuses == [PlanStatus.WORK_IN_PROGRESS]
    assert PlanStatusHistory.objects.filter(plan_version=approved_version).count() == 1


def test_review_comments_do_not_follow(copied):
    assert QuestionComment.objects.filter(plan_version=copied).count() == 0


def test_the_crisis_roster_does_not_follow(approved_version, copied):
    """`cmsc_members` hangs off the cost code; copying it duplicates real people."""
    assert CmscMember.objects.filter(plan_version=copied).count() == 0
    assert CmscMember.objects.filter(plan_version=approved_version).count() == 1


# --------------------------------------------------------------------------- #
# Isolation — the other half of the exit criterion
# --------------------------------------------------------------------------- #


def test_the_source_version_is_untouched(approved_version, actor):
    """Snapshot every field and child count, copy, and assert nothing moved."""
    before = {
        field.attname: getattr(approved_version, field.attname)
        for field in PlanVersion._meta.concrete_fields
    }
    counts_before = {
        spec.label: spec.model.objects.filter(plan_version=approved_version).count()
        for spec in CARRIED_RELATIONS
    }

    copy_plan_version(approved_version, actor=actor)

    approved_version.refresh_from_db()
    after = {
        field.attname: getattr(approved_version, field.attname)
        for field in PlanVersion._meta.concrete_fields
    }
    assert after == before

    counts_after = {
        spec.label: spec.model.objects.filter(plan_version=approved_version).count()
        for spec in CARRIED_RELATIONS
    }
    assert counts_after == counts_before


def test_editing_the_copy_does_not_reach_the_original(approved_version, copied):
    clone = QuestionAnswer.objects.get(plan_version=copied)
    clone.answer_json = {"value": "No"}
    clone.save()

    original = QuestionAnswer.objects.get(plan_version=approved_version)
    assert original.answer_json == {"value": "Yes"}


def test_every_carried_relation_actually_produced_rows(approved_version, copied):
    """Guards the fixture as much as the service.

    If a relation were left unpopulated in the fixture, its copy test would pass
    vacuously — zero rows copied to zero rows.
    """
    for spec in CARRIED_RELATIONS:
        source_count = spec.model.objects.filter(plan_version=approved_version).count()
        target_count = spec.model.objects.filter(plan_version=copied).count()
        assert source_count > 0, f"{spec.label} is not exercised by the fixture"
        assert target_count == source_count, f"{spec.label} copied {target_count}/{source_count}"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status",
    [
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.REWORK,
    ],
)
def test_an_in_flight_version_cannot_be_copied(approved_version, actor, status):
    """Two live versions of one plan has no meaning — which is current?"""
    approved_version.status = status
    approved_version.save(update_fields=["status"])

    with pytest.raises(PlanVersionError) as error:
        copy_plan_version(approved_version, actor=actor)
    assert error.value.code == "source_not_copyable"


def test_an_exempted_version_can_be_copied(approved_version, actor):
    approved_version.status = PlanStatus.EXEMPTED
    approved_version.save(update_fields=["status"])
    assert copy_plan_version(approved_version, actor=actor).version_number == 2


def test_a_second_copy_is_refused_while_the_first_is_open(approved_version, actor, copied):
    """The copy from `copied` is Work in Progress, so the plan already has a live version."""
    with pytest.raises(PlanVersionError) as error:
        copy_plan_version(approved_version, actor=actor)
    assert error.value.code == "open_version_exists"


def test_copying_again_is_allowed_once_the_open_version_closes(approved_version, actor, copied):
    copied.status = PlanStatus.APPROVED
    copied.save(update_fields=["status"])

    third = copy_plan_version(copied, actor=actor)
    assert third.version_number == 3


def test_a_failed_copy_leaves_no_partial_version(approved_version, actor, monkeypatch):
    """One transaction: a half-copied version would look complete and lose a section."""
    from apps.plans import versioning

    def explode(spec, source, target):
        if spec.label == "risks":
            raise RuntimeError("database went away")
        return 0

    monkeypatch.setattr(versioning, "_copy_relation", explode)

    with pytest.raises(RuntimeError):
        copy_plan_version(approved_version, actor=actor)

    assert PlanVersion.objects.filter(plan_id=approved_version.plan_id).count() == 1


# --------------------------------------------------------------------------- #
# Completeness of the classification
# --------------------------------------------------------------------------- #


def test_every_plan_version_child_is_classified():
    """A new table referencing plan_version must be a deliberate decision.

    Without this, adding a content table in a later phase silently stops being
    copied — and nothing fails until a coordinator finds the section empty in a
    version they just created.
    """
    carried = {spec.model._meta.label for spec in CARRIED_RELATIONS}
    classified = carried | set(NOT_CARRIED)

    referencing = {
        related.related_model._meta.label
        for related in PlanVersion._meta.related_objects
        if isinstance(related.field, models.ForeignKey)
    }

    unclassified = referencing - classified
    assert not unclassified, (
        "These models reference PlanVersion but are neither copied nor listed in "
        f"NOT_CARRIED: {sorted(unclassified)}. Decide which, and say why."
    )


# --------------------------------------------------------------------------- #
# ensure_current_version (Phase 3.6)
# --------------------------------------------------------------------------- #


def test_a_plan_with_no_version_gets_a_not_started_one(org, actor):
    plan = Plan.objects.create(cost_code=org["cost_code"], process=org["process"])

    version = ensure_current_version(plan, actor=actor)

    assert version.version_number == 1
    assert version.status == PlanStatus.NOT_STARTED
    assert PlanStatusHistory.objects.filter(plan_version=version).count() == 1


def test_ensure_current_version_is_idempotent(org, actor):
    plan = Plan.objects.create(cost_code=org["cost_code"], process=org["process"])

    first = ensure_current_version(plan, actor=actor)
    second = ensure_current_version(plan, actor=actor)

    assert first.pk == second.pk
    assert PlanVersion.objects.filter(plan=plan).count() == 1


def test_ensure_current_version_returns_the_newest(approved_version, actor, copied):
    current = ensure_current_version(approved_version.plan, actor=actor)
    assert current.pk == copied.pk
