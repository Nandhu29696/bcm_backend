"""
Overdue risk-action reminders (Phase 5.5).

An action past its target date and not closed is overdue. Each overdue action
mails its risk's owner once per day; if the risk has no owner with an email,
the version's assigned coordinators are mailed instead, so an orphaned risk
still reaches someone.

Idempotent per (action, recipient, day) through the notification log's key,
so running the job twice in a day - a retried Celery beat, a manual run after
a scheduled one - sends nothing twice. Phase 6 wires this to Celery beat; the
`send_risk_action_reminders` command runs it by hand until then.
"""

from __future__ import annotations

import datetime as dt
import logging

from apps.notifications.models import DeliveryStatus, NotificationEvent, NotificationLog
from apps.notifications.services import build_idempotency_key, send_notification
from apps.plans.models import CoordinatorAssignment
from apps.risk.models import RiskAction
from apps.risk.serializers import CLOSED_ACTION_STATUSES

logger = logging.getLogger(__name__)


def overdue_actions(today: dt.date | None = None):
    today = today or dt.date.today()
    return (
        RiskAction.objects.filter(target_date__lt=today)
        .exclude(status__in=CLOSED_ACTION_STATUSES)
        .select_related(
            "risk",
            "risk__owner_employee",
            "risk__plan_version",
            "risk__plan_version__plan__cost_code",
        )
        .order_by("target_date")
    )


def recipients_for(action: RiskAction) -> list[tuple[str, str]]:
    """(email, display name) pairs: the owner, else the version's coordinators."""
    owner = action.risk.owner_employee
    if owner is not None and owner.email:
        return [(owner.email, owner.full_name)]
    return [
        (a.employee.email, a.employee.full_name)
        for a in CoordinatorAssignment.objects.filter(
            plan_version=action.risk.plan_version, active_flag=True
        ).select_related("employee")
        if a.employee.email
    ]


def send_overdue_reminders(today: dt.date | None = None) -> int:
    """Send today's reminders. Returns how many emails were dispatched."""
    today = today or dt.date.today()
    sent = 0
    for action in overdue_actions(today):
        risk = action.risk
        cost_code = risk.plan_version.plan.cost_code
        for email, name in recipients_for(action):
            key = build_idempotency_key(
                NotificationEvent.RISK_ACTION_OVERDUE, action.pk, email, salt=today.isoformat()
            )
            if NotificationLog.objects.filter(
                idempotency_key=key, status=DeliveryStatus.SENT
            ).exists():
                continue  # already reminded today
            log = send_notification(
                event_type=NotificationEvent.RISK_ACTION_OVERDUE,
                to_email=email,
                subject=f"Overdue: {action.get_action_type_display().lower()} action on {cost_code.cost_code}",
                template_name="risk_action_overdue",
                context={
                    "display_name": name,
                    "cost_code": cost_code.cost_code,
                    "risk_name": risk.risk_name,
                    "action_type": action.get_action_type_display(),
                    "description": action.description,
                    "status": action.status or "not started",
                    "target_date": action.target_date.isoformat(),
                    "days_overdue": (today - action.target_date).days,
                    "plan_version_id": risk.plan_version_id,
                },
                entity_type="RISK_ACTION",
                entity_id=action.pk,
                idempotency_key=key,
            )
            if log.status == DeliveryStatus.SENT:
                sent += 1
    logger.info("Overdue risk-action reminders: %s sent for %s", sent, today)
    return sent
