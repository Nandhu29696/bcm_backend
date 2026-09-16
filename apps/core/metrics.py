"""
Application metrics in Prometheus text format (Phase 10.4).

    GET /api/v1/metrics/        Authorization: Bearer <METRICS_TOKEN>

Gauges a scraper can alert on: database round-trip time, Celery queue depth
(when the broker is reachable), and the backlog counters that tell you a
worker has stopped - pending notifications, running call trees, pending
report requests - plus the business counters the dashboards show.

Protected by a shared token rather than a user session because the scraper is
a machine. With no METRICS_TOKEN configured the endpoint does not exist (404),
so a forgotten setting cannot expose it.
"""

from __future__ import annotations

import hmac
import time

from django.conf import settings
from django.db import connection
from django.http import Http404, HttpResponse
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView


def _line(name: str, value, help_text: str, kind: str = "gauge", labels: dict | None = None) -> str:
    label = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
    return f"# HELP {name} {help_text}\n# TYPE {name} {kind}\n{name}{label} {value}\n"


def celery_queue_depth() -> int | None:
    """Length of the default Celery queue, or None when the broker is not reachable."""
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
        )
        return int(client.llen("celery"))
    except Exception:  # noqa: BLE001 - a missing broker is what the metric reports
        return None


def collect() -> str:
    from apps.calltree.models import CallTreeRun, RunStatus
    from apps.notifications.models import DeliveryStatus, NotificationLog
    from apps.organization.models import CostCode
    from apps.organization.querysets import CURRENT_STATUS, with_current_status
    from apps.plans.models import PlanStatus
    from apps.reporting.models import ReportRequest, RequestStatus

    out = []
    started = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    out.append(
        _line(
            "bcm_db_roundtrip_seconds",
            round(time.perf_counter() - started, 4),
            "Time for SELECT 1 against the primary database.",
        )
    )

    depth = celery_queue_depth()
    out.append(
        _line(
            "bcm_celery_queue_depth",
            -1 if depth is None else depth,
            "Tasks waiting on the default Celery queue (-1: broker unreachable or eager).",
        )
    )
    out.append(
        _line(
            "bcm_notifications_pending",
            NotificationLog.objects.filter(status=DeliveryStatus.PENDING).count(),
            "Notifications not yet delivered.",
        )
    )
    out.append(
        _line(
            "bcm_notifications_failed",
            NotificationLog.objects.filter(status=DeliveryStatus.FAILED).count(),
            "Notifications that exhausted their retries.",
        )
    )
    out.append(
        _line(
            "bcm_call_tree_runs_running",
            CallTreeRun.objects.filter(status=RunStatus.RUNNING).count(),
            "Call tree runs in progress.",
        )
    )
    out.append(
        _line(
            "bcm_report_requests_pending",
            ReportRequest.objects.filter(status=RequestStatus.PENDING).count(),
            "Report requests not yet built.",
        )
    )

    counts = {s: 0 for s, _ in PlanStatus.choices}
    from django.db.models import Count

    for row in (
        with_current_status(CostCode.objects.all())
        .order_by()
        .values(CURRENT_STATUS)
        .annotate(n=Count("cost_code_id"))
    ):
        counts[row[CURRENT_STATUS]] = row["n"]
    for status, n in counts.items():
        out.append(
            _line(
                "bcm_cost_codes_by_status",
                n,
                "Cost codes by resolved BCP status.",
                labels={"status": status},
            )
        )
    return "".join(out)


class MetricsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, *args, **kwargs):
        token = settings.METRICS_TOKEN
        if not token:
            raise Http404
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, token):
            return HttpResponse(status=401)
        return HttpResponse(collect(), content_type="text/plain; version=0.0.4; charset=utf-8")
