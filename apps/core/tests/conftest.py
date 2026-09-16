"""Call tree and crisis tests reuse the plans fixtures: an org, an Approved version and its coordinator."""

from apps.plans.tests.conftest import (  # noqa: F401 - re-exported fixtures
    actor,
    admin_client,
    approved_version,
    coordinator_client,
    employee,
    org,
    question,
)
