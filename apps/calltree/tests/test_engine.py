"""
Call tree execution (Phase 8 exit criteria).

    - a run completes in simulation with correct escalation
    - a provider outage degrades gracefully and loses nothing
    - webhook replay does not double-record
    - a switched-off provider is never reached; enabling one is audited
"""

from unittest import mock

import pytest
from django.urls import reverse

from apps.calltree import engine, providers
from apps.calltree.models import CallAttempt, CallTreeMember, Channel, MemberStage, RunStatus
from apps.calltree.providers import ProviderResult, twilio_signature
from apps.core.models import AuditLog
from apps.notifications.models import NotificationLog

pytestmark = pytest.mark.django_db


def start(org, actor, *, simulation=True):
    return engine.start_run(
        cost_code=org["cost_code"],
        initiated_by=actor,
        simulation=simulation,
        call_tree_type="Call tree",
    )


def attempts_of(run, name):
    member = run.members.get(member_name__startswith=name)
    return member, [
        (a.channel, a.attempt_number, a.attempt_status) for a in member.attempts.order_by("pk")
    ]


class TestSimulation:
    def test_run_completes_with_correct_escalation(self, org, actor, roster):
        run = start(org, actor)
        run.refresh_from_db()
        assert run.status == RunStatus.COMPLETED
        assert run.simulation_flag is True
        assert run.providers_enabled == {"VOICE": False, "MS_TEAMS": False, "EMAIL": False}

        asha, trail = attempts_of(run, "Asha")
        assert trail == [
            ("VOICE", 1, "No Answer"),
            ("VOICE", 2, "No Answer"),
            ("VOICE", 3, "Answered"),
        ]
        assert asha.reached_flag and asha.reached_channel == Channel.VOICE
        assert asha.escalation_level == MemberStage.DONE

        bala, trail = attempts_of(run, "Bala")
        assert [t[:2] for t in trail] == [("VOICE", 1), ("VOICE", 2), ("VOICE", 3), ("MS_TEAMS", 1)]
        assert trail[-1][2] == "Acknowledged"
        assert bala.reached_channel == Channel.MS_TEAMS

        chitra, trail = attempts_of(run, "Chitra")
        assert [t[:2] for t in trail] == [
            ("VOICE", 1),
            ("VOICE", 2),
            ("VOICE", 3),
            ("MS_TEAMS", 1),
            ("EMAIL", 1),
            ("EMAIL", 2),
            ("EMAIL", 3),
        ]
        assert [t[2] for t in trail[-3:]] == [
            "Email Sent",
            "Follow up Email Sent",
            "Escalation Email Sent",
        ]
        assert not chitra.reached_flag and chitra.escalation_level == MemberStage.DONE

    def test_report_by_level_and_channel(self, org, actor, roster):
        run = start(org, actor)
        report = engine.run_report(run)
        assert (report["members"], report["reached"], report["unreached"]) == (3, 2, 1)
        by_level = {row["level"]: row for row in report["by_level"]}
        assert by_level[1]["members_attempted"] == 3 and by_level[1]["members_reached"] == 1
        assert by_level[2]["members_attempted"] == 2 and by_level[2]["members_reached"] == 1
        assert by_level[3]["members_attempted"] == 1 and by_level[3]["members_reached"] == 0
        assert by_level[1]["attempts"] == 9
        voice = next(r for r in report["by_channel"] if r["channel"] == "VOICE")
        assert voice["statuses"] == {"No Answer": 8, "Answered": 1}

    def test_simulation_sends_no_email_and_writes_no_live_audit(self, org, actor, roster):
        before = NotificationLog.objects.count()
        start(org, actor)
        assert NotificationLog.objects.count() == before
        assert not AuditLog.objects.filter(entity_type="CallTreeRun").exists()

    def test_repeating_a_round_records_nothing_twice(self, org, actor, roster):
        run = start(org, actor)
        count = CallAttempt.objects.filter(call_tree_member__call_tree_run=run).count()
        assert engine.step_run(run.pk) is True
        assert engine.step_run(run.pk) is True
        assert CallAttempt.objects.filter(call_tree_member__call_tree_run=run).count() == count

    def test_empty_roster_is_refused(self, org, actor, approved_version):
        from apps.crisis.models import CmscMember

        CmscMember.objects.filter(cost_code=org["cost_code"]).update(active_flag=False)
        with pytest.raises(engine.NoRosterMembers):
            start(org, actor)


class TestOutage:
    def test_provider_exception_is_recorded_and_the_run_continues(
        self, org, actor, roster, monkeypatch
    ):
        calls = {"n": 0}

        class Flaky(providers.FakeVoice):
            def send(self, member, attempt_number, context):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ConnectionError("provider down")
                return super().send(member, attempt_number, context)

        original = providers.adapter_for

        def patched(channel, *, simulation):
            return Flaky() if channel == Channel.VOICE else original(channel, simulation=simulation)

        monkeypatch.setattr(engine, "adapter_for", patched)
        run = start(org, actor)
        run.refresh_from_db()
        assert run.status == RunStatus.COMPLETED
        failed = CallAttempt.objects.get(
            call_tree_member__call_tree_run=run, attempt_status="Failed"
        )
        assert failed.status_code == "error" and "provider down" in failed.comments
        # The failed attempt counted against the voice budget; the member still escalated.
        asha, trail = attempts_of(run, "Asha")
        assert len([t for t in trail if t[0] == "VOICE"]) == 3
        assert asha.reached_flag  # answered on attempt 3 all the same


class TestKillSwitch:
    def test_live_run_with_providers_off_never_calls_out(self, org, actor, roster, settings):
        settings.TWILIO_ENABLED = False
        settings.TEAMS_ENABLED = False
        with (
            mock.patch.object(providers.requests, "post") as post,
            mock.patch.object(providers.requests, "get") as get,
        ):
            run = start(org, actor, simulation=False)
        post.assert_not_called()
        get.assert_not_called()
        run.refresh_from_db()
        assert run.status == RunStatus.COMPLETED
        assert run.providers_enabled == {"VOICE": False, "MS_TEAMS": False, "EMAIL": True}
        statuses = set(
            CallAttempt.objects.filter(
                call_tree_member__call_tree_run=run, channel="VOICE"
            ).values_list("attempt_status", flat=True)
        )
        assert statuses == {"Provider disabled"}
        # Email is real: three members x (notify + follow up) plus escalations to the BU lead.
        assert NotificationLog.objects.filter(event_type="CALL_TREE_NOTIFY").count() == 6
        escalations = NotificationLog.objects.filter(event_type="CALL_TREE_ESCALATION")
        assert escalations.count() == 3 and {e.to_email for e in escalations} == {
            "priya@example.com"
        }
        assert (
            "mgr.asha@example.com" in escalations.get(context__member_name="Asha Voice").cc_emails
        )
        # The initiator gets the summary.
        assert NotificationLog.objects.filter(
            event_type="CALL_TREE_SUMMARY", to_email=actor.email
        ).exists()
        # Email alone counts as a live provider: the audit row exists and says so.
        audit = AuditLog.objects.get(entity_type="CallTreeRun", entity_id=run.pk)
        assert audit.detail["providers_enabled"]["VOICE"] is False and audit.detail["live"] is True

    def test_enabling_twilio_reaches_the_provider_and_is_audited(
        self, org, actor, roster, settings
    ):
        settings.TWILIO_ENABLED = True
        settings.TWILIO_ACCOUNT_SID = "ACxxx"
        settings.TWILIO_AUTH_TOKEN = "secret"
        response = mock.Mock(status_code=201)
        response.json.return_value = {"sid": "CA123", "status": "queued"}
        with mock.patch.object(providers.requests, "post", return_value=response) as post:
            run = start(org, actor, simulation=False)
        assert post.call_count == 3  # one call per member, then everything waits on webhooks
        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert (
            CallAttempt.objects.filter(
                call_tree_member__call_tree_run=run, attempt_status="Calling"
            ).count()
            == 3
        )
        audit = AuditLog.objects.get(entity_type="CallTreeRun", entity_id=run.pk)
        assert audit.detail["providers_enabled"]["VOICE"] is True
        assert audit.actor_id == actor.pk


class TestWebhooks:
    @pytest.fixture
    def pending_run(self, org, actor, roster, settings):
        settings.TWILIO_ENABLED = True
        settings.TWILIO_ACCOUNT_SID = "ACxxx"
        settings.TWILIO_AUTH_TOKEN = "secret"
        settings.PUBLIC_BASE_URL = "http://testserver"
        response = mock.Mock(status_code=201)
        counter = {"n": 0}

        def sid(*args, **kwargs):
            counter["n"] += 1
            return {"sid": f"CA{counter['n']}", "status": "queued"}

        response.json.side_effect = sid
        with mock.patch.object(providers.requests, "post", return_value=response):
            run = start(org, actor, simulation=False)
        return run

    def _post(self, client, params, *, sign=True):
        url = reverse("calltree:twilio-status")
        headers = {}
        if sign:
            headers["HTTP_X_TWILIO_SIGNATURE"] = twilio_signature(
                f"http://testserver{url}", params, "secret"
            )
        return client.post(url, params, **headers)

    def test_signed_callback_resolves_and_replay_is_a_noop(self, pending_run, api_client, settings):
        attempt = CallAttempt.objects.get(provider_reference="CA1")
        params = {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "18"}
        with mock.patch.object(
            providers.requests,
            "post",
            return_value=mock.Mock(status_code=201, json=lambda: {"sid": "CAx"}),
        ):
            assert self._post(api_client, params).status_code == 204
        attempt.refresh_from_db()
        assert attempt.attempt_status == "Answered" and attempt.call_duration_seconds == 18
        member = attempt.call_tree_member
        member.refresh_from_db()
        assert member.reached_flag and member.reached_channel == Channel.VOICE

        # Replay: same callback again, with a different duration to prove it is ignored.
        with mock.patch.object(
            providers.requests,
            "post",
            return_value=mock.Mock(status_code=201, json=lambda: {"sid": "CAy"}),
        ):
            assert self._post(api_client, {**params, "CallDuration": "99"}).status_code == 204
        attempt.refresh_from_db()
        assert attempt.call_duration_seconds == 18
        assert CallAttempt.objects.filter(call_tree_member=member).count() == 1

    def test_unsigned_callback_is_refused(self, pending_run, api_client):
        response = self._post(api_client, {"CallSid": "CA1", "CallStatus": "completed"}, sign=False)
        assert response.status_code == 403
        assert CallAttempt.objects.get(provider_reference="CA1").attempt_status == "Calling"

    def test_no_answer_escalates_to_the_next_attempt(self, pending_run, api_client):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"sid": "CA-next", "status": "queued"}
        with mock.patch.object(providers.requests, "post", return_value=response) as post:
            assert (
                self._post(api_client, {"CallSid": "CA1", "CallStatus": "no-answer"}).status_code
                == 204
            )
        post.assert_called_once()
        member = CallAttempt.objects.get(provider_reference="CA1").call_tree_member
        assert list(
            member.attempts.order_by("attempt_number").values_list(
                "attempt_number", "attempt_status"
            )
        ) == [
            (1, "No Answer"),
            (2, "Calling"),
        ]

    def test_timed_out_attempt_expires(self, pending_run, settings):
        import datetime as dt

        from django.utils import timezone

        settings.CALL_TREE_ATTEMPT_TIMEOUT_SECONDS = 60
        CallAttempt.objects.filter(provider_reference="CA1").update(
            attempted_at=timezone.now() - dt.timedelta(minutes=5)
        )
        assert engine.expire_pending(pending_run) == 1
        assert CallAttempt.objects.get(provider_reference="CA1").status_code == "timeout"

    def test_teams_webhook_handshake_and_resolution(self, org, actor, roster, api_client, settings):
        settings.TEAMS_ENABLED = True
        settings.TEAMS_WEBHOOK_CLIENT_STATE = "shared-secret"
        url = reverse("calltree:teams-webhook")
        handshake = api_client.post(f"{url}?validationToken=abc")
        assert handshake.status_code == 200 and handshake.content == b"abc"

        run = start(org, actor)  # simulation, completed
        member = CallTreeMember.objects.filter(call_tree_run=run).first()
        attempt = CallAttempt.objects.create(
            call_tree_member=member,
            channel=Channel.MS_TEAMS,
            attempt_number=9,
            attempt_status="Calling",
            provider_reference="call-1",
        )
        bad = api_client.post(
            url,
            {
                "value": [
                    {
                        "clientState": "wrong",
                        "resourceData": {"id": "call-1", "state": "established"},
                    }
                ]
            },
            format="json",
        )
        assert bad.status_code == 403
        ok = api_client.post(
            url,
            {
                "value": [
                    {
                        "clientState": "shared-secret",
                        "resourceData": {"id": "call-1", "state": "established"},
                    }
                ]
            },
            format="json",
        )
        assert ok.status_code == 202
        attempt.refresh_from_db()
        assert attempt.attempt_status == "Acknowledged"


class TestMonitoringApi:
    def test_run_detail_and_report_are_scoped(
        self, org, actor, roster, coordinator_client, api_client, user_factory
    ):
        run = start(org, actor)
        detail = coordinator_client.get(reverse("calltree:run-detail", args=[run.pk]))
        assert detail.status_code == 200
        assert len(detail.data["members_detail"]) == 3
        assert detail.data["members_detail"][0]["attempts"]
        report = coordinator_client.get(reverse("calltree:run-report", args=[run.pk]))
        assert report.status_code == 200 and report.data["reached"] == 2

        outsider = user_factory(email="other@example.com", roles=["BCM_COORDINATOR"])
        api_client.force_authenticate(user=outsider)
        assert api_client.get(reverse("calltree:run-detail", args=[run.pk])).status_code == 404

    def test_result_helper_defaults(self):
        result = ProviderResult(status="x")
        assert not result.reached and not result.pending
