"""
Root URL configuration.

Everything under /api/v1/ (AD-10). Version lives in the URL; a breaking change
gets /api/v2/ rather than mutating v1 in place.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

API = "api/v1/"

urlpatterns = [
    path("admin/", admin.site.urls),
    path(API, include("apps.core.urls")),
    path(API, include("apps.accounts.urls")),
    path(API, include("apps.organization.urls")),
    path(API, include("apps.plans.urls")),
    path(API, include("apps.questionnaire.urls")),
    path(API, include("apps.assessments.urls")),
    path(API, include("apps.risk.urls")),
    path(API, include("apps.documents.urls")),
    path(API, include("apps.exemptions.urls")),
    path(API, include("apps.crisis.urls")),
    path(API, include("apps.calltree.urls")),
    path(API, include("apps.testing.urls")),
    path(API, include("apps.helpcenter.urls")),
    path(API, include("apps.reporting.urls")),
    path(API, include("apps.notifications.urls")),
    # Domain routes are added by their phase:
]

if settings.ENABLE_API_DOCS:
    urlpatterns += [
        path(f"{API}schema/", SpectacularAPIView.as_view(), name="schema"),
        path(
            f"{API}docs/",
            SpectacularSwaggerView.as_view(url_name="schema"),
            name="swagger-ui",
        ),
        path(
            f"{API}redoc/",
            SpectacularRedocView.as_view(url_name="schema"),
            name="redoc",
        ),
    ]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
