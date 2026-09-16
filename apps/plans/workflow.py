"""
Plan status transitions (Phase 6) — journey steps 6 and 7.

    submit   Work in Progress / Rework  ->  Pending BU Lead Review
    approve  Pending BU Lead Review     ->  Approved      (+ document generation)
    rework   Pending BU Lead Review     ->  Rework        (comment required)

Every transition is validated against `ALLOWED_TRANSITIONS` (the one table of
permitted moves), writes a `plan_status_history` row, writes an audit entry,
and sends the right email to the right people:

    submit   TO the cost code's BU lead      CC every assigned coordinator
    approve  TO every assigned coordinator   CC the BU lead
    rework   TO every assigned coordinator   CC the BU lead

Transitions are verbs on the API, never a PATCH of `status` (AD-10). Nothing
outside this module changes a version's status.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.accounts.services import record_audit
from apps.core.exceptions import DomainError, InvalidStateTransition
from apps.core.models import AuditLog
from apps.notifications.models import NotificationEvent
from apps.notifications.services import build_idempotency_key, send_notification
from apps.plans.models import CoordinatorAssignment, PlanStatus, PlanStatusHistory, PlanVersion

logger = logging.getLogger(__name__)


class PlanIncomplete(DomainError):
    """Submission refused because required questions are unanswered."""

    default_code = "plan_incomplete"

    def __init__(self, incomplete: list[dict]):
        names = ", ".join(s["section_name"] for s in incomplete)
        super().__init__(f"These sections are not complete: {names}.")
        self.incomplete = incomplete


class CommentRequired(DomainError):
    default_code = "comment_required"
    default_detail = "Say what needs to change before sending a plan back."


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #


def bu_lead_email(version: PlanVersion) -> str:
    lead = version.plan.cost_code.bu_lead
    return (lead.email or "").strip() if lead else ""


def coordinator_emails(version: PlanVersion) -> list[str]:
    return [
        email.strip()
        for email in CoordinatorAssignment.objects.filter(plan_version=version, active_flag=True)
        .select_related("employee")
        .values_list("employee__email", flat=True)
        if email and email.strip()
    ]


def _plan_context(version: PlanVersion) -> dict:
    cost_code = version.plan.cost_code
    return {
        "plan_version_id": version.pk,
        "version_number": version.version_number,
        "cost_code": cost_code.cost_code,
        "process_name": getattr(cost_code.process, "process_name", ""),
        "estate_name": getattr(cost_code.estate, "estate_name", ""),
        "bu_lead_name": getattr(cost_code.bu_lead, "lead_name", ""),
    }


def _notify(
    *,
    event: str,
    version: PlanVersion,
    to: list[str],
    cc: list[str],
    subject: str,
    template: str,
    extra: dict,
    salt: str,
) -> None:
    """One email per TO recipient, the same CC list on each, idempotent per transition."""
    cc = [address for address in dict.fromkeys(cc) if address and address not in to]
    for address in dict.fromkeys(to):
        if not address:
            continue
        send_notification(
            event_type=event,
            to_email=address,
            cc_emails=cc,
            subject=subject,
            template_name=template,
            context={**_plan_context(version), **extra},
            entity_type="PLAN_VERSION",
            entity_id=version.pk,
            idempotency_key=build_idempotency_key(event, version.pk, address, salt=salt),
        )


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


def _move(version: PlanVersion, to_status: str, *, actor, comments: str) -> PlanVersion:
    """Apply a transition, or refuse it. Locks the row so two clicks cannot race."""
    locked = PlanVersion.objects.select_for_update().get(pk=version.pk)
    if not locked.can_transition_to(to_status):
        raise InvalidStateTransition(
            f"A version that is {locked.status} cannot move to {to_status}."
        )
    locked.status = to_status
    locked.updated_by = actor
    if to_status == PlanStatus.APPROVED:
        locked.approved_by = actor
        locked.approved_at = timezone.now()
        locked.published_flag = True
    locked.save(
        update_fields=[
            "status",
            "updated_by",
            "approved_by",
            "approved_at",
            "published_flag",
            "updated_at",
        ]
    )

    PlanStatusHistory.objects.create(
        plan_version=locked, status=to_status, comments=comments, changed_by=actor
    )
    record_audit(
        action=AuditLog.Action.STATUS_TRANSITION,
        actor=actor,
        entity_type="PlanVersion",
        entity_id=locked.pk,
        detail={"from": version.status, "to": to_status, "comments": comments},
    )
    return locked


def incomplete_sections(version: PlanVersion) -> list[dict]:
    """Sections that would block submission, freshly recomputed."""
    from apps.assessments.models import SectionStatusValue
    from apps.questionnaire.services import recompute_section_statuses

    return [
        {
            "section_id": s.section.section_id,
            "section_name": s.section.section_name,
            "status": s.status,
            "required_answered": s.required_answered,
            "required_visible": s.required_visible,
        }
        for s in recompute_section_statuses(version)
        if s.status != SectionStatusValue.COMPLETED
    ]


@transaction.atomic
def mark_in_progress(version: PlanVersion, *, actor) -> PlanVersion:
    """Not Started / Rework -> Work in Progress, on the first edit.

    Called by every write path (answers, structured sections) so the status
    trail records when work actually began, or resumed after a rework, rather
    than only when it was submitted. A no-op for any other status.
    """
    # Decide on the LOCKED row, not the instance the caller holds. Two saves a
    # few milliseconds apart both arrive with a Not Started instance; the first
    # moves the row, and the second must see that and do nothing, rather than
    # decide from its stale copy and then fail the transition check.
    locked = PlanVersion.objects.select_for_update().get(pk=version.pk)
    if locked.status not in (PlanStatus.NOT_STARTED, PlanStatus.REWORK):
        return locked
    comment = "Work started." if locked.status == PlanStatus.NOT_STARTED else "Rework started."
    return _move(locked, PlanStatus.WORK_IN_PROGRESS, actor=actor, comments=comment)


@transaction.atomic
def submit(version: PlanVersion, *, actor, comments: str = "") -> PlanVersion:
    """Journey step 6. Refused while any section is incomplete."""
    if version.status in (PlanStatus.NOT_STARTED, PlanStatus.REWORK):
        # Submitting straight from Rework is fine; the trail still shows the
        # intermediate step the transition table requires.
        version = mark_in_progress(version, actor=actor)
    if not version.can_transition_to(PlanStatus.PENDING_BU_LEAD_REVIEW):
        raise InvalidStateTransition(f"A version that is {version.status} cannot be submitted.")
    blockers = incomplete_sections(version)
    if blockers:
        raise PlanIncomplete(blockers)

    moved = _move(
        version,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        actor=actor,
        comments=comments or "Submitted for BU lead review.",
    )
    # A resubmission after rework is a new event; salt on the history row count.
    salt = f"submit-{PlanStatusHistory.objects.filter(plan_version=moved).count()}"
    _notify(
        event=NotificationEvent.PLAN_SUBMITTED,
        version=moved,
        to=[bu_lead_email(moved)],
        cc=coordinator_emails(moved),
        subject=f"BCP plan for {moved.plan.cost_code.cost_code} is ready for your review",
        template="plan_submitted",
        extra={"submitted_by": getattr(actor, "display_name", ""), "comments": comments},
        salt=salt,
    )
    if not bu_lead_email(moved):
        logger.warning("Plan version %s submitted but its cost code has no BU lead email", moved.pk)
    return moved


@transaction.atomic
def approve(version: PlanVersion, *, actor, comments: str = "") -> PlanVersion:
    """Journey step 7, approve. Also queues the document (Phase 7)."""
    moved = _move(version, PlanStatus.APPROVED, actor=actor, comments=comments or "Approved.")
    salt = f"approve-{moved.approved_at.isoformat() if moved.approved_at else ''}"
    _notify(
        event=NotificationEvent.PLAN_APPROVED,
        version=moved,
        to=coordinator_emails(moved),
        cc=[bu_lead_email(moved)],
        subject=f"BCP plan for {moved.plan.cost_code.cost_code} has been approved",
        template="plan_approved",
        extra={"approved_by": getattr(actor, "display_name", ""), "comments": comments},
        salt=salt,
    )

    from apps.core.tasks import enqueue_after_commit
    from apps.documents.tasks import generate_plan_documents

    actor_id = actor.pk if actor else None
    enqueue_after_commit(lambda: generate_plan_documents.delay(moved.pk, actor_id))
    return moved


@transaction.atomic
def rework(version: PlanVersion, *, actor, comments: str) -> PlanVersion:
    """Journey step 7, send back. The comment is what the coordinator acts on."""
    if not (comments or "").strip():
        raise CommentRequired()
    moved = _move(version, PlanStatus.REWORK, actor=actor, comments=comments.strip())
    salt = f"rework-{PlanStatusHistory.objects.filter(plan_version=moved).count()}"
    _notify(
        event=NotificationEvent.PLAN_REWORK,
        version=moved,
        to=coordinator_emails(moved),
        cc=[bu_lead_email(moved)],
        subject=f"BCP plan for {moved.plan.cost_code.cost_code} needs rework",
        template="plan_rework",
        extra={"reviewed_by": getattr(actor, "display_name", ""), "comments": comments.strip()},
        salt=salt,
    )
    return moved
