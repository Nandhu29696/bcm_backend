"""Task dispatch helpers (AD-9)."""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.db import transaction


def enqueue_after_commit(enqueue: Callable[[], object]) -> None:
    """Run `enqueue` once the surrounding transaction commits.

    With a broker, a task must not start until the rows it will read are
    committed - a worker rendering a version the transaction then rolls back
    is the kind of bug that surfaces months later as a document for a plan
    that was never approved. In eager mode (development, tests) the task runs
    inline on this same connection, so "after commit" has no meaning, and
    waiting for one means nothing ever runs inside a test transaction.
    """
    if settings.CELERY_TASK_ALWAYS_EAGER:
        enqueue()
    else:
        transaction.on_commit(enqueue)


from celery import shared_task  # noqa: E402


@shared_task(name="apps.core.tasks.apply_retention")
def apply_retention_task() -> dict:
    """Beat job: sweep everything past its configured retention period."""
    from apps.core.retention import apply_retention

    return apply_retention()
