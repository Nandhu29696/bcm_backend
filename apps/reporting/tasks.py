"""Report generation and the schedule sweep (Celery)."""

from celery import shared_task

from apps.reporting.services import due_requests, run_request


@shared_task(name="apps.reporting.tasks.run_report_request")
def run_report_request(request_id: int) -> str:
    return run_request(request_id).status


@shared_task(name="apps.reporting.tasks.run_scheduled_reports")
def run_scheduled_reports() -> int:
    """Beat job: run every scheduled request whose time has come."""
    count = 0
    for request_id in due_requests().values_list("pk", flat=True):
        run_report_request.delay(request_id)
        count += 1
    return count
