"""Estate and cost code routes (journey steps 2-3)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.organization import views

app_name = "organization"

router = DefaultRouter()
router.register("estates", views.EstateViewSet, basename="estate")

# Nested under the estate because a cost code list only has meaning within one.
# Registered explicitly rather than as router @actions so each view gets its own
# filterset and ordering fields instead of inheriting the estate viewset's.
estate_patterns = [
    path(
        "estates/<int:estate_id>/cost-codes/",
        views.CostCodeListView.as_view(),
        name="estate-cost-codes",
    ),
    path(
        "estates/<int:estate_id>/cost-code-filters/",
        views.CostCodeFilterOptionsView.as_view(),
        name="estate-cost-code-filters",
    ),
]

urlpatterns = estate_patterns + [
    path("master-data/", views.MasterDataView.as_view(), name="master-data"),
    path("", include(router.urls)),
]
