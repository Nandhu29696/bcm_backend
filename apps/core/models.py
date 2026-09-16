"""Shared base models and managers."""

from django.conf import settings
from django.db import models


class ActiveQuerySet(models.QuerySet):
    """Queryset helpers for the `active_flag` soft-delete convention (AD-6)."""

    def active(self):
        return self.filter(active_flag=True)

    def inactive(self):
        return self.filter(active_flag=False)


class ActiveManager(models.Manager.from_queryset(ActiveQuerySet)):
    """Default manager that hides soft-deleted rows."""

    def get_queryset(self):
        return super().get_queryset().filter(active_flag=True)


class TimeStampedModel(models.Model):
    """created_at / updated_at maintained by Django, not by the database.

    The original MySQL schema used `DEFAULT CURRENT_TIMESTAMP ON UPDATE
    CURRENT_TIMESTAMP`. Django is now the only writer, so the column defaults are
    intentionally dropped in favour of auto_now_add / auto_now.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    """Soft delete via `active_flag` (AD-6).

    `objects` hides inactive rows; `all_objects` sees everything. `base_manager_name`
    points at the unfiltered manager so Django's own related-object fetching,
    cascade handling and serialisation are not silently starved of rows — using a
    filtered manager as the base manager causes very hard-to-diagnose bugs.
    """

    active_flag = models.BooleanField(default=True, db_index=True)

    objects = ActiveManager()
    all_objects = models.Manager.from_queryset(ActiveQuerySet)()

    class Meta:
        abstract = True
        base_manager_name = "all_objects"

    def soft_delete(self, *, using=None):
        self.active_flag = False
        self.save(update_fields=["active_flag"], using=using)

    def restore(self, *, using=None):
        self.active_flag = True
        self.save(update_fields=["active_flag"], using=using)


class BaseModel(TimeStampedModel, SoftDeleteModel):
    """Timestamps + soft delete. The default base for new domain models."""

    class Meta:
        abstract = True
        base_manager_name = "all_objects"


class AuditLog(models.Model):
    """Append-only record of security- and workflow-significant events.

    Written by the auth flows (Phase 1) and every plan status transition
    (Phase 6). Never updated or deleted.
    """

    class Action(models.TextChoices):
        LOGIN_SUCCESS = "LOGIN_SUCCESS", "Login success"
        LOGIN_FAILURE = "LOGIN_FAILURE", "Login failure"
        LOGIN_LOCKOUT = "LOGIN_LOCKOUT", "Login lockout"
        LOGOUT = "LOGOUT", "Logout"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset"
        ROLE_CHANGE = "ROLE_CHANGE", "Role change"
        STATUS_TRANSITION = "STATUS_TRANSITION", "Status transition"
        RECORD_CREATED = "RECORD_CREATED", "Record created"
        RECORD_UPDATED = "RECORD_UPDATED", "Record updated"
        RECORD_DELETED = "RECORD_DELETED", "Record deleted"

    audit_log_id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=40, choices=Action.choices)
    entity_type = models.CharField(max_length=80, blank=True)
    entity_id = models.BigIntegerField(null=True, blank=True)
    detail = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["actor", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.entity_type}:{self.entity_id}"
