"""
CRUD for the structured sections of a plan version (Phase 5).

Service descriptions, critical contacts, network requirements, risks, risk
actions and recovery strategies are all "rows that belong to a version". They
share three rules, and the base class here is where those rules live once:

  1. Scope: the version is resolved through the caller's estate scope; an
     unknown or out-of-scope version is a 404 (AD-3).
  2. Authoring: reads need scope; writes need an authoring claim on the
     version — admin, or an actively assigned coordinator.
  3. Immutability: an Approved or Exempted version refuses writes with 409
     (AD-4). Copy it to change it.

Lists are not paginated. They are per-plan and short; paginating a five-row
contact list would only complicate the editor.
"""

from __future__ import annotations

from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated

from apps.accounts.permissions import IsActiveUser
from apps.plans.access import PlanVersionScopedMixin


class NotAnAuthor(APIException):
    status_code = http_status.HTTP_403_FORBIDDEN
    default_detail = "Only an assigned coordinator or an administrator can change this plan."
    default_code = "not_an_author"


class VersionNotEditable(APIException):
    status_code = http_status.HTTP_409_CONFLICT
    default_code = "plan_not_editable"

    def __init__(self, version):
        super().__init__(
            f"Version {version.version_number} is {version.status} and cannot be changed. "
            "Create a new version to make changes."
        )


class PlanVersionChildViewSet(PlanVersionScopedMixin, viewsets.ModelViewSet):
    """Subclass with `model`, `serializer_class` and optionally `select_related`."""

    model = None
    select_related: tuple[str, ...] = ()
    permission_classes = [IsAuthenticated, IsActiveUser]
    pagination_class = None
    #: Explicit, so this view's own queryset is never scoped by the inherited
    #: PlanVersion path (which does not exist on child models). Scope is applied
    #: when the parent version is resolved.
    estate_scope_path = None

    def get_queryset(self):
        version = self.get_plan_version()
        queryset = self.model.objects.filter(plan_version=version)
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        return queryset.order_by("pk")

    def initial(self, request, *args, **kwargs):
        """Gate writes BEFORE the body is validated.

        DRF validates the payload first and only then reaches `perform_create`.
        Checking authorship there would answer a viewer's empty POST with a 400
        that spells out the valid payload, and a write to an approved version
        with a validation error instead of the 409 that explains the real
        problem. Authorisation is decided first, on the request alone.
        """
        super().initial(request, *args, **kwargs)
        if request.method not in SAFE_METHODS:
            self.assert_writable()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # So a serializer can default fields from the plan (its process, cost
        # code) while validating, not only when saving.
        context["plan_version"] = self.get_plan_version()
        return context

    def assert_writable(self):
        version = self.get_plan_version()
        if not self.caller_may_author(version):
            raise NotAnAuthor()
        if not version.is_editable:
            raise VersionNotEditable(version)
        return version

    def perform_write_bookkeeping(self):
        """The first edit moves Not Started / Rework to Work in Progress."""
        from apps.plans.workflow import mark_in_progress

        version = mark_in_progress(self.get_plan_version(), actor=self.request.user)
        self._plan_version = version
        return version

    def perform_create(self, serializer):
        self.assert_writable()
        version = self.perform_write_bookkeeping()
        serializer.save(plan_version=version, **self.create_defaults())

    def perform_update(self, serializer):
        self.assert_writable()
        self.perform_write_bookkeeping()
        serializer.save()

    def perform_destroy(self, instance):
        self.assert_writable()
        self.perform_write_bookkeeping()
        instance.delete()

    def create_defaults(self) -> dict:
        """Extra fields set on create — an audit stamp, say."""
        return {}
