"""Infrastructure routes."""

from django.urls import path

from apps.core.metrics import MetricsView
from apps.core.views import HealthView, ReadinessView

app_name = "core"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("ready/", ReadinessView.as_view(), name="ready"),
    path("metrics/", MetricsView.as_view(), name="metrics"),
]
