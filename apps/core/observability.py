"""
Observability (Phase 10.4): request ids, structured logs, request logging.

Every request gets an id - the caller's `X-Request-ID` if it sent one, else a
fresh one - which is echoed on the response, attached to every log line
emitted while handling it, and forwarded to Sentry. A support ticket with a
request id therefore finds the exact log lines and the exact error event.

`JsonFormatter` is used by the production logging config: one JSON object per
line, which a log shipper can index without a parsing rule. Development keeps
the readable console format.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdFilter(logging.Filter):
    """Stamps the current request id on every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key in ("method", "path", "status", "duration_ms", "user_id", "task"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class RequestIdMiddleware:
    """Assigns the request id, echoes it, and logs one line per request."""

    log = logging.getLogger("apps.request")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = request.headers.get(REQUEST_ID_HEADER, "").strip()
        # Accept a caller's id only if it is plainly an id: no log injection via header.
        safe = bool(supplied) and all(c.isalnum() or c in "-_" for c in supplied)
        request_id = supplied[:64] if safe else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.request_id = request_id
        started = time.perf_counter()
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if not request.path.endswith(("/health/", "/ready/", "/metrics/")):
            user = getattr(request, "user", None)
            self.log.info(
                "%s %s -> %s in %sms",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "user_id": (
                        getattr(user, "pk", None)
                        if user is not None and user.is_authenticated
                        else None
                    ),
                },
            )
        return response


def init_sentry(settings_module) -> bool:
    """Wire Sentry if a DSN is configured. Returns whether it was."""
    dsn = getattr(settings_module, "SENTRY_DSN", "")
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    def add_request_id(event, hint):
        event.setdefault("tags", {})["request_id"] = request_id_var.get()
        return event

    sentry_sdk.init(
        dsn=dsn,
        environment=getattr(settings_module, "SENTRY_ENVIRONMENT", "production"),
        release=getattr(settings_module, "APP_VERSION", None) or None,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=getattr(settings_module, "SENTRY_TRACES_SAMPLE_RATE", 0.0),
        # Never ship user PII or request bodies: plans hold client contract terms.
        send_default_pii=False,
        max_request_body_size="never",
        before_send=add_request_id,
    )
    return True
