"""
Exemptions (Phase 7.5).

A coordinator asks for a plan version to be excused from the full BCP cycle
(the process is being decommissioned, say). The BU lead decides:

    request   ->  Pending          emails TO the BU lead, CC coordinators
    approve   ->  Approved         emails the requester; the plan version -> Exempted
    reject    ->  Rejected         emails the requester
    rework    ->  Rework           emails the requester (comment required)

Every decision writes a typed comment on the exemption's trail. Only one open
exemption per version, and the version must still be editable: an approved
plan does not need excusing.
"""

from __future__ import annotations

from django.db import transaction

from apps.accounts.services import record_audit
from apps.core.exceptions import DomainError, InvalidStateTransition
from apps.core.models import AuditLog
from apps.exemptions.models import CommentType, Exemption, ExemptionComment, ExemptionStatus
from apps.notifications.models import NotificationEvent
from apps.notifications.services import build_idempotency_key, send_notification
from apps.plans.models import PlanStatus, PlanStatusHistory, PlanVersion
from apps.plans.workflow import _plan_context, bu_lead_email, coordinator_emails, mark_in_progress

#: Decisions that can be made on an exemption in each status.
EXEMPTION_TRANSITIONS: dict[str, tuple[str, ...]] = {
    ExemptionStatus.PENDING: (
        ExemptionStatus.APPROVED,
        ExemptionStatus.REJECTED,
        ExemptionStatus.REWORK,
    ),
    ExemptionStatus.REWORK: (ExemptionStatus.PENDING,),  # the requester resubmits
    ExemptionStatus.APPROVED: (),
    ExemptionStatus.REJECTED: (),
}

OPEN_EXEMPTION_STATUSES = (ExemptionStatus.PENDING, ExemptionStatus.REWORK)


class ExemptionNotAllowed(DomainError):
    default_code = "exemption_not_allowed"


class ReasonRequired(DomainError):
    default_code = "reason_required"
    default_detail = "Give a reason for the exemption request."


def _requester_email(exemption: Exemption) -> str:
    user = exemption.requested_by
    if user is None:
        return ""
    if user.email:
        return user.email
    employee = getattr(user, "employee", None)
    return getattr(employee, "email", "") or ""


def _notify_decision(exemption: Exemption, *, actor, comment: str) -> None:
    version = exemption.plan_version
    # The requester, or - if they have no address - the version's coordinators.
    send_to = [_requester_email(exemption)]
    if not send_to[0]:
        send_to = coordinator_emails(version)
    for address in dict.fromkeys(send_to):
        send_notification(
            event_type=NotificationEvent.EXEMPTION_DECIDED,
            to_email=address,
            cc_emails=[e for e in coordinator_emails(version) if e != address],
            subject=f"Exemption request for {version.plan.cost_code.cost_code}: {exemption.status}",
            template_name="exemption_decided",
            context={
                **_plan_context(version),
                "decision": exemption.status,
                "decided_by": getattr(actor, "display_name", ""),
                "comment": comment,
                "exemption_id": exemption.pk,
            },
            entity_type="EXEMPTION",
            entity_id=exemption.pk,
            idempotency_key=build_idempotency_key(
                NotificationEvent.EXEMPTION_DECIDED,
                exemption.pk,
                address,
                salt=f"{exemption.status}-{exemption.comments.count()}",
            ),
        )


@transaction.atomic
def request_exemption(
    version: PlanVersion, *, actor, reason: str, answers: dict | None = None
) -> Exemption:
    reason = (reason or "").strip()
    if not reason:
        raise ReasonRequired()
    # Any still-open version can ask - including one never started, which is
    # the common case for a process being decommissioned. Approval routes it
    # through Work in Progress so the transition table is honoured.
    if not version.is_editable:
        raise ExemptionNotAllowed(
            f"A version that is {version.status} cannot be exempted; it is already closed or under review."
        )
    if Exemption.objects.filter(plan_version=version, status__in=OPEN_EXEMPTION_STATUSES).exists():
        raise ExemptionNotAllowed("An exemption request is already open for this version.")

    answers = answers or {}
    exemption = Exemption.objects.create(
        plan_version=version,
        requested_by=actor,
        reason=reason,
        answer_1=(answers.get("answer_1") or "")[:500],
        answer_2=(answers.get("answer_2") or "")[:500],
        answer_3=(answers.get("answer_3") or "")[:500],
    )
    ExemptionComment.objects.create(
        exemption=exemption,
        author=actor,
        comment_type=CommentType.GENERAL,
        comment=reason,
        status=ExemptionStatus.PENDING,
    )
    record_audit(
        action=AuditLog.Action.RECORD_CREATED,
        actor=actor,
        entity_type="Exemption",
        entity_id=exemption.pk,
        detail={"plan_version_id": version.pk},
    )

    lead = bu_lead_email(version)
    if lead:
        send_notification(
            event_type=NotificationEvent.EXEMPTION_SUBMITTED,
            to_email=lead,
            cc_emails=coordinator_emails(version),
            subject=f"Exemption requested for BCP plan {version.plan.cost_code.cost_code}",
            template_name="exemption_submitted",
            context={
                **_plan_context(version),
                "requested_by": getattr(actor, "display_name", ""),
                "reason": reason,
                "exemption_id": exemption.pk,
            },
            entity_type="EXEMPTION",
            entity_id=exemption.pk,
            idempotency_key=build_idempotency_key(
                NotificationEvent.EXEMPTION_SUBMITTED, exemption.pk, lead
            ),
        )
    return exemption


def _decide(
    exemption: Exemption, to_status: str, *, actor, comment: str, comment_type: str
) -> Exemption:
    locked = (
        Exemption.objects.select_for_update()
        .select_related("plan_version__plan__cost_code")
        .get(pk=exemption.pk)
    )
    if to_status not in EXEMPTION_TRANSITIONS.get(locked.status, ()):
        raise InvalidStateTransition(
            f"An exemption that is {locked.status} cannot move to {to_status}."
        )
    locked.status = to_status
    locked.save(update_fields=["status", "updated_at"])
    ExemptionComment.objects.create(
        exemption=locked,
        author=actor,
        comment_type=comment_type,
        comment=comment or f"{to_status}.",
        status=to_status,
    )
    record_audit(
        action=AuditLog.Action.STATUS_TRANSITION,
        actor=actor,
        entity_type="Exemption",
        entity_id=locked.pk,
        detail={"to": to_status, "comment": comment},
    )
    return locked


@transaction.atomic
def approve_exemption(exemption: Exemption, *, actor, comment: str = "") -> Exemption:
    """Approving excuses the plan: its version moves to Exempted (terminal)."""
    decided = _decide(
        exemption,
        ExemptionStatus.APPROVED,
        actor=actor,
        comment=comment,
        comment_type=CommentType.APPROVAL,
    )
    version = PlanVersion.objects.select_for_update().get(pk=decided.plan_version_id)
    version = mark_in_progress(version, actor=actor)
    if not version.can_transition_to(PlanStatus.EXEMPTED):
        raise InvalidStateTransition(f"A version that is {version.status} cannot be exempted.")
    version.status = PlanStatus.EXEMPTED
    version.updated_by = actor
    version.save(update_fields=["status", "updated_by", "updated_at"])
    PlanStatusHistory.objects.create(
        plan_version=version,
        status=PlanStatus.EXEMPTED,
        comments=f"Exemption approved: {comment}" if comment else "Exemption approved.",
        changed_by=actor,
    )
    _notify_decision(decided, actor=actor, comment=comment)
    return decided


@transaction.atomic
def reject_exemption(exemption: Exemption, *, actor, comment: str = "") -> Exemption:
    decided = _decide(
        exemption,
        ExemptionStatus.REJECTED,
        actor=actor,
        comment=comment,
        comment_type=CommentType.GENERAL,
    )
    _notify_decision(decided, actor=actor, comment=comment)
    return decided


@transaction.atomic
def rework_exemption(exemption: Exemption, *, actor, comment: str) -> Exemption:
    if not (comment or "").strip():
        raise DomainError(
            "Say what the request needs before sending it back.", code="comment_required"
        )
    decided = _decide(
        exemption,
        ExemptionStatus.REWORK,
        actor=actor,
        comment=comment.strip(),
        comment_type=CommentType.REWORK,
    )
    _notify_decision(decided, actor=actor, comment=comment.strip())
    return decided


@transaction.atomic
def resubmit_exemption(exemption: Exemption, *, actor, reason: str) -> Exemption:
    """After a rework, the requester updates the reason and sends it again."""
    reason = (reason or "").strip()
    if not reason:
        raise ReasonRequired()
    decided = _decide(
        exemption,
        ExemptionStatus.PENDING,
        actor=actor,
        comment=reason,
        comment_type=CommentType.GENERAL,
    )
    decided.reason = reason
    decided.save(update_fields=["reason", "updated_at"])
    return decided
