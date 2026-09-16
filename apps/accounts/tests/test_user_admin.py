"""User administration and the per-user second factor."""

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Employee, UserAccount, UserEstateScope, UserRole
from apps.core.models import AuditLog
from apps.organization.models import Estate

pytestmark = pytest.mark.django_db

PASSWORD = "test-pass-123"


@pytest.fixture
def admin_client(user_factory):
    client = APIClient()
    client.force_authenticate(user=user_factory(email="root@example.com", roles=["BCM_ADMIN"]))
    return client


@pytest.fixture(autouse=True)
def _roles(db):
    """The role catalogue, as seed_reference_data provides it."""
    from apps.accounts.models import Role
    from apps.accounts.roles import RoleCode

    for code, label in RoleCode.choices:
        Role.objects.get_or_create(role_code=code, defaults={"role_name": label})


@pytest.fixture
def alice(user_factory):
    return user_factory(email="alice@example.com", display_name="Alice", roles=["BCM_VIEWER"])


def login(email):
    return APIClient().post(
        reverse("accounts:auth:login"), {"email": email, "password": PASSWORD}, format="json"
    )


class TestMfaSwitch:
    def test_mfa_on_by_default_asks_for_a_code(self, alice, settings):
        settings.REQUIRE_OTP_FOR_LOGIN = True
        response = login(alice.email)
        assert response.status_code == 200 and response.data["otp_required"] is True
        assert "access" not in response.data
        assert len(mail.outbox) == 1

    def test_admin_switches_mfa_off_and_the_user_signs_in_directly(
        self, admin_client, alice, settings
    ):
        settings.REQUIRE_OTP_FOR_LOGIN = True
        response = admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"mfa_enabled": False},
            format="json",
        )
        assert response.status_code == 200 and response.data["mfa_enabled"] is False

        signed_in = login(alice.email)
        assert signed_in.status_code == 200 and signed_in.data["otp_required"] is False
        assert signed_in.data["access"] and signed_in.data["refresh"]
        assert mail.outbox == []
        audit = AuditLog.objects.filter(
            action=AuditLog.Action.LOGIN_SUCCESS, entity_id=alice.pk
        ).latest("pk")
        assert audit.detail == {"mfa": False, "reason": "disabled_for_user"}

        # And back on: the next sign-in asks for a code again.
        admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"mfa_enabled": True},
            format="json",
        )
        assert login(alice.email).data["otp_required"] is True

    def test_global_switch_off_wins_over_the_user_flag(self, alice, settings):
        settings.REQUIRE_OTP_FOR_LOGIN = False
        alice.mfa_enabled = True
        alice.save(update_fields=["mfa_enabled"])
        response = login(alice.email)
        assert response.data["otp_required"] is False
        audit = AuditLog.objects.filter(
            action=AuditLog.Action.LOGIN_SUCCESS, entity_id=alice.pk
        ).latest("pk")
        assert audit.detail["reason"] == "disabled_globally"

    def test_only_an_administrator_can_switch_it(self, alice, user_factory):
        client = APIClient()
        client.force_authenticate(
            user=user_factory(email="lead@example.com", roles=["BCM_BU_LEAD"])
        )
        response = client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"mfa_enabled": False},
            format="json",
        )
        assert response.status_code == 403
        alice.refresh_from_db()
        assert alice.mfa_enabled is True

    def test_profile_shows_the_flag(self, alice):
        client = APIClient()
        client.force_authenticate(user=alice)
        assert client.get(reverse("accounts:auth:me")).data["mfa_enabled"] is True


class TestUserAdministration:
    def test_list_search_and_filter(self, admin_client, alice, user_factory):
        user_factory(email="bob@example.com", display_name="Bob", user_status="Pending")
        url = reverse("accounts:admin-user-list")
        everyone = admin_client.get(url).data
        rows = everyone["results"] if isinstance(everyone, dict) else everyone
        assert {r["email"] for r in rows} >= {
            "alice@example.com",
            "bob@example.com",
            "root@example.com",
        }
        assert all("mfa_enabled" in r and "estate_ids" in r and "role_codes" in r for r in rows)
        pending = admin_client.get(url, {"user_status": "Pending"}).data
        pending_rows = pending["results"] if isinstance(pending, dict) else pending
        assert [r["email"] for r in pending_rows] == ["bob@example.com"]
        found = admin_client.get(url, {"search": "alic"}).data
        found_rows = found["results"] if isinstance(found, dict) else found
        assert [r["email"] for r in found_rows] == ["alice@example.com"]

    def test_one_patch_activates_links_and_grants(self, admin_client, alice):
        estate = Estate.objects.create(estate_name="Alpha")
        other = Estate.objects.create(estate_name="Beta")
        employee = Employee.objects.create(
            employee_number="E-77", full_name="Alice Employee", email="alice@example.com"
        )
        alice.user_status = "Pending"
        alice.save(update_fields=["user_status"])

        response = admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {
                "user_status": "Active",
                "employee_id": employee.pk,
                "role_codes": ["BCM_COORDINATOR", "BCM_VIEWER"],
                "estate_ids": [estate.pk, other.pk],
            },
            format="json",
        )
        assert response.status_code == 200, response.data
        body = response.data
        assert body["user_status"] == "Active" and body["employee"]["employee_number"] == "E-77"
        assert body["role_codes"] == ["BCM_COORDINATOR", "BCM_VIEWER"]
        assert body["estate_ids"] == sorted([estate.pk, other.pk])

        # Narrow: roles and estates are reconciled, not appended; a removed estate is deactivated, not deleted.
        narrowed = admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"role_codes": ["BCM_COORDINATOR"], "estate_ids": [estate.pk]},
            format="json",
        )
        assert narrowed.data["role_codes"] == ["BCM_COORDINATOR"] and narrowed.data[
            "estate_ids"
        ] == [estate.pk]
        assert UserRole.objects.filter(user=alice).count() == 1
        assert UserEstateScope.objects.get(user=alice, estate=other).active_flag is False
        assert UserEstateScope.objects.filter(user=alice).count() == 2

        # Re-granting reactivates the same row.
        admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"estate_ids": [estate.pk, other.pk]},
            format="json",
        )
        assert UserEstateScope.objects.filter(user=alice).count() == 2
        assert UserEstateScope.objects.get(user=alice, estate=other).active_flag is True

        audit = AuditLog.objects.filter(
            action=AuditLog.Action.RECORD_UPDATED, entity_type="UserAccount", entity_id=alice.pk
        ).first()
        assert audit is not None and "role_codes" in audit.detail["changed"]

    def test_unknown_role_or_estate_is_refused(self, admin_client, alice):
        url = reverse("accounts:admin-user-detail", args=[alice.pk])
        assert (
            admin_client.patch(url, {"role_codes": ["BCM_WIZARD"]}, format="json").status_code
            == 400
        )
        assert admin_client.patch(url, {"estate_ids": [999999]}, format="json").status_code == 400
        assert sorted(alice.role_codes) == ["BCM_VIEWER"]

    def test_an_employee_links_to_one_account_only(self, admin_client, alice, user_factory):
        employee = Employee.objects.create(
            employee_number="E-1", full_name="Shared", email="shared@example.com"
        )
        other = user_factory(email="other@example.com")
        other.employee = employee
        other.save(update_fields=["employee"])
        response = admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"employee_id": employee.pk},
            format="json",
        )
        assert response.status_code == 400 and "employee_id" in response.data["field_errors"]

    def test_deactivation_locks_the_user_out(self, admin_client, alice):
        admin_client.patch(
            reverse("accounts:admin-user-detail", args=[alice.pk]),
            {"is_active": False},
            format="json",
        )
        assert login(alice.email).status_code in (400, 401, 403)
        assert UserAccount.objects.get(pk=alice.pk).is_active is False
