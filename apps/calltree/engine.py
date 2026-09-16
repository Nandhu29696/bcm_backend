"""
Call tree execution engine (Phase 8).

A run snapshots the cost code's CMSC roster and then escalates each member
through the stages the legacy process used:

    stage 1  VOICE     up to 3 calls
    stage 2  MS_TEAMS  1 call
    stage 3  EMAIL     notify, follow up, escalate to the BU lead
    stage 4  DONE      reached, or every channel exhausted

The engine is driven in rounds: `step_run` gives every open member its next
attempt, then reports whether anything is still outstanding. The Celery task
calls it again after a delay until it says the run is finished. Every attempt
is one `call_attempts` row keyed on (member, channel, attempt_number), so a
round that repeats - a retried task, a redelivered webhook - finds the row it
already wrote and leaves it alone.

Nothing here raises for a provider failure. A provider that errors gets a
"Failed" attempt and the member escalates; the run keeps going and the failure
is on the record.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import record_audit
from apps.calltree.models import (
    CallAttempt,
    CallTreeMember,
    CallTreeRun,
    Channel,
    MemberStage,
    RunStatus,
)
from apps.calltree.providers import ProviderResult, adapter_for, live_providers
from apps.core.exceptions import DomainError
from apps.core.models import AuditLog
from apps.core.tasks import enqueue_after_commit
from apps.crisis.models import CmscMember

logger = logging.getLogger(__name__)

#: Channel and attempt budget per stage, in escalation order.
STAGE_PLAN: dict[int, tuple[str, int]] = {
    MemberStage.VOICE: (Channel.VOICE, 3),
    MemberStage.TEAMS: (Channel.MS_TEAMS, 1),
    MemberStage.EMAIL: (Channel.EMAIL, 3),
}

PENDING_STATUS = "Calling"


class NoRosterMembers(DomainError):
    default_detail = "This cost code has no active CMSC members to call."
    default_code = "no_roster_members"


# --------------------------------------------------------------------------- #
# Starting a run
# --------------------------------------------------------------------------- #


def _run_context(run: CallTreeRun) -> dict:
    cost_code = run.cost_code
    lead = getattr(cost_code, "bu_lead", None) if cost_code else None
    return {
        "call_tree_run_id": run.pk,
        "cost_code": cost_code.cost_code if cost_code else "",
        "process_name": getattr(getattr(cost_code, "process", None), "process_name", ""),
        "estate_name": getattr(getattr(cost_code, "estate", None), "estate_name", ""),
        "bu_lead_name": getattr(lead, "lead_name", ""),
        "bu_lead_email": (getattr(lead, "email", "") or "").strip(),
        "call_tree_type": run.call_tree_type,
        "simulation": run.simulation_flag,
    }


def dial_number(country_code: str, phone_number: str) -> str:
    """E.164 for the provider: "+91" + "98330 01207" -> "+919833001207"."""
    digits = "".join(c for c in (phone_number or "") if c.isdigit())
    if not digits:
        return ""
    if (phone_number or "").strip().startswith("+"):
        return f"+{digits}"
    code = "".join(c for c in (country_code or "") if c.isdigit())
    return f"+{code}{digits}" if code else digits


@transaction.atomic
def start_run(
    *, cost_code, plan_version=None, initiated_by=None, simulation: bool, call_tree_type: str
) -> CallTreeRun:
    """Snapshot the roster and queue the first round. Raises if there is nobody to call."""
    roster = list(
        CmscMember.objects.filter(cost_code=cost_code, active_flag=True).order_by(
            "member_name", "pk"
        )
    )
    if not roster:
        raise NoRosterMembers()

    providers = dict.fromkeys(live_providers(), False) if simulation else live_providers()
    run = CallTreeRun.objects.create(
        cost_code=cost_code,
        plan_version=plan_version,
        process=cost_code.process,
        center=cost_code.center,
        call_tree_type=call_tree_type,
        simulation_flag=simulation,
        providers_enabled=providers,
        initiated_by=initiated_by,
        status=RunStatus.RUNNING,
        started_at=timezone.now(),
    )
    run.broadcast_id = f"BCM-{run.pk:06d}"
    run.save(update_fields=["broadcast_id"])

    CallTreeMember.objects.bulk_create(
        [
            CallTreeMember(
                call_tree_run=run,
                cmsc_member=member,
                employee=member.employee,
                member_name=member.member_name,
                member_email=member.member_email,
                phone_number=dial_number(member.country_code, member.phone_number),
                sequence_number=index,
                escalation_level=MemberStage.VOICE,
            )
            for index, member in enumerate(roster, start=1)
        ]
    )

    if not simulation and any(providers.values()):
        # Reaching real people is a deliberate act. The audit row names who
        # started it and which providers were switched on at the time.
        record_audit(
            action=AuditLog.Action.RECORD_CREATED,
            actor=initiated_by,
            entity_type="CallTreeRun",
            entity_id=run.pk,
            detail={"live": True, "providers_enabled": providers, "members": len(roster)},
        )

    from apps.calltree.tasks import run_call_tree

    enqueue_after_commit(lambda: run_call_tree.delay(run.pk))
    return run


# --------------------------------------------------------------------------- #
# Stepping
# --------------------------------------------------------------------------- #


def next_step(member: CallTreeMember) -> tuple[str, int] | None:
    """The (channel, attempt_number) this member should get next, or None when done."""
    if (
        member.reached_flag
        or member.escalation_level is None
        or member.escalation_level >= MemberStage.DONE
    ):
        return None
    channel, budget = STAGE_PLAN[member.escalation_level]
    made = member.attempts.filter(channel=channel).count()
    if made >= budget:
        return None
    return channel, made + 1


def _escalate(member: CallTreeMember) -> None:
    channel, budget = STAGE_PLAN[member.escalation_level]
    if (
        member.attempts.filter(channel=channel).exclude(attempt_status=PENDING_STATUS).count()
        < budget
    ):
        return
    member.escalation_level += 1
    if member.escalation_level >= MemberStage.DONE:
        member.completed_at = timezone.now()
    member.save(update_fields=["escalation_level", "completed_at"])


def apply_result(attempt: CallAttempt, result: ProviderResult) -> CallAttempt:
    """Record what the provider said and move the member on if warranted."""
    attempt.attempt_status = PENDING_STATUS if result.pending else result.status
    attempt.status_code = result.status_code
    attempt.provider_reference = result.provider_reference or attempt.provider_reference
    attempt.response_key = result.response_key
    attempt.call_duration_seconds = result.duration_seconds
    attempt.comments = result.comments
    attempt.attempted_at = attempt.attempted_at or timezone.now()
    attempt.save()

    if result.pending:
        return attempt

    member = attempt.call_tree_member
    if result.reached:
        member.reached_flag = True
        member.reached_channel = attempt.channel
        member.escalation_level = MemberStage.DONE
        member.completed_at = timezone.now()
        member.save(
            update_fields=["reached_flag", "reached_channel", "escalation_level", "completed_at"]
        )
    else:
        _escalate(member)
    return attempt


def execute_step(member: CallTreeMember, context: dict) -> CallAttempt | None:
    step = next_step(member)
    if step is None:
        return None
    channel, number = step
    attempt, created = CallAttempt.objects.get_or_create(
        call_tree_member=member,
        channel=channel,
        attempt_number=number,
        defaults={"attempt_status": PENDING_STATUS, "attempted_at": timezone.now()},
    )
    if not created:
        # A repeated round found its own row: nothing to do until it resolves.
        return attempt

    adapter = adapter_for(channel, simulation=member.call_tree_run.simulation_flag)
    try:
        result = adapter.send(member, number, {**context, "attempt_id": attempt.pk})
    except Exception as exc:  # noqa: BLE001 - a provider outage must not stop the run
        logger.exception("Call tree attempt %s failed", attempt.pk)
        result = ProviderResult(status="Failed", status_code="error", comments=str(exc)[:500])
    return apply_result(attempt, result)


def expire_pending(run: CallTreeRun) -> int:
    """Attempts the provider never reported back on count as not answered."""
    cutoff = timezone.now() - dt.timedelta(seconds=settings.CALL_TREE_ATTEMPT_TIMEOUT_SECONDS)
    expired = 0
    for attempt in CallAttempt.objects.filter(
        call_tree_member__call_tree_run=run, attempt_status=PENDING_STATUS, attempted_at__lte=cutoff
    ).select_related("call_tree_member"):
        apply_result(
            attempt,
            ProviderResult(
                status="No Answer", status_code="timeout", comments="No response from the provider."
            ),
        )
        expired += 1
    return expired


def step_run(run_id: int) -> bool:
    """One round for every open member. Returns True when the run is finished."""
    run = CallTreeRun.objects.select_related(
        "cost_code__bu_lead", "cost_code__process", "cost_code__estate"
    ).get(pk=run_id)
    if run.status != RunStatus.RUNNING:
        return True
    expire_pending(run)
    context = _run_context(run)

    outstanding = False
    for member in run.members.filter(
        reached_flag=False, escalation_level__lt=MemberStage.DONE
    ).order_by("sequence_number"):
        member.call_tree_run = run
        if member.attempts.filter(attempt_status=PENDING_STATUS).exists():
            outstanding = True
            continue
        execute_step(member, context)
        member.refresh_from_db()
        if (
            not member.reached_flag
            and member.escalation_level < MemberStage.DONE
            or member.attempts.filter(attempt_status=PENDING_STATUS).exists()
        ):
            outstanding = True

    if outstanding:
        return False
    complete_run(run)
    return True


def resolve_attempt(attempt: CallAttempt, result: ProviderResult) -> None:
    """A webhook's verdict on a pending attempt. Idempotent: a replay is a no-op."""
    if attempt.attempt_status != PENDING_STATUS:
        logger.info("Webhook replay for attempt %s ignored", attempt.pk)
        return
    apply_result(attempt, result)
    from apps.calltree.tasks import run_call_tree

    enqueue_after_commit(lambda: run_call_tree.delay(attempt.call_tree_member.call_tree_run_id))


# --------------------------------------------------------------------------- #
# Completion and reporting
# --------------------------------------------------------------------------- #


def complete_run(run: CallTreeRun) -> None:
    run.status = RunStatus.COMPLETED
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "completed_at"])

    initiator_email = (getattr(run.initiated_by, "email", "") or "").strip()
    if initiator_email and not run.simulation_flag:
        from apps.notifications.models import NotificationEvent
        from apps.notifications.services import send_notification

        report = run_report(run)
        send_notification(
            event_type=NotificationEvent.CALL_TREE_SUMMARY,
            to_email=initiator_email,
            subject=f"Call tree {run.broadcast_id} complete: {report['reached']}/{report['members']} reached",
            template_name="call_tree_summary",
            context={**_run_context(run), **report},
            entity_type="CALL_TREE_RUN",
            entity_id=run.pk,
            idempotency_key=f"calltree:{run.pk}:summary",
        )


def run_report(run: CallTreeRun) -> dict:
    """Response rates by escalation level and by channel, plus the member roll."""
    members = list(run.members.prefetch_related("attempts").order_by("sequence_number"))
    total = len(members)
    reached = sum(1 for m in members if m.reached_flag)

    by_level = []
    for stage, (channel, _budget) in STAGE_PLAN.items():
        attempted = [m for m in members if any(a.channel == channel for a in m.attempts.all())]
        reached_here = [m for m in attempted if m.reached_flag and m.reached_channel == channel]
        by_level.append(
            {
                "level": int(stage),
                "label": MemberStage(stage).label,
                "channel": channel,
                "members_attempted": len(attempted),
                "members_reached": len(reached_here),
                "attempts": sum(
                    1 for m in members for a in m.attempts.all() if a.channel == channel
                ),
                "response_rate": (
                    round(len(reached_here) / len(attempted), 3) if attempted else None
                ),
            }
        )

    by_channel = {}
    for member in members:
        for attempt in member.attempts.all():
            row = by_channel.setdefault(
                attempt.channel,
                {"channel": attempt.channel, "attempts": 0, "reached": 0, "statuses": {}},
            )
            row["attempts"] += 1
            row["statuses"][attempt.attempt_status] = (
                row["statuses"].get(attempt.attempt_status, 0) + 1
            )
            if attempt.channel == member.reached_channel and member.reached_flag:
                row["reached"] += 1

    return {
        "members": total,
        "reached": reached,
        "unreached": total - reached,
        "response_rate": round(reached / total, 3) if total else None,
        "by_level": by_level,
        "by_channel": [by_channel[c] for c in Channel.values if c in by_channel],
    }
