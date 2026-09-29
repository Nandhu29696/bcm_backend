"""
The bell and browser push.

    GET  /notifications/?unread=1          the caller's notifications, newest first
    GET  /notifications/unread-count/
    POST /notifications/{id}/read/
    POST /notifications/read-all/
    GET  /notifications/push/public-key/   VAPID public key, or configured=false
    POST /notifications/push/subscribe/    {endpoint, keys: {p256dh, auth}}
    POST /notifications/push/unsubscribe/  {endpoint}
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsActiveUser, IsAdmin
from apps.accounts.services import record_audit
from apps.core.models import AuditLog
from apps.core.tasks import enqueue_after_commit
from apps.notifications import inapp
from apps.notifications.inapp import SILENT_EVENTS
from apps.notifications.models import (
    DeliveryStatus,
    NotificationLog,
    PushSubscription,
    UserNotification,
)
from apps.notifications.tasks import deliver_notification


class UserNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserNotification
        fields = [
            "user_notification_id",
            "category",
            "title",
            "body",
            "link",
            "read_at",
            "created_at",
        ]


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(responses=UserNotificationSerializer(many=True))
    def get(self, request, *args, **kwargs):
        rows = UserNotification.objects.filter(user=request.user)
        if request.query_params.get("unread") in ("1", "true"):
            rows = rows.filter(read_at__isnull=True)
        rows = rows[:50]
        return Response(
            {
                "results": UserNotificationSerializer(rows, many=True).data,
                "unread": UserNotification.objects.filter(
                    user=request.user, read_at__isnull=True
                ).count(),
            }
        )


class UnreadCountView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        return Response(
            {
                "unread": UserNotification.objects.filter(
                    user=request.user, read_at__isnull=True
                ).count()
            }
        )


class MarkReadView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def post(self, request, *args, **kwargs):
        row = get_object_or_404(
            UserNotification, pk=self.kwargs["user_notification_id"], user=request.user
        )
        if row.read_at is None:
            row.read_at = timezone.now()
            row.save(update_fields=["read_at"])
        return Response(UserNotificationSerializer(row).data)


class MarkAllReadView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def post(self, request, *args, **kwargs):
        updated = UserNotification.objects.filter(user=request.user, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        return Response({"marked": updated})


class PushPublicKeyView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        return Response(
            {"configured": inapp.push_configured(), "public_key": settings.VAPID_PUBLIC_KEY}
        )


class PushSubscriptionSerializer(serializers.Serializer):
    endpoint = serializers.URLField(max_length=2000)
    keys = serializers.DictField(child=serializers.CharField(max_length=255))

    def validate_keys(self, keys):
        if not keys.get("p256dh") or not keys.get("auth"):
            raise serializers.ValidationError("keys.p256dh and keys.auth are required.")
        return keys


class PushSubscribeView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    @extend_schema(request=PushSubscriptionSerializer)
    def post(self, request, *args, **kwargs):
        if not inapp.push_configured():
            return Response(
                {
                    "detail": "Browser push is not configured on this server.",
                    "code": "push_not_configured",
                    "field_errors": {},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        body = PushSubscriptionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        inapp.subscribe(
            request.user,
            endpoint=body.validated_data["endpoint"],
            p256dh=body.validated_data["keys"]["p256dh"],
            auth=body.validated_data["keys"]["auth"],
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        return Response(
            {
                "subscribed": True,
                "devices": PushSubscription.objects.filter(user=request.user).count(),
            },
            status=201,
        )


class PushUnsubscribeView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def post(self, request, *args, **kwargs):
        endpoint = request.data.get("endpoint", "")
        removed = inapp.unsubscribe(request.user, endpoint=endpoint) if endpoint else 0
        return Response(
            {
                "removed": removed,
                "devices": PushSubscription.objects.filter(user=request.user).count(),
            }
        )


# --------------------------------------------------------------------------- #
# Administration: the delivery log and re-sending (PENDING #28)
# --------------------------------------------------------------------------- #


class NotificationLogSerializer(serializers.ModelSerializer):
    resendable = serializers.SerializerMethodField()

    class Meta:
        model = NotificationLog
        fields = [
            "notification_log_id",
            "event_type",
            "to_email",
            "cc_emails",
            "subject",
            "status",
            "attempts",
            "error_detail",
            "entity_type",
            "entity_id",
            "sent_at",
            "created_at",
            "resendable",
        ]

    def get_resendable(self, log) -> bool:
        # Credentials were redacted before the row was stored; nothing to resend.
        return log.status != DeliveryStatus.SENT and log.event_type not in SILENT_EVENTS


def _require_admin(request, view) -> None:
    if not IsAdmin().has_permission(request, view):
        raise PermissionDenied("Administrators only.")


# `entity_type` is a free-text label written at send time (see apps/*/services.py
# call sites), and both an UPPER_CASE and a PascalCase spelling exist in the data
# for the same kind of entity -- not something to silently normalise here. Each
# entry maps every spelling actually in use to the ORM path from that model down
# to a CostCode, so an Estate/Cost-code filter can resolve which entity_id values
# on THIS entity_type belong to the requested scope.
_ENTITY_COST_CODE_PATHS: dict[str, str | None] = {
    "PlanVersion": "plan__cost_code",
    "PLAN_VERSION": "plan__cost_code",
    "CoordinatorAssignment": "plan_version__plan__cost_code",
    "Exemption": "plan_version__plan__cost_code",
    "EXEMPTION": "plan_version__plan__cost_code",
    "CrisisEvent": "cost_code",
    "CRISIS_EVENT": "cost_code",
    "CallTreeRun": "cost_code",
    "CALL_TREE_RUN": "cost_code",
    "Test": "plan_version__plan__cost_code",
    "CostCode": None,  # the entity IS the cost code; see below.
}


def _entity_model(entity_type: str):
    from apps.calltree.models import CallTreeRun
    from apps.crisis.models import CrisisEvent
    from apps.exemptions.models import Exemption
    from apps.organization.models import CostCode
    from apps.plans.models import CoordinatorAssignment, PlanVersion
    from apps.testing.models import Test

    return {
        "PlanVersion": PlanVersion,
        "PLAN_VERSION": PlanVersion,
        "CoordinatorAssignment": CoordinatorAssignment,
        "Exemption": Exemption,
        "EXEMPTION": Exemption,
        "CrisisEvent": CrisisEvent,
        "CRISIS_EVENT": CrisisEvent,
        "CallTreeRun": CallTreeRun,
        "CALL_TREE_RUN": CallTreeRun,
        "Test": Test,
        "CostCode": CostCode,
    }.get(entity_type)


def _scope_to_notification_filter(estate_id: str, cost_code: str) -> Q | None:
    """A Q matching only rows whose entity resolves into the requested scope.

    `entity_type`/`entity_id` is a generic reference (AD-8), so this is a
    fan-out: for every entity kind that has a knowable path to a cost code,
    resolve which ids on THAT kind match, then OR the per-type results
    together. Entity kinds with no cost-code/estate concept (OTP challenges,
    user accounts, report requests, ...) never match and are correctly
    excluded rather than silently ignored.
    """
    if not estate_id and not cost_code:
        return None

    combined = Q(pk__in=[])  # matches nothing until a branch below adds to it
    for entity_type, path in _ENTITY_COST_CODE_PATHS.items():
        model = _entity_model(entity_type)
        if model is None:
            continue
        if path is None:
            # CostCode is the entity itself.
            filters = Q()
            if estate_id:
                filters &= Q(estate_id=estate_id)
            if cost_code:
                filters &= Q(cost_code__icontains=cost_code)
            ids = set(model.objects.filter(filters).values_list("pk", flat=True))
        else:
            filters = Q()
            if estate_id:
                filters &= Q(**{f"{path}__estate_id": estate_id})
            if cost_code:
                filters &= Q(**{f"{path}__cost_code__icontains": cost_code})
            ids = set(model.objects.filter(filters).values_list("pk", flat=True))
        if ids:
            combined |= Q(entity_type=entity_type, entity_id__in=ids)
    return combined


class NotificationLogAdminView(APIView):
    """GET /admin/notifications/?status=FAILED&search=&estate=&cost_code= - the delivery log."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    def get(self, request, *args, **kwargs):
        _require_admin(request, self)
        rows = NotificationLog.objects.order_by("-created_at")
        status_filter = request.query_params.get("status", "")
        if status_filter:
            rows = rows.filter(status=status_filter)
        search = (request.query_params.get("search") or "").strip()
        if search:
            rows = rows.filter(Q(to_email__icontains=search) | Q(subject__icontains=search))
        estate_id = request.query_params.get("estate", "")
        cost_code = (request.query_params.get("cost_code") or "").strip()
        scope_filter = _scope_to_notification_filter(estate_id, cost_code)
        if scope_filter is not None:
            rows = rows.filter(scope_filter)
        counts = dict.fromkeys(DeliveryStatus.values, 0)
        for row in NotificationLog.objects.values("status").annotate(n=Count("pk")):
            counts[row["status"]] = row["n"]
        return Response(
            {"results": NotificationLogSerializer(rows[:200], many=True).data, "counts": counts}
        )


class NotificationResendView(APIView):
    """POST /admin/notifications/{id}/resend/ - deliver a failed notification again."""

    permission_classes = [IsAuthenticated, IsActiveUser]

    def post(self, request, *args, **kwargs):
        _require_admin(request, self)
        log = get_object_or_404(NotificationLog, pk=self.kwargs["notification_log_id"])
        if log.status == DeliveryStatus.SENT:
            return Response(
                {"detail": "Already sent.", "code": "already_sent", "field_errors": {}},
                status=http_status.HTTP_409_CONFLICT,
            )
        if log.event_type in SILENT_EVENTS:
            return Response(
                {
                    "detail": "One-time codes and reset links cannot be re-sent; "
                    "the user must request a new one.",
                    "code": "not_resendable",
                    "field_errors": {},
                },
                status=http_status.HTTP_400_BAD_REQUEST,
            )
        log.status = DeliveryStatus.PENDING
        log.error_detail = ""
        log.attempts = 0
        log.save(update_fields=["status", "error_detail", "attempts"])
        record_audit(
            action=AuditLog.Action.RECORD_UPDATED,
            actor=request.user,
            entity_type="NotificationLog",
            entity_id=log.pk,
            detail={"resend": True, "event_type": log.event_type, "to": log.to_email},
            request=request,
        )
        context = dict(log.context or {})
        enqueue_after_commit(lambda: deliver_notification.delay(log.pk, context))
        log.refresh_from_db()
        return Response(NotificationLogSerializer(log).data)
