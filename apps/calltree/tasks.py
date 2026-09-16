"""Call tree execution as Celery tasks."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from apps.calltree.engine import PENDING_STATUS, step_run
from apps.calltree.models import CallAttempt, CallTreeRun, RunStatus

logger = logging.getLogger(__name__)

#: A safety net for the inline loop: 7 attempts per member at most, plus slack.
MAX_INLINE_ROUNDS = 12


@shared_task(bind=True, name="apps.calltree.tasks.run_call_tree")
def run_call_tree(self, run_id: int) -> bool:
    """Advance a run one round; come back later if members are still open.

    With a worker, the next round is scheduled after CALL_TREE_RETRY_SECONDS so
    a member who did not answer is not redialled instantly. Eager (development,
    tests) runs the rounds back to back, which is what a simulation wants - and
    stops if an attempt is waiting on a webhook, since nothing inline can
    resolve it.
    """
    if self.request.is_eager:
        for _ in range(MAX_INLINE_ROUNDS):
            if step_run(run_id):
                return True
            if CallAttempt.objects.filter(
                call_tree_member__call_tree_run_id=run_id, attempt_status=PENDING_STATUS
            ).exists():
                return False
        logger.warning(
            "Call tree run %s did not finish within %s inline rounds", run_id, MAX_INLINE_ROUNDS
        )
        return False

    if step_run(run_id):
        return True
    self.apply_async(args=[run_id], countdown=settings.CALL_TREE_RETRY_SECONDS)
    return False


@shared_task(name="apps.calltree.tasks.sweep_running_runs")
def sweep_running_runs() -> int:
    """Beat job: nudge every running run so timed-out attempts expire and escalate."""
    count = 0
    for run_id in CallTreeRun.objects.filter(status=RunStatus.RUNNING).values_list("pk", flat=True):
        run_call_tree.delay(run_id)
        count += 1
    return count
