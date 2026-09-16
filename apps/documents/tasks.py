"""Document generation as a Celery task (Phase 7.1)."""

from __future__ import annotations

import logging

from celery import shared_task

from apps.documents.generation import generate_for_version
from apps.plans.models import PlanVersion

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="apps.documents.tasks.generate_plan_documents", max_retries=3)
def generate_plan_documents(self, plan_version_id: int, actor_id: int | None = None) -> int:
    """Render and attach the documents for a version. Returns how many were attached."""
    from django.contrib.auth import get_user_model

    version = (
        PlanVersion.objects.filter(pk=plan_version_id).select_related("plan__cost_code").first()
    )
    if version is None:
        logger.warning("Plan version %s vanished before document generation", plan_version_id)
        return 0
    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    try:
        return len(generate_for_version(version, actor=actor))
    except Exception as exc:
        logger.exception("Document generation failed for version %s", plan_version_id)
        if self.request.is_eager or self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1)) from exc
