"""
Channel adapters for call tree execution (Phase 8).

One interface, five implementations:

    FakeVoice / FakeTeams / FakeEmail   simulation - deterministic, never leaves the process
    TwilioVoice                         live voice, only when TWILIO_ENABLED
    GraphTeams                          live Teams call, only when TEAMS_ENABLED
    EmailChannel                        live email through the notification log
    DisabledProvider                    what a live run gets for a switched-off channel

The kill switch lives in `adapter_for`: a live run asks for a channel and gets
the real adapter only if its flag is on. Otherwise it gets `DisabledProvider`,
which records "Provider disabled" and lets the run escalate past it. There is
no code path that reaches a real provider with the flag off, so a misconfigured
environment cannot call real people.

Fake outcomes are keyed on the last digit of the member's phone number so a
demo roster can exercise every path on purpose:

    0-5   voice answered on attempt (digit % 3) + 1
    6-7   voice never answered, Teams acknowledged
    8-9   voice never answered, Teams not answered, all three emails go out
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Protocol

import requests
from django.conf import settings

from apps.calltree.models import Channel

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15

EMAIL_STATUS = {1: "Email Sent", 2: "Follow up Email Sent", 3: "Escalation Email Sent"}


@dataclass
class ProviderResult:
    """What one attempt came to.

    `pending` means the provider will report the real outcome later through a
    webhook; the attempt stays open until it does or until it times out.
    """

    status: str
    reached: bool = False
    pending: bool = False
    provider_reference: str = ""
    status_code: str = ""
    response_key: str = ""
    duration_seconds: int | None = None
    comments: str = ""


class ChannelAdapter(Protocol):
    channel: str
    live: bool

    def send(self, member, attempt_number: int, context: dict) -> ProviderResult: ...


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


def _last_digit(phone: str) -> int:
    digits = [c for c in (phone or "") if c.isdigit()]
    return int(digits[-1]) if digits else 9


class FakeVoice:
    channel = Channel.VOICE
    live = False

    def send(self, member, attempt_number, context):
        digit = _last_digit(member.phone_number)
        ref = f"SIM-VOICE-{member.pk}-{attempt_number}"
        if digit <= 5 and attempt_number == (digit % 3) + 1:
            return ProviderResult(
                status="Answered",
                reached=True,
                response_key="1",
                duration_seconds=12,
                provider_reference=ref,
                status_code="completed",
            )
        return ProviderResult(status="No Answer", provider_reference=ref, status_code="no-answer")


class FakeTeams:
    channel = Channel.MS_TEAMS
    live = False

    def send(self, member, attempt_number, context):
        ref = f"SIM-TEAMS-{member.pk}"
        if _last_digit(member.phone_number) in (6, 7):
            return ProviderResult(
                status="Acknowledged",
                reached=True,
                provider_reference=ref,
                status_code="established",
            )
        return ProviderResult(
            status="Not Answered", provider_reference=ref, status_code="terminated"
        )


class FakeEmail:
    channel = Channel.EMAIL
    live = False

    def send(self, member, attempt_number, context):
        return ProviderResult(
            status=EMAIL_STATUS[attempt_number],
            provider_reference=f"SIM-EMAIL-{member.pk}-{attempt_number}",
            status_code="sent",
        )


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #


class DisabledProvider:
    """A switched-off live channel. Records the fact and reaches nobody."""

    live = False

    def __init__(self, channel: str):
        self.channel = channel

    def send(self, member, attempt_number, context):
        return ProviderResult(
            status="Provider disabled",
            status_code="disabled",
            comments=f"{self.channel} is not enabled in this environment.",
        )


def _api(path: str) -> str:
    return f"{settings.PUBLIC_BASE_URL}{settings.API_BASE_PATH}{path}"


class TwilioVoice:
    """Places a call through Twilio's REST API and waits for the status webhook."""

    channel = Channel.VOICE
    live = True

    def send(self, member, attempt_number, context):
        if not member.phone_number:
            return ProviderResult(status="No phone number", status_code="skipped")
        sid = settings.TWILIO_ACCOUNT_SID
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
            auth=(sid, settings.TWILIO_AUTH_TOKEN),
            data={
                "To": member.phone_number,
                "From": settings.TWILIO_PHONE_NUMBER_ID,
                "Url": _api(f"/webhooks/twilio/twiml/?attempt={context['attempt_id']}"),
                "StatusCallback": _api("/webhooks/twilio/voice-status/"),
                "StatusCallbackEvent": "completed",
                "Timeout": settings.CALL_TREE_RING_SECONDS,
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        return ProviderResult(
            status="Calling",
            pending=True,
            provider_reference=body.get("sid", ""),
            status_code=body.get("status", "queued"),
        )


def twilio_signature(url: str, params: dict, auth_token: str) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL plus sorted POST params."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class GraphTeams:
    """Places a Teams call through Microsoft Graph's calling API (app-only)."""

    channel = Channel.MS_TEAMS
    live = True

    def _token(self) -> str:
        response = requests.post(
            f"https://login.microsoftonline.com/{settings.MS_GRAPH_TENANT_ID}/oauth2/v2.0/token",
            data={
                "client_id": settings.MS_GRAPH_CLIENT_ID,
                "client_secret": settings.MS_GRAPH_CLIENT_SECRET,
                "scope": settings.MS_GRAPH_SCOPE,
                "grant_type": "client_credentials",
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def send(self, member, attempt_number, context):
        if not member.member_email:
            return ProviderResult(status="No email", status_code="skipped")
        headers = {"Authorization": f"Bearer {self._token()}"}
        user = requests.get(
            f"https://graph.microsoft.com/v1.0/users/{member.member_email}?$select=id",
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        if user.status_code == 404:
            return ProviderResult(status="Not a Teams user", status_code="not-found")
        user.raise_for_status()
        target = {
            "@odata.type": "#microsoft.graph.invitationParticipantInfo",
            "identity": {
                "@odata.type": "#microsoft.graph.identitySet",
                "user": {"@odata.type": "#microsoft.graph.identity", "id": user.json()["id"]},
            },
        }
        call = requests.post(
            "https://graph.microsoft.com/v1.0/communications/calls",
            headers=headers,
            json={
                "@odata.type": "#microsoft.graph.call",
                "callbackUri": _api("/webhooks/teams/"),
                "targets": [target],
                "requestedModalities": ["audio"],
                "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"},
                "clientContext": settings.TEAMS_WEBHOOK_CLIENT_STATE,
            },
            timeout=HTTP_TIMEOUT,
        )
        call.raise_for_status()
        return ProviderResult(
            status="Calling",
            pending=True,
            provider_reference=call.json().get("id", ""),
            status_code="establishing",
        )


class EmailChannel:
    """Three emails: notify, follow up, escalate to the BU lead."""

    channel = Channel.EMAIL
    live = True

    def send(self, member, attempt_number, context):
        from apps.notifications.models import DeliveryStatus, NotificationEvent
        from apps.notifications.services import send_notification

        run = member.call_tree_run
        if attempt_number == 3:
            to_email = context.get("bu_lead_email", "")
            if not to_email:
                return ProviderResult(status="No BU lead email", status_code="skipped")
            cc = [member.reporting_manager_email] if member.reporting_manager_email else []
            event, template = NotificationEvent.CALL_TREE_ESCALATION, "call_tree_escalation"
            subject = (
                f"Escalation: {member.member_name} has not responded to the call tree "
                f"for {context['cost_code']}"
            )
        else:
            if not member.member_email:
                return ProviderResult(status="No email", status_code="skipped")
            to_email, cc = member.member_email, []
            event, template = NotificationEvent.CALL_TREE_NOTIFY, "call_tree_notify"
            prefix = "Follow up: " if attempt_number == 2 else ""
            subject = f"{prefix}Call tree activated for {context['cost_code']} - please respond"
        log = send_notification(
            event_type=event,
            to_email=to_email,
            cc_emails=cc,
            subject=subject,
            template_name=template,
            context={
                **context,
                "member_name": member.member_name,
                "attempt_number": attempt_number,
            },
            entity_type="CALL_TREE_RUN",
            entity_id=run.pk,
            idempotency_key=f"calltree:{run.pk}:{member.pk}:email:{attempt_number}",
        )
        ref = f"notification:{log.pk}"
        if log.status == DeliveryStatus.SENT:
            return ProviderResult(
                status=EMAIL_STATUS[attempt_number], provider_reference=ref, status_code="sent"
            )
        return ProviderResult(
            status="Email failed",
            provider_reference=ref,
            status_code=str(log.status).lower(),
            comments=(log.error_detail or "")[:500],
        )


# --------------------------------------------------------------------------- #
# Selection - the kill switch
# --------------------------------------------------------------------------- #


def live_providers() -> dict[str, bool]:
    """Which live channels this environment may use. Email is always available."""
    return {
        Channel.VOICE: bool(settings.TWILIO_ENABLED),
        Channel.MS_TEAMS: bool(settings.TEAMS_ENABLED),
        Channel.EMAIL: True,
    }


def adapter_for(channel: str, *, simulation: bool) -> ChannelAdapter:
    if simulation:
        return {
            Channel.VOICE: FakeVoice(),
            Channel.MS_TEAMS: FakeTeams(),
            Channel.EMAIL: FakeEmail(),
        }[channel]
    if not live_providers().get(channel):
        return DisabledProvider(channel)
    return {
        Channel.VOICE: TwilioVoice(),
        Channel.MS_TEAMS: GraphTeams(),
        Channel.EMAIL: EmailChannel(),
    }[channel]
