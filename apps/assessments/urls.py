"""Structured BIA section routes, nested under a plan version."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.assessments import views

app_name = "assessments"

router = SimpleRouter()
router.register(
    "service-descriptions", views.ServiceDescriptionViewSet, basename="service-description"
)
router.register("critical-contacts", views.CriticalContactViewSet, basename="critical-contact")
router.register(
    "network-requirements", views.NetworkRequirementViewSet, basename="network-requirement"
)

urlpatterns = [
    path("plan-versions/<int:plan_version_id>/", include(router.urls)),
]
