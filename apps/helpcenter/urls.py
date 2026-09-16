"""Help library routes (Phase 9.1)."""

from django.urls import path

from apps.helpcenter import views

app_name = "helpcenter"

urlpatterns = [
    path("help/", views.HelpListView.as_view(), name="help-list"),
    path("help/categories/", views.HelpCategoriesView.as_view(), name="help-categories"),
    path("help/<int:help_resource_id>/", views.HelpDetailView.as_view(), name="help-detail"),
]
