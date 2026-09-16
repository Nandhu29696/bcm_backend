"""
Seed roles and the lookup catalogue.

Idempotent: running it twice leaves row counts unchanged. It replaces the role and
lookup portions of the retired `database/sample_data.sql`, which could not be run
repeatedly (it inserts explicit primary keys 1-10 and collides on a second run).
"""

from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Role
from apps.core.management.commands._csvutil import read_legacy_csv, to_int
from apps.lookups.models import LookupCategory, LookupValue

# The permission primitives AD-3 keys on. Defined here rather than loaded from the
# legacy CSV (which holds only four) because permission classes reference these
# codes directly.
ROLES = [
    ("BCM_ADMIN", "BCM Administrator"),
    ("BCM_COORDINATOR", "BCM Coordinator"),
    ("BCM_REVIEWER", "BCM Reviewer"),
    ("BCM_VIEWER", "BCM Viewer"),
    ("BCM_APPROVER", "BCM Approver"),
    ("BCM_BU_LEAD", "Business Unit Lead"),
    ("BCM_RISK_OWNER", "Risk Owner"),
    ("BCM_TEST_MANAGER", "Test Manager"),
    ("BCM_DOCUMENT_CONTROLLER", "Document Controller"),
    ("BCM_AUDITOR", "Auditor"),
]


class Command(BaseCommand):
    help = "Seed roles and the scored lookup catalogue. Safe to re-run."

    @transaction.atomic
    def handle(self, *args, **options):
        self._seed_roles()
        self._seed_lookups()
        self.stdout.write(self.style.SUCCESS("Reference data seeded."))

    def _seed_roles(self):
        created = 0
        for index, (code, name) in enumerate(ROLES, start=1):
            _, was_created = Role.objects.update_or_create(
                role_code=code,
                defaults={"role_name": name, "legacy_id": index, "active_flag": True},
            )
            created += int(was_created)
        self.stdout.write(f"  roles: {Role.objects.count()} total ({created} new)")

    def _seed_lookups(self):
        rows = read_legacy_csv("BCP_BIA_Category.csv")
        # legacy id of a skipped duplicate -> the row that stands for it.
        aliases: dict[int, LookupCategory] = {}
        for row in rows:
            legacy_id = to_int(row.get("ID"))
            if legacy_id is None:
                continue
            points = None
            raw_points = (row.get("Point") or "").strip()
            if raw_points:
                try:
                    points = Decimal(raw_points)
                except InvalidOperation:
                    points = None
            name = row.get("Title", "")
            category_type = row.get("Category_Type", "")
            existing_by_name = LookupCategory.objects.filter(
                category_type__iexact=category_type.strip(), category_name__iexact=name.strip()
            ).exclude(legacy_id=legacy_id)
            survivor = existing_by_name.first()
            if (
                survivor is not None
                and not LookupCategory.objects.filter(legacy_id=legacy_id).exists()
            ):
                # The other export already supplied this entry (and it may carry the
                # points); a second row under a new id would be the duplicate we retire.
                # Its child values attach to the survivor instead.
                aliases[legacy_id] = survivor
                continue
            LookupCategory.objects.update_or_create(
                legacy_id=legacy_id,
                defaults={"category_name": name, "category_type": category_type, "points": points},
            )

        by_legacy = {c.legacy_id: c for c in LookupCategory.objects.all()}
        by_legacy.update(aliases)

        skipped = 0
        for row in read_legacy_csv("BCP_BIA_SubCategory.csv"):
            legacy_id = to_int(row.get("ID"))
            category = by_legacy.get(to_int(row.get("CategoryID")))
            if legacy_id is None or category is None:
                skipped += 1
                continue
            LookupValue.objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "category": category,
                    "subcategory_name": row.get("Title", ""),
                },
            )

        self.stdout.write(
            f"  lookup categories: {LookupCategory.objects.count()}  "
            f"values: {LookupValue.objects.count()}"
            + (f"  (skipped {skipped} orphan rows)" if skipped else "")
        )

        # The two exports overlap; collapse duplicates so scoring and answer
        # options never see the same entry twice (PENDING #12).
        from apps.lookups.services import dedupe_catalogue

        deduped = dedupe_catalogue()
        if deduped["retired"]:
            self.stdout.write(
                f"  catalogue: retired {deduped['retired']} duplicate rows in "
                f"{deduped['groups']} groups, moved {deduped['values_moved']} values, "
                f"rewrote {deduped['answers_rewritten']} answers"
            )

        # order_by() clears Meta.ordering — without it those columns join the
        # SELECT and DISTINCT stops deduplicating.
        types = (
            LookupCategory.objects.exclude(category_type="")
            .order_by()
            .values_list("category_type", flat=True)
            .distinct()
        )
        self.stdout.write(f"  category types ({len(types)}): {', '.join(sorted(types))}")
