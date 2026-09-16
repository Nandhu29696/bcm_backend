"""
Crisis events and the CMSC roster (Phase 8).

A crisis event is a record that something happened (or was exercised) for a
cost code. Initiating it optionally starts a call tree run over the cost code's
CMSC roster; the run is linked back so the event page can show it live.

The roster is maintained by hand or by CSV upload. Upload is an upsert keyed
on email, because the legacy list was rebuilt from spreadsheets and the same
person must not appear twice after two uploads.
"""

from __future__ import annotations

import csv
import io
import logging

from django.db import transaction
from django.utils import timezone

from apps.accounts.services import record_audit
from apps.calltree.engine import start_run
from apps.core.exceptions import DomainError, InvalidStateTransition
from apps.core.models import AuditLog
from apps.crisis.models import CmscMember, CrisisEvent
from apps.notifications.models import NotificationEvent
from apps.notifications.services import send_notification
from apps.organization.models import CostCode
from apps.plans.models import CoordinatorAssignment, PlanVersion

logger = logging.getLogger(__name__)

EVENT_TRANSITIONS = {
    CrisisEvent.Status.PLANNED: {CrisisEvent.Status.INITIATED, CrisisEvent.Status.CANCELLED},
    CrisisEvent.Status.INITIATED: {
        CrisisEvent.Status.IN_PROGRESS,
        CrisisEvent.Status.CLOSED,
        CrisisEvent.Status.CANCELLED,
    },
    CrisisEvent.Status.IN_PROGRESS: {CrisisEvent.Status.CLOSED},
    CrisisEvent.Status.CLOSED: set(),
    CrisisEvent.Status.CANCELLED: set(),
}


class CallTreeAlreadyRunning(DomainError):
    default_detail = "A call tree is already running for this event."
    default_code = "call_tree_running"


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #

ROSTER_COLUMNS = {
    "member_name": ("member_name", "cmsc_member", "name"),
    "member_email": ("member_email", "email", "cmsc_member_emailid"),
    "country_code": ("country_code",),
    "phone_number": ("phone_number", "phone"),
    "reporting_manager_name": (
        "reporting_manager_name",
        "reporting_manager's_name",
        "reporting_manager",
    ),
    "reporting_manager_email": (
        "reporting_manager_email",
        "reporting_manager's_email_id.",
        "reporting_manager_email_id",
    ),
    "center": ("center", "centre"),
    "location": ("location",),
}


def _normalise_header(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_")


def normalise_country_code(value: str) -> str:
    """ "91" -> "+91": the legacy sheet stored the dialling code without the plus."""
    digits = "".join(c for c in (value or "") if c.isdigit())
    return f"+{digits}" if digits else ""


def _map_row(row: dict) -> dict:
    keyed = {_normalise_header(k): (v or "").strip() for k, v in row.items() if k is not None}
    out = {}
    for field, aliases in ROSTER_COLUMNS.items():
        out[field] = next((keyed[a] for a in aliases if a in keyed and keyed[a]), "")
    out["country_code"] = normalise_country_code(out["country_code"])
    out["phone_number"] = "".join(c for c in out["phone_number"] if c.isdigit() or c == "+")
    return out


@transaction.atomic
def upload_roster(cost_code: CostCode, content: bytes, *, actor=None) -> dict:
    """Upsert roster rows from a CSV. Returns counts and per-row errors; never partial."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    created = updated = 0
    errors: list[dict] = []
    seen: set[str] = set()

    for index, raw in enumerate(reader, start=2):
        if not any((v or "").strip() for v in raw.values() if v is not None):
            continue  # the legacy exports are padded with blank lines
        row = _map_row(raw)
        if not row["member_name"]:
            errors.append({"line": index, "error": "member_name is required"})
            continue
        if not row["member_email"] and not row["phone_number"]:
            errors.append({"line": index, "error": "an email or a phone number is required"})
            continue
        email = row["member_email"].lower()
        if email and email in seen:
            errors.append({"line": index, "error": f"{email} appears more than once in the file"})
            continue
        seen.add(email)

        existing = (
            CmscMember.all_objects.filter(cost_code=cost_code, member_email__iexact=email).first()
            if email
            else None
        )
        if existing is not None:
            for field, value in row.items():
                setattr(existing, field, value)
            existing.active_flag = True
            existing.plan_version = current_version(cost_code)
            existing.bu_lead = cost_code.bu_lead
            existing.process = cost_code.process
            existing.region = cost_code.region
            existing.save()
            updated += 1
        else:
            CmscMember.objects.create(
                cost_code=cost_code,
                plan_version=current_version(cost_code),
                process=cost_code.process,
                region=cost_code.region,
                bu_lead=cost_code.bu_lead,
                created_by=actor,
                **row,
            )
            created += 1

    if errors:
        transaction.set_rollback(True)
        return {"created": 0, "updated": 0, "errors": errors}

    record_audit(
        action=AuditLog.Action.RECORD_UPDATED,
        actor=actor,
        entity_type="CmscRoster",
        entity_id=cost_code.pk,
        detail={"created": created, "updated": updated},
    )
    return {"created": created, "updated": updated, "errors": []}


def current_version(cost_code: CostCode) -> PlanVersion | None:
    return PlanVersion.objects.filter(plan__cost_code=cost_code).order_by("-version_number").first()


# --------------------------------------------------------------------------- #
# Crisis events
# --------------------------------------------------------------------------- #


def _coordinator_emails(cost_code: CostCode) -> list[str]:
    """The coordinators of the cost code's current version, whatever its status."""
    return [
        email.strip()
        for email in CoordinatorAssignment.objects.filter(
            plan_version=current_version(cost_code), active_flag=True
        )
        .values_list("employee__email", flat=True)
        .distinct()
        if email and email.strip()
    ]


def _event_context(event: CrisisEvent) -> dict:
    cost_code = event.cost_code
    return {
        "crisis_event_id": event.pk,
        "cost_code": cost_code.cost_code,
        "process_name": getattr(cost_code.process, "process_name", ""),
        "estate_name": getattr(cost_code.estate, "estate_name", ""),
        "bu_lead_name": getattr(cost_code.bu_lead, "lead_name", ""),
        "event_type": event.event_type,
        "csd_ticket_number": event.csd_ticket_number,
        "event_date": event.event_date.isoformat() if event.event_date else "",
        "event_time": event.event_time.strftime("%H:%M") if event.event_time else "",
        "comments": event.comments,
        "initiated_by": (
            getattr(event.created_by, "display_name", "") if event.created_by_id else ""
        ),
    }


@transaction.atomic
def create_event(
    cost_code: CostCode, *, actor, data: dict, initiate: bool, simulation: bool
) -> CrisisEvent:
    event = CrisisEvent.objects.create(
        cost_code=cost_code,
        plan_version=current_version(cost_code),
        created_by=actor,
        csd_ticket_number=data.get("csd_ticket_number", ""),
        event_type=data["event_type"],
        comments=data.get("comments", ""),
        event_date=data.get("event_date") or timezone.now().date(),
        event_time=data.get("event_time"),
        status=CrisisEvent.Status.PLANNED,
    )
    record_audit(
        action=AuditLog.Action.RECORD_CREATED,
        actor=actor,
        entity_type="CrisisEvent",
        entity_id=event.pk,
        detail={"event_type": event.event_type, "csd_ticket_number": event.csd_ticket_number},
    )
    if initiate:
        initiate_event(event, actor=actor, simulation=simulation)
    return event


def _move(event: CrisisEvent, to_status: str, *, actor) -> CrisisEvent:
    if to_status not in EVENT_TRANSITIONS[event.status]:
        raise InvalidStateTransition(
            f"A crisis event cannot move from {event.status} to {to_status}."
        )
    previous = event.status
    event.status = to_status
    event.save(update_fields=["status"])
    record_audit(
        action=AuditLog.Action.STATUS_TRANSITION,
        actor=actor,
        entity_type="CrisisEvent",
        entity_id=event.pk,
        detail={"from": previous, "to": to_status},
    )
    return event


@transaction.atomic
def initiate_event(event: CrisisEvent, *, actor, simulation: bool) -> CrisisEvent:
    """Declare the event and start the call tree over the roster."""
    if event.call_tree_run_id and event.call_tree_run.status in ("PENDING", "RUNNING"):
        raise CallTreeAlreadyRunning()
    if event.status == CrisisEvent.Status.PLANNED:
        _move(event, CrisisEvent.Status.INITIATED, actor=actor)
    event.initiated_flag = True

    run = start_run(
        cost_code=event.cost_code,
        plan_version=event.plan_version,
        initiated_by=actor,
        simulation=simulation,
        call_tree_type=event.event_type,
    )
    event.call_tree_run = run
    event.save(update_fields=["initiated_flag", "call_tree_run"])
    if event.status == CrisisEvent.Status.INITIATED:
        # The call tree is under way (or, eager, already done): the event is live.
        _move(event, CrisisEvent.Status.IN_PROGRESS, actor=actor)

    lead_email = (getattr(event.cost_code.bu_lead, "email", "") or "").strip()
    if lead_email and not simulation:
        send_notification(
            event_type=NotificationEvent.CRISIS_INITIATED,
            to_email=lead_email,
            cc_emails=_coordinator_emails(event.cost_code),
            subject=f"Crisis event initiated for {event.cost_code.cost_code}: {event.event_type}",
            template_name="crisis_initiated",
            context=_event_context(event),
            entity_type="CRISIS_EVENT",
            entity_id=event.pk,
            idempotency_key=f"crisis:{event.pk}:initiated:{run.pk}",
        )
    return event


def close_event(event: CrisisEvent, *, actor, comments: str = "") -> CrisisEvent:
    if comments:
        event.comments = f"{event.comments}\n\nClosed: {comments}".strip()
        event.save(update_fields=["comments"])
    return _move(event, CrisisEvent.Status.CLOSED, actor=actor)


def cancel_event(event: CrisisEvent, *, actor) -> CrisisEvent:
    return _move(event, CrisisEvent.Status.CANCELLED, actor=actor)
