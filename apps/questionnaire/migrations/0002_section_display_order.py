"""
Reorder the section tabs: Basic Questions, MAO, RTO, MBCO, RPO, then BIA.

The legacy export ordered sections by id, which put MAO after BIA. The editor
now shows the contractual numbers together and BIA last, in its own part of
the plan. Same mapping as `seed_questionnaire.SECTION_ORDER`; the seed is
idempotent, and this migration covers databases that are not re-seeded.
"""

from django.db import migrations

SECTION_ORDER = {1: 1, 6: 2, 2: 3, 3: 4, 4: 5, 5: 6}


def reorder(apps, schema_editor):
    Section = apps.get_model("questionnaire", "Section")
    for legacy_id, order in SECTION_ORDER.items():
        Section.objects.filter(legacy_id=legacy_id).update(display_order=order)


def restore(apps, schema_editor):
    Section = apps.get_model("questionnaire", "Section")
    for legacy_id in SECTION_ORDER:
        Section.objects.filter(legacy_id=legacy_id).update(display_order=legacy_id)


class Migration(migrations.Migration):
    dependencies = [("questionnaire", "0001_initial")]

    operations = [migrations.RunPython(reorder, restore)]
