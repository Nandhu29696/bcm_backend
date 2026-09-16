"""Risk register, actions and recovery strategy routes, nested under a plan version."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.risk import views

app_name = "risk"

# SimpleRouter, not DefaultRouter: the nested actions router is mounted at
# `risks/<risk_id>/`, and a DefaultRouter's API-root view at that path would
# capture the risk detail URL and answer 405 to PATCH and DELETE.
router = SimpleRouter()
router.register("risks", views.RiskViewSet, basename="risk")
router.register("recovery-strategies", views.RecoveryStrategyViewSet, basename="recovery-strategy")

actions = SimpleRouter()
actions.register("actions", views.RiskActionViewSet, basename="risk-action")

urlpatterns = [
    path(
        "plan-versions/<int:plan_version_id>/risk-options/",
        views.RiskOptionsView.as_view(),
        name="risk-options",
    ),
    path("plan-versions/<int:plan_version_id>/risks/<int:risk_id>/", include(actions.urls)),
    path("plan-versions/<int:plan_version_id>/", include(router.urls)),
]
