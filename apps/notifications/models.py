"""
Notification dispatch and delivery log (AD-8).

Journey steps 6 and 7 are email-driven, so email is infrastructure here rather than
a detail. Every send is recorded: what was sent, to whom, for which entity, and
whether it succeeded.

`idempotency_key` is what stops a Celery retry from mailing a BU lead twice. It is
derived from (event_type, entity_id, recipient) and enforced unique — a retry that
re-enters dispatch hits the constraint and short-circuits instead of re-sending.
"""

from django.conf import settings
from django.db import models


class NotificationEvent(models.TextChoices):
    PLAN_SUBMITTED = "PLAN_SUBMITTED", "Plan submitted for review"
    PLAN_APPROVED = "PLAN_APPROVED", "Plan approved"
    PLAN_REWORK = "PLAN_REWORK", "Plan sent back for rework"
    COORDINATOR_ASSIGNED = "COORDINATOR_ASSIGNED", "Coordinator assigned"
    EXEMPTION_SUBMITTED = "EXEMPTION_SUBMITTED", "Exemption submitted"
    EXEMPTION_DECIDED = "EXEMPTION_DECIDED", "Exemption approved or rejected"
    REVIEW_REMINDER = "REVIEW_REMINDER", "Review pending reminder"
    RISK_ACTION_OVERDUE = "RISK_ACTION_OVERDUE", "Risk action overdue"
    TEST_REMINDER = "TEST_REMINDER", "Upcoming test reminder"
    CRISIS_INITIATED = "CRISIS_INITIATED", "Crisis event initiated"
    CALL_TREE_NOTIFY = "CALL_TREE_NOTIFY", "Call tree email notification"
    CALL_TREE_ESCALATION = "CALL_TREE_ESCALATION", "Call tree escalation to BU lead"
    CALL_TREE_SUMMARY = "CALL_TREE_SUMMARY", "Call tree run summary"
    REPORT_READY = "REPORT_READY", "Report ready"
    OTP_CODE = "OTP_CODE", "One-time passcode"
    PASSWORD_RESET = "PASSWORD_RESET", "Password reset"


class DeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    SUPPRESSED = "SUPPRESSED", "Suppressed"


class NotificationLog(models.Model):
    notification_log_id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=50, choices=NotificationEvent.choices)

    entity_type = models.CharField(max_length=80, blank=True)
    entity_id = models.BigIntegerField(null=True, blank=True)

    # Stored as sent, not as a FK: a delivery log must remain accurate even if the
    # recipient's account is later renamed or removed.
    to_email = models.EmailField(max_length=320)
    cc_emails = models.TextField(blank=True, help_text="Comma-separated.")
    recipient_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="recipient_user_id",
        related_name="notifications_received",
    )

    subject = models.CharField(max_length=500, blank=True)
    template_name = models.CharField(max_length=150, blank=True)
    context = models.JSONField(null=True, blank=True)

    status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    error_detail = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    idempotency_key = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id"], name="idx_notif_entity"),
            models.Index(fields=["status", "created_at"], name="idx_notif_status"),
        ]

    def __str__(self):
        return f"{self.event_type} -> {self.to_email} [{self.status}]"


class UserNotification(models.Model):
    """An in-app notification for one user (the bell).

    Written alongside the email whenever the recipient is a known account, so
    the two never disagree. `link` is a frontend path; `category` is the
    notification event type so the UI can pick an icon.
    """

    user_notification_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="notifications",
    )
    category = models.CharField(max_length=50, choices=NotificationEvent.choices)
    title = models.CharField(max_length=300)
    body = models.CharField(max_length=1000, blank=True)
    link = models.CharField(max_length=500, blank=True)
    source_log = models.ForeignKey(
        NotificationLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="notification_log_id",
        related_name="user_notifications",
    )
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "read_at", "created_at"], name="idx_usernotif_user_read")
        ]

    def __str__(self):
        return f"{self.user_id}: {self.title}"


class PushSubscription(models.Model):
    """A browser's Web Push subscription (service worker + VAPID)."""

    push_subscription_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="push_subscriptions",
    )
    # Endpoints run to ~600 characters; MySQL cannot index that in utf8mb4, so
    # uniqueness is on the SHA-256 of the endpoint.
    endpoint = models.TextField()
    endpoint_hash = models.CharField(max_length=64, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "push_subscriptions"

    def __str__(self):
        return f"{self.user_id} @ {self.endpoint[:40]}"
