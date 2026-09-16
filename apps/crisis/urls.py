"""Crisis management routes (Phase 8)."""

from django.urls import path

from apps.crisis import views

app_name = "crisis"

urlpatterns = [
    path("cost-codes/<int:cost_code_id>/cmsc-members/", views.RosterView.as_view(), name="roster"),
    path(
        "cost-codes/<int:cost_code_id>/cmsc-members/upload/",
        views.RosterUploadView.as_view(),
        name="roster-upload",
    ),
    path(
        "cmsc-members/<int:cmsc_member_id>/",
        views.CmscMemberDetailView.as_view(),
        name="member-detail",
    ),
    path("crisis-events/", views.EventListView.as_view(), name="event-list"),
    path(
        "cost-codes/<int:cost_code_id>/crisis-events/",
        views.CostCodeEventsView.as_view(),
        name="cost-code-events",
    ),
    path(
        "crisis-events/<int:crisis_event_id>/", views.EventDetailView.as_view(), name="event-detail"
    ),
    path(
        "crisis-events/<int:crisis_event_id>/initiate/",
        views.EventActionView.as_view(action_name="initiate"),
        name="event-initiate",
    ),
    path(
        "crisis-events/<int:crisis_event_id>/close/",
        views.EventActionView.as_view(action_name="close"),
        name="event-close",
    ),
    path(
        "crisis-events/<int:crisis_event_id>/cancel/",
        views.EventActionView.as_view(action_name="cancel"),
        name="event-cancel",
    ),
]
