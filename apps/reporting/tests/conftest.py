"""Reporting tests reuse the plans fixtures: an org, an Approved version and its coordinator."""

import pytest

from apps.plans.tests.conftest import (  # noqa: F401 - re-exported fixtures
    actor,
    admin_client,
    approved_version,
    coordinator_client,
    employee,
    org,
    question,
)


@pytest.fixture(autouse=True)
def _catalogue(db):
    """The rating catalogue: the heat map axes come from it."""
    from django.core.management import call_command

    from apps.lookups.models import LookupCategory

    if not LookupCategory.objects.filter(category_type="Likelihood", points__isnull=False).exists():
        call_command("seed_reference_data", verbosity=0)
