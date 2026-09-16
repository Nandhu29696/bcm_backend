"""
Data scoping (AD-3).

The Phase 1 exit criterion is specific: a role-gated endpoint must return 403 on
the object AND filter the list — proven by two separate tests. Those are
`test_object_access_is_denied_outside_scope` and
`test_list_is_filtered_to_scope` at the bottom of this module.

They are separate because they fail independently. A view can implement
`has_object_permission` correctly and still leak every row through its list
route, which is the more dangerous of the two and the easier to miss.
"""

import pytest
from django.urls import reverse
from rest_framework import serializers
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import UserEstateScope
from apps.accounts.scoping import ScopedQuerySetMixin, ScopeResolver
from apps.organization.models import CostCode, Estate, Process, Subprocess

pytestmark = pytest.mark.django_db


# Minimal serializers so the mixin can be exercised before the Phase 2 viewsets
# exist. Defined here rather than in the organization app — test scaffolding does
# not belong in production code.
class EstateStubSerializer(serializers.ModelSerializer):
    class Meta:
        model = Estate
        fields = ["estate_id", "estate_name"]


class CostCodeStubSerializer(serializers.ModelSerializer):
    class Meta:
        model = CostCode
        fields = ["cost_code_id", "cost_code", "estate"]


@pytest.fixture
def estates():
    return [
        Estate.objects.create(estate_name="Alpha Estate"),
        Estate.objects.create(estate_name="Beta Estate"),
        Estate.objects.create(estate_name="Gamma Estate"),
    ]


@pytest.fixture
def cost_codes(estates):
    process = Process.objects.create(process_name="Support")
    subprocess = Subprocess.objects.create(subprocess_name="Tier 1", process=process)
    return [
        CostCode.objects.create(
            cost_code=f"CC-{i:03d}",
            estate=estate,
            process=process,
            subprocess=subprocess,
        )
        for i, estate in enumerate(estates, start=1)
    ]


def grant(user, estate):
    return UserEstateScope.objects.create(user=user, estate=estate)


# --------------------------------------------------------------------------- #
# ScopeResolver
# --------------------------------------------------------------------------- #


def test_admin_sees_all_estates(user_factory):
    user = user_factory(email="admin@example.com", roles=["BCM_ADMIN"])
    assert ScopeResolver(user).sees_all_estates is True


def test_auditor_sees_all_estates(user_factory):
    user = user_factory(email="auditor@example.com", roles=["BCM_AUDITOR"])
    assert ScopeResolver(user).sees_all_estates is True


def test_coordinator_does_not_see_all_estates(user_factory):
    user = user_factory(email="coord@example.com", roles=["BCM_COORDINATOR"])
    assert ScopeResolver(user).sees_all_estates is False


def test_scope_reflects_granted_estates(user_factory, estates):
    user = user_factory(email="scoped@example.com", roles=["BCM_COORDINATOR"])
    grant(user, estates[0])
    grant(user, estates[2])

    assert ScopeResolver(user).estate_ids == frozenset({estates[0].pk, estates[2].pk})


def test_inactive_scope_row_is_ignored(user_factory, estates):
    user = user_factory(email="inactive@example.com", roles=["BCM_COORDINATOR"])
    UserEstateScope.objects.create(user=user, estate=estates[0], active_flag=False)
    assert ScopeResolver(user).estate_ids == frozenset()


def test_user_with_no_roles_resolves_to_empty_scope(user_factory):
    """The landing state for an SSO sign-in with no HR record.

    Must be an empty scope, never an exception — the account works, it just has
    no data, which surfaces the provisioning gap instead of hiding it.
    """
    user = user_factory(email="pending@example.com")
    scope = ScopeResolver(user)

    assert scope.sees_all_estates is False
    assert scope.estate_ids == frozenset()
    assert scope.role_codes == set()


# --------------------------------------------------------------------------- #
# ScopedQuerySetMixin
# --------------------------------------------------------------------------- #


class EstateListView(ScopedQuerySetMixin, ListAPIView):
    queryset = Estate.objects.all()
    serializer_class = EstateStubSerializer
    permission_classes = [IsAuthenticated]
    estate_scope_path = ""  # this model IS Estate


class CostCodeListView(ScopedQuerySetMixin, ListAPIView):
    queryset = CostCode.objects.all()
    serializer_class = CostCodeStubSerializer
    permission_classes = [IsAuthenticated]
    estate_scope_path = "estate_id"


def call(view_class, user):
    request = APIRequestFactory().get("/")
    force_authenticate(request, user=user)
    return view_class.as_view()(request)


def rows(response) -> list[dict]:
    """Unwrap a list response. The default pagination wraps rows in `results`."""
    data = response.data
    return list(data["results"] if isinstance(data, dict) and "results" in data else data)


def test_estate_list_is_scoped(user_factory, estates):
    user = user_factory(email="two@example.com", roles=["BCM_COORDINATOR"])
    grant(user, estates[0])
    grant(user, estates[1])

    names = {row["estate_name"] for row in rows(call(EstateListView, user))}
    assert names == {"Alpha Estate", "Beta Estate"}


def test_admin_estate_list_is_unscoped(user_factory, estates):
    user = user_factory(email="admin2@example.com", roles=["BCM_ADMIN"])
    assert len(rows(call(EstateListView, user))) == 3


def test_unscoped_user_gets_an_empty_list_not_an_error(user_factory, estates):
    user = user_factory(email="nothing@example.com")
    response = call(EstateListView, user)

    assert response.status_code == 200
    assert rows(response) == []


def test_related_model_is_scoped_through_its_path(user_factory, estates, cost_codes):
    user = user_factory(email="cc@example.com", roles=["BCM_COORDINATOR"])
    grant(user, estates[1])

    codes = {row["cost_code"] for row in rows(call(CostCodeListView, user))}
    assert codes == {"CC-002"}


def test_opting_out_of_scoping_returns_everything(user_factory, estates):
    class UnscopedView(EstateListView):
        estate_scope_path = None

    user = user_factory(email="opt@example.com", roles=["BCM_COORDINATOR"])
    assert len(rows(call(UnscopedView, user))) == 3


# --------------------------------------------------------------------------- #
# The two exit-criterion tests
# --------------------------------------------------------------------------- #


def test_list_is_filtered_to_scope(user_factory, estates, cost_codes):
    """Exit criterion, part one: the LIST route must not leak out-of-scope rows.

    This is the failure mode object permissions cannot catch, because list routes
    never call `has_object_permission`.
    """
    user = user_factory(email="listscope@example.com", roles=["BCM_COORDINATOR"])
    grant(user, estates[0])

    response = call(CostCodeListView, user)
    returned = {row["cost_code"] for row in rows(response)}

    assert returned == {"CC-001"}
    assert "CC-002" not in returned
    assert "CC-003" not in returned


def test_object_access_is_denied_outside_scope(user_factory, estates, cost_codes):
    """Exit criterion, part two: the DETAIL route must 404 outside scope.

    404 rather than 403 on purpose — a 403 confirms the row exists, which leaks
    the very thing scoping is meant to hide.
    """
    from rest_framework.generics import RetrieveAPIView

    class CostCodeDetailView(ScopedQuerySetMixin, RetrieveAPIView):
        queryset = CostCode.objects.all()
        serializer_class = CostCodeStubSerializer
        permission_classes = [IsAuthenticated]
        estate_scope_path = "estate_id"

    user = user_factory(email="objscope@example.com", roles=["BCM_COORDINATOR"])
    grant(user, estates[0])

    in_scope, out_of_scope = cost_codes[0], cost_codes[1]
    factory = APIRequestFactory()

    request = factory.get("/")
    force_authenticate(request, user=user)
    allowed = CostCodeDetailView.as_view()(request, pk=in_scope.pk)
    assert allowed.status_code == 200

    request = factory.get("/")
    force_authenticate(request, user=user)
    denied = CostCodeDetailView.as_view()(request, pk=out_of_scope.pk)
    assert denied.status_code == 404


def test_role_gated_endpoint_rejects_wrong_role(api_client, user_factory):
    """Role check, as distinct from scope: the admin API refuses a coordinator."""
    coordinator = user_factory(email="notadmin@example.com", roles=["BCM_COORDINATOR"])
    api_client.force_authenticate(user=coordinator)

    response = api_client.get(reverse("accounts:admin-user-list"))
    assert response.status_code == 403


def test_role_gated_endpoint_allows_admin(api_client, user_factory):
    admin = user_factory(email="isadmin@example.com", roles=["BCM_ADMIN"])
    api_client.force_authenticate(user=admin)

    response = api_client.get(reverse("accounts:admin-user-list"))
    assert response.status_code == 200
