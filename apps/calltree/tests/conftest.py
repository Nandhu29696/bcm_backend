"""Call tree and crisis tests reuse the plans fixtures: an org, an Approved version and its coordinator."""

import pytest

from apps.crisis.models import CmscMember
from apps.plans.tests.conftest import (  # noqa: F401 - re-exported fixtures
    actor,
    admin_client,
    approved_version,
    coordinator_client,
    employee,
    org,
    question,
)


@pytest.fixture
def roster(org, approved_version):  # noqa: F811 - pytest fixtures by name
    """Three members whose phone numbers steer the fake providers down each path.

    ...2  answered on the third voice call
    ...6  never answers voice, acknowledges the Teams call
    ...9  reached by nobody: three emails, the last to the BU lead
    """
    CmscMember.objects.filter(cost_code=org["cost_code"]).delete()
    members = [
        CmscMember.objects.create(
            cost_code=org["cost_code"],
            member_name="Asha Voice",
            member_email="asha@example.com",
            country_code="+91",
            phone_number="9000000002",
            reporting_manager_email="mgr.asha@example.com",
        ),
        CmscMember.objects.create(
            cost_code=org["cost_code"],
            member_name="Bala Teams",
            member_email="bala@example.com",
            country_code="+91",
            phone_number="9000000006",
        ),
        CmscMember.objects.create(
            cost_code=org["cost_code"],
            member_name="Chitra Email",
            member_email="chitra@example.com",
            country_code="+91",
            phone_number="9000000009",
            reporting_manager_email="mgr.chitra@example.com",
        ),
    ]
    return members
