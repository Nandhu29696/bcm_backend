"""The documents tests reuse the plans fixtures: a fully populated Approved version."""

from apps.plans.tests.conftest import (  # noqa: F401 - re-exported fixtures
    actor,
    admin_client,
    approved_version,
    coordinator_client,
    employee,
    org,
    question,
)
