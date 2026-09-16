"""In-app notification and browser push routes."""

from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("notifications/", views.NotificationListView.as_view(), name="list"),
    path("notifications/unread-count/", views.UnreadCountView.as_view(), name="unread-count"),
    path("notifications/read-all/", views.MarkAllReadView.as_view(), name="read-all"),
    path(
        "notifications/<int:user_notification_id>/read/", views.MarkReadView.as_view(), name="read"
    ),
    path(
        "notifications/push/public-key/", views.PushPublicKeyView.as_view(), name="push-public-key"
    ),
    path("notifications/push/subscribe/", views.PushSubscribeView.as_view(), name="push-subscribe"),
    path(
        "notifications/push/unsubscribe/",
        views.PushUnsubscribeView.as_view(),
        name="push-unsubscribe",
    ),
    path("admin/notifications/", views.NotificationLogAdminView.as_view(), name="admin-log"),
    path(
        "admin/notifications/<int:notification_log_id>/resend/",
        views.NotificationResendView.as_view(),
        name="admin-resend",
    ),
]
