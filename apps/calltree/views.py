"""
Call tree monitoring and provider webhooks (Phase 8).

    GET  /call-tree-runs/{id}/                 live view: members and every attempt
    GET  /call-tree-runs/{id}/report/          response rates by level and channel
    GET  /cost-codes/{id}/call-tree-runs/      history for a cost code

    POST /webhooks/twilio/voice-status/        Twilio call status callback
    GET  /webhooks/twilio/twiml/?attempt=      the call script Twilio fetches
    POST /webhooks/twilio/voice-response/      the key the member pressed
    POST /webhooks/teams/                      Graph call notifications

Webhooks authenticate by signature or shared secret, never by session. Each
one finds its attempt by the provider's reference and hands the verdict to
`engine.resolve_attempt`, which ignores a replay - a second delivery of the
same callback records nothing.
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser
from apps.accounts.scoping import ScopedQuerySetMixin
from apps.calltree import engine
from apps.calltree.models import CallAttempt, CallTreeRun, Channel
from apps.calltree.providers import ProviderResult, twilio_signature
from apps.calltree.serializers import RunDetailSerializer, RunSummarySerializer
from apps.crisis.access import CostCodeScopedMixin

logger = logging.getLogger(__name__)


def _runs_queryset():
    return CallTreeRun.objects.select_related("cost_code", "initiated_by").prefetch_related(
        "members__attempts"
    )


class RunScopedMixin(ScopedQuerySetMixin):
    estate_scope_path = "cost_code__estate_id"
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get_run(self) -> CallTreeRun:
        return get_object_or_404(
            self.scope_queryset(_runs_queryset()), pk=self.kwargs["call_tree_run_id"]
        )


class RunDetailView(RunScopedMixin, APIView):
    @extend_schema(responses=RunDetailSerializer)
    def get(self, request, *args, **kwargs):
        return Response(RunDetailSerializer(self.get_run()).data)


class RunReportView(RunScopedMixin, APIView):
    def get(self, request, *args, **kwargs):
        run = self.get_run()
        return Response({**RunSummarySerializer(run).data, **engine.run_report(run)})


class CostCodeRunsView(CostCodeScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=RunSummarySerializer(many=True))
    def get(self, request, *args, **kwargs):
        cost_code = self.get_cost_code()
        runs = _runs_queryset().filter(cost_code=cost_code).order_by("-started_at")
        return Response(RunSummarySerializer(runs, many=True).data)


# --------------------------------------------------------------------------- #
# Twilio
# --------------------------------------------------------------------------- #


TWILIO_OUTCOMES = {
    "completed": ("Answered", True),
    "busy": ("Busy", False),
    "no-answer": ("No Answer", False),
    "failed": ("Failed", False),
    "canceled": ("Cancelled", False),
}


def _twilio_request_ok(request) -> bool:
    if not settings.TWILIO_ENABLED or not settings.TWILIO_AUTH_TOKEN:
        return False
    url = f"{settings.PUBLIC_BASE_URL}{request.get_full_path()}"
    expected = twilio_signature(
        url, {k: request.POST[k] for k in request.POST}, settings.TWILIO_AUTH_TOKEN
    )
    return expected == request.headers.get("X-Twilio-Signature", "")


class TwilioStatusWebhook(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        if not _twilio_request_ok(request):
            return Response(status=http_status.HTTP_403_FORBIDDEN)
        sid = request.POST.get("CallSid", "")
        attempt = (
            CallAttempt.objects.filter(provider_reference=sid, channel=Channel.VOICE)
            .select_related("call_tree_member")
            .first()
        )
        if attempt is None:
            logger.warning("Twilio status for unknown call %s", sid)
            return Response(status=http_status.HTTP_204_NO_CONTENT)
        call_status = request.POST.get("CallStatus", "")
        label, reached = TWILIO_OUTCOMES.get(call_status, (call_status or "Unknown", False))
        duration = request.POST.get("CallDuration")
        engine.resolve_attempt(
            attempt,
            ProviderResult(
                status=label,
                reached=reached,
                status_code=call_status,
                provider_reference=sid,
                duration_seconds=int(duration) if duration and duration.isdigit() else None,
                response_key=attempt.response_key,
            ),
        )
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class TwilioTwimlView(APIView):
    """The script read to the member. Fetched by Twilio, not by a browser."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, *args, **kwargs):
        attempt = (
            CallAttempt.objects.filter(pk=request.query_params.get("attempt", 0))
            .select_related("call_tree_member__call_tree_run__cost_code")
            .first()
        )
        cost_code = attempt.call_tree_member.call_tree_run.cost_code.cost_code if attempt else ""
        action = (
            f"{settings.PUBLIC_BASE_URL}{settings.API_BASE_PATH}/webhooks/twilio/voice-response/"
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather numDigits="1" action="{escape(action)}" method="POST">'
            f"<Say>This is a business continuity call tree notification for cost code {escape(cost_code)}. "
            "Press 1 to confirm you have received this message.</Say></Gather>"
            "<Say>No input received. Goodbye.</Say></Response>"
        )
        return HttpResponse(twiml, content_type="text/xml")


class TwilioResponseWebhook(APIView):
    """Records the pressed key; the status callback still decides the outcome."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        if not _twilio_request_ok(request):
            return Response(status=http_status.HTTP_403_FORBIDDEN)
        sid = request.POST.get("CallSid", "")
        digits = request.POST.get("Digits", "")
        CallAttempt.objects.filter(provider_reference=sid, channel=Channel.VOICE).update(
            response_key=digits[:10]
        )
        return HttpResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Thank you.</Say></Response>',
            content_type="text/xml",
        )


# --------------------------------------------------------------------------- #
# Microsoft Teams (Graph calling)
# --------------------------------------------------------------------------- #


class TeamsWebhook(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        # Subscription validation handshake: echo the token as plain text.
        token = request.query_params.get("validationToken")
        if token:
            return HttpResponse(token, content_type="text/plain")
        if not settings.TEAMS_ENABLED or not settings.TEAMS_WEBHOOK_CLIENT_STATE:
            return Response(status=http_status.HTTP_403_FORBIDDEN)
        for item in (request.data or {}).get("value", []):
            resource = item.get("resourceData") or {}
            if (
                item.get("clientState", resource.get("clientContext"))
                != settings.TEAMS_WEBHOOK_CLIENT_STATE
            ):
                return Response(status=http_status.HTTP_403_FORBIDDEN)
            call_id = resource.get("id") or item.get("resource", "").rsplit("/", 1)[-1]
            state = resource.get("state", "")
            attempt = CallAttempt.objects.filter(
                provider_reference=call_id, channel=Channel.MS_TEAMS
            ).first()
            if attempt is None:
                continue
            if state == "established":
                engine.resolve_attempt(
                    attempt,
                    ProviderResult(
                        status="Acknowledged",
                        reached=True,
                        status_code=state,
                        provider_reference=call_id,
                    ),
                )
            elif state == "terminated":
                engine.resolve_attempt(
                    attempt,
                    ProviderResult(
                        status="Not Answered", status_code=state, provider_reference=call_id
                    ),
                )
        return Response(status=http_status.HTTP_202_ACCEPTED)
