"""A fresh seed produces no duplicate catalogue rows and loses no child values."""

import pytest
from django.core.management import call_command

from apps.lookups.models import LookupCategory, LookupValue
from apps.lookups.services import duplicate_groups

pytestmark = pytest.mark.django_db


def test_fresh_seed_has_no_duplicates_and_keeps_every_value():
    LookupValue.objects.all().delete()
    LookupCategory.objects.all().delete()
    call_command("seed_reference_data", verbosity=0)
    assert duplicate_groups() == []
    assert LookupCategory.objects.filter(category_type="Likelihood").count() == 3
    assert (
        LookupCategory.objects.filter(category_type="Likelihood", points__isnull=True).count() == 0
    )
    # Every legacy value row landed somewhere: the CSV has 116 rows with a category.
    assert LookupValue.objects.count() == 116
    # Values whose legacy parent was a retired duplicate hang off the survivor.
    irm = LookupCategory.objects.get(category_type="Corporate function", category_name="IRM")
    assert irm.values.exists()
    # Idempotent.
    call_command("seed_reference_data", verbosity=0)
    assert duplicate_groups() == [] and LookupValue.objects.count() == 116
