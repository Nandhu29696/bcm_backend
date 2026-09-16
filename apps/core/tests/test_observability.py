"""Request ids, structured logs, metrics and the backup manifest (Phase 10.4-10.5)."""

import json
import logging

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.core.observability import JsonFormatter, RequestIdFilter, request_id_var

pytestmark = pytest.mark.django_db


class TestRequestId:
    def test_every_response_carries_a_request_id(self, api_client):
        response = api_client.get(reverse("core:health"))
        assert response.status_code == 200
        assert len(response["X-Request-ID"]) == 32

    def test_a_callers_id_is_echoed_and_an_unsafe_one_replaced(self, api_client):
        echoed = api_client.get(reverse("core:health"), HTTP_X_REQUEST_ID="trace-abc-123")
        assert echoed["X-Request-ID"] == "trace-abc-123"
        replaced = api_client.get(reverse("core:health"), HTTP_X_REQUEST_ID="evil\nInjected: yes")
        assert (
            replaced["X-Request-ID"] != "evil\nInjected: yes"
            and len(replaced["X-Request-ID"]) == 32
        )

    def test_request_line_is_logged_with_status_and_duration(self, api_client, caplog):
        with caplog.at_level(logging.INFO, logger="apps.request"):
            api_client.get("/api/v1/estates/")
        record = next(r for r in caplog.records if r.name == "apps.request")
        assert record.path == "/api/v1/estates/" and record.status == 401
        assert record.duration_ms >= 0 and record.request_id == record.request_id

    def test_json_formatter_emits_one_object_per_line(self):
        token = request_id_var.set("req-1")
        try:
            record = logging.LogRecord(
                "apps.x", logging.WARNING, __file__, 1, "hello %s", ("world",), None
            )
            RequestIdFilter().filter(record)
            line = JsonFormatter().format(record)
        finally:
            request_id_var.reset(token)
        payload = json.loads(line)
        assert (
            payload["message"] == "hello world"
            and payload["level"] == "WARNING"
            and payload["request_id"] == "req-1"
        )


class TestMetrics:
    def test_absent_without_a_token_and_guarded_with_one(self, settings, api_client, org):
        settings.METRICS_TOKEN = ""
        assert api_client.get(reverse("core:metrics")).status_code == 404
        settings.METRICS_TOKEN = "scrape-secret"
        assert api_client.get(reverse("core:metrics")).status_code == 401
        assert (
            api_client.get(reverse("core:metrics"), HTTP_AUTHORIZATION="Bearer wrong").status_code
            == 401
        )
        response = api_client.get(
            reverse("core:metrics"), HTTP_AUTHORIZATION="Bearer scrape-secret"
        )
        assert response.status_code == 200 and response["Content-Type"].startswith("text/plain")
        body = response.content.decode()
        assert "bcm_db_roundtrip_seconds" in body
        assert 'bcm_cost_codes_by_status{status="Not Started"} 2' in body
        assert "bcm_notifications_pending 0" in body
        assert "bcm_celery_queue_depth -1" in body  # eager in tests: no broker


class TestBackupManifest:
    def test_table_counts_cover_the_key_tables(self, org):
        from apps.core.backup import KEY_TABLES, table_counts

        counts = table_counts()
        for table in KEY_TABLES:
            assert table in counts, table
        assert counts["cost_codes"] == 2 and counts["estates"] == 2

    def test_mysql_binary_lookup_explains_itself(self, monkeypatch):
        from apps.core import backup

        monkeypatch.setenv("MYSQL_BIN_DIR", "C:/definitely/not/here")
        monkeypatch.setattr(backup.shutil, "which", lambda name: None)
        monkeypatch.setattr(backup, "DEFAULT_WINDOWS_BIN", backup.Path("C:/nor/here"))
        with pytest.raises(FileNotFoundError, match="MYSQL_BIN_DIR"):
            backup.mysql_binary("mysqldump")


class TestSecurityHeaders:
    def test_unauthenticated_download_link_is_refused_and_download_needs_a_valid_token(
        self, api_client
    ):
        assert api_client.get(reverse("documents:document-link", args=[1])).status_code == 401
        assert (
            api_client.get(
                reverse("documents:document-download", args=[1]), {"token": "forged"}
            ).status_code
            == 404
        )

    def test_login_is_throttled(self, api_client, settings):
        client = APIClient()
        statuses = [
            client.post(
                reverse("accounts:auth:login"),
                {"email": "nobody@example.com", "password": "wrong"},
                format="json",
            ).status_code
            for _ in range(12)
        ]
        assert 429 in statuses
