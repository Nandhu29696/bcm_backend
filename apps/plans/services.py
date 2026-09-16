"""
Plan-level operations that are more than a model write (Phase 3).

Anything with a side effect beyond the row itself lives here rather than in a
serializer or view: assignment sends mail, edits write audit entries, and
transitions (Phase 6) will write history. Keeping them in one place is what lets
Phase 6 reuse them from a Celery task without dragging a request object along.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apps.accounts.services import record_audit
from apps.core.models import AuditLog
from apps.notifications.models import NotificationEvent
from apps.notifications.services import build_idempotency_key, send_notification
from apps.plans.models import CoordinatorAssignment, PlanVersion

logger = logging.getLogger(__name__)


def _plan_context(plan_version: PlanVersion) -> dict:
    """Shared email context describing which plan is being talked about."""
    cost_code = plan_version.plan.cost_code
    return {
        "plan_version_id": plan_version.pk,
        "version_number": plan_version.version_number,
        "status": plan_version.status,
        "cost_code": cost_code.cost_code,
        "process_name": getattr(cost_code.process, "process_name", ""),
        "estate_name": getattr(cost_code.estate, "estate_name", ""),
    }


def notify_coordinator_assigned(assignment: CoordinatorAssignment, *, actor=None) -> None:
    """Tell a newly-assigned coordinator that work is waiting for them.

    A failure to send is recorded on the notification log, never raised: the
    assignment itself has already been made, and unwinding it because an SMTP
    server was briefly unreachable would be the wrong trade. The log row is what
    makes the missed email recoverable.
    """
    employee = assignment.employee
    recipient = (employee.email or "").strip()
    if not recipient:
        logger.warning(
            "Coordinator %s has no email address; assignment %s not notified.",
            employee.pk,
            assignment.pk,
        )
        return

    context = _plan_context(assignment.plan_version)
    context.update(
        {
            "display_name": employee.full_name,
            "coordinator_type": assignment.coordinator_type,
            "assigned_by": getattr(actor, "display_name", "") or "",
        }
    )

    send_notification(
        event_type=NotificationEvent.COORDINATOR_ASSIGNED,
        to_email=recipient,
        subject=f"You are the coordinator for {context['cost_code']}",
        template_name="coordinator_assigned",
        context=context,
        # Reverse one-to-one: Django raises RelatedObjectDoesNotExist, which
        # subclasses AttributeError, so the default applies for an employee with
        # no login.
        recipient_user=getattr(employee, "user_account", None),
        entity_type="PLAN_VERSION",
        entity_id=assignment.plan_version_id,
        # Keyed on the assignment AND its last-modified stamp. Keyed on the
        # assignment alone, a coordinator removed and later re-assigned would
        # never hear about it: the row is reactivated rather than recreated, so
        # the key would collide with the send from the first time round and be
        # suppressed. Including `updated_at` means a genuine reassignment mails
        # again, while a retry of the *same* send still collides and does not.
        idempotency_key=build_idempotency_key(
            NotificationEvent.COORDINATOR_ASSIGNED,
            assignment.pk,
            recipient,
            salt=assignment.updated_at.isoformat() if assignment.updated_at else "",
        ),
    )


@transaction.atomic
def assign_coordinator(
    *,
    plan_version: PlanVersion,
    employee,
    coordinator_type: str = "",
    additional_user_flag: bool = False,
    actor=None,
    request=None,
) -> CoordinatorAssignment:
    """Assign a coordinator to a plan version and notify them.

    Re-assigning someone previously removed reactivates the existing row rather
    than creating a second one — the unique constraint on
    (plan_version, employee, coordinator_type) would reject the insert, and a soft
    delete must be reversible without leaving a hole in the audit trail.
    """
    assignment, created = CoordinatorAssignment.all_objects.get_or_create(
        plan_version=plan_version,
        employee=employee,
        coordinator_type=coordinator_type,
        defaults={
            "estate": plan_version.plan.cost_code.estate,
            "additional_user_flag": additional_user_flag,
            "active_flag": True,
        },
    )

    if not created:
        reactivated = not assignment.active_flag
        assignment.active_flag = True
        assignment.additional_user_flag = additional_user_flag
        assignment.estate = plan_version.plan.cost_code.estate
        # `updated_at` is listed explicitly: with `update_fields`, Django writes
        # only the named columns, so an `auto_now` field left out is never
        # refreshed. The notification's idempotency key is derived from it.
        assignment.save(
            update_fields=["active_flag", "additional_user_flag", "estate", "updated_at"]
        )
        if not reactivated:
            # Already active and unchanged — no second email.
            return assignment

    record_audit(
        action=AuditLog.Action.RECORD_CREATED,
        actor=actor,
        entity_type="CoordinatorAssignment",
        entity_id=assignment.pk,
        detail={
            "plan_version_id": plan_version.pk,
            "employee_id": employee.pk,
            "coordinator_type": coordinator_type,
        },
        request=request,
    )

    # Sent after the row is committed, so a rollback cannot leave a coordinator
    # holding an email about an assignment that does not exist.
    transaction.on_commit(lambda: notify_coordinator_assigned(assignment, actor=actor))
    return assignment


@transaction.atomic
def remove_coordinator(assignment: CoordinatorAssignment, *, actor=None, request=None) -> None:
    """Soft-delete an assignment (AD-6). History must still show it existed."""
    assignment.soft_delete()
    record_audit(
        action=AuditLog.Action.RECORD_DELETED,
        actor=actor,
        entity_type="CoordinatorAssignment",
        entity_id=assignment.pk,
        detail={
            "plan_version_id": assignment.plan_version_id,
            "employee_id": assignment.employee_id,
        },
        request=request,
    )


def record_cost_code_change(*, cost_code, before: dict, actor=None, request=None) -> None:
    """Audit a cost code edit, recording only what actually changed.

    Logging the whole row on every save buries the one field that moved, which is
    the only thing anyone reads an audit trail to find.
    """
    after = {key: getattr(cost_code, key) for key in before}
    changed = {
        key: {"from": before[key], "to": after[key]} for key in before if before[key] != after[key]
    }
    if not changed:
        return

    record_audit(
        action=AuditLog.Action.RECORD_UPDATED,
        actor=actor,
        entity_type="CostCode",
        entity_id=cost_code.pk,
        detail={"changed": changed},
        request=request,
    )
