"""
Generate a realistically-sized dataset for profiling the cost code list (Phase 2.5).

The demo seed is ~12 cost codes. Every query in Phase 2 looks instant at that size,
including the ones that are quadratic. This command builds the volume at which the
difference shows: 50,000 cost codes and ~150,000 plan versions by default.

    python manage.py generate_load_data              # 50k / 3 versions each
    python manage.py generate_load_data --cost-codes 5000
    python manage.py generate_load_data --clear      # remove what it generated

Everything it creates is tagged with the `LOAD_` cost code prefix and a dedicated
estate, so `--clear` can remove it without touching demo or real data. It is a
profiling tool, never part of the normal seed chain.
"""

from __future__ import annotations

import random
import time

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.organization.models import (
    BuLead,
    CostCode,
    Estate,
    Lob,
    Location,
    Process,
    Region,
    Subprocess,
)
from apps.plans.models import Plan, PlanStatus, PlanVersion

LOAD_ESTATE = "Load Test Estate"
COST_CODE_PREFIX = "LOAD-"
BATCH = 2000


class Command(BaseCommand):
    help = "Generate a large synthetic dataset for Phase 2 performance profiling."

    def add_arguments(self, parser):
        parser.add_argument("--cost-codes", type=int, default=50_000)
        parser.add_argument("--versions-per-plan", type=int, default=3)
        parser.add_argument("--processes", type=int, default=40)
        parser.add_argument("--bu-leads", type=int, default=25)
        parser.add_argument("--seed", type=int, default=20260913)
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete the generated data and exit.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            return self.clear()

        random.seed(options["seed"])
        started = time.perf_counter()

        estate = self.build_estate()
        processes, subprocesses = self.build_processes(estate, options["processes"])
        bu_leads = self.build_bu_leads(options["bu_leads"])
        regions = list(Region.objects.all()[:5]) or [
            Region.objects.create(region_name="Load Region", geography="Test")
        ]
        lobs = list(Lob.objects.all()[:5]) or [Lob.objects.create(lob_name="Load LOB")]
        locations = list(Location.objects.all()[:5]) or [
            Location.objects.create(location_name="Load Location", region=regions[0])
        ]

        cost_codes = self.build_cost_codes(
            estate,
            processes,
            subprocesses,
            bu_leads,
            regions,
            lobs,
            locations,
            options["cost_codes"],
        )
        self.build_plans(cost_codes, options["versions_per_plan"])

        elapsed = time.perf_counter() - started
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated {len(cost_codes):,} cost codes and "
                f"{PlanVersion.objects.filter(plan__cost_code__estate=estate).count():,} "
                f"plan versions in {elapsed:.1f}s."
            )
        )
        self.stdout.write(f"Estate id: {estate.estate_id}")

    # ------------------------------------------------------------------ build

    def build_estate(self) -> Estate:
        estate, _ = Estate.objects.get_or_create(estate_name=LOAD_ESTATE)
        return estate

    def build_processes(self, estate, count: int):
        processes = []
        for index in range(count):
            process, _ = Process.objects.get_or_create(process_name=f"Load Process {index:03d}")
            processes.append(process)

        subprocesses = []
        for process in processes:
            for index in range(3):
                subprocess, _ = Subprocess.objects.get_or_create(
                    subprocess_name=f"{process.process_name} / Sub {index}",
                    process=process,
                )
                subprocesses.append(subprocess)
        return processes, subprocesses

    def build_bu_leads(self, count: int):
        leads = []
        for index in range(count):
            lead, _ = BuLead.objects.get_or_create(
                lead_name=f"Load Lead {index:03d}",
                defaults={"email": f"load.lead{index:03d}@example.com"},
            )
            leads.append(lead)
        return leads

    def build_cost_codes(
        self, estate, processes, subprocesses, bu_leads, regions, lobs, locations, count
    ):
        existing = CostCode.all_objects.filter(estate=estate).count()
        if existing >= count:
            self.stdout.write(f"{existing:,} cost codes already present; reusing.")
            return list(CostCode.all_objects.filter(estate=estate).only("cost_code_id"))

        by_process: dict[int, list] = {}
        for subprocess in subprocesses:
            by_process.setdefault(subprocess.process_id, []).append(subprocess)

        pending = []
        for index in range(existing, count):
            process = processes[index % len(processes)]
            pending.append(
                CostCode(
                    cost_code=f"{COST_CODE_PREFIX}{index:06d}",
                    estate=estate,
                    process=process,
                    subprocess=random.choice(by_process[process.process_id]),
                    bu_lead=random.choice(bu_leads),
                    region=random.choice(regions),
                    lob=random.choice(lobs),
                    location=random.choice(locations),
                    # A tenth are soft-deleted, so the active_flag filter has real
                    # work to do rather than matching every row.
                    active_flag=index % 10 != 0,
                )
            )
            if len(pending) >= BATCH:
                CostCode.all_objects.bulk_create(pending)
                pending = []
                self.stdout.write(f"  cost codes: {index + 1:,}/{count:,}", ending="\r")
        if pending:
            CostCode.all_objects.bulk_create(pending)

        self.stdout.write("")
        return list(CostCode.all_objects.filter(estate=estate).only("cost_code_id", "process_id"))

    def build_plans(self, cost_codes, versions_per_plan: int):
        statuses = list(PlanStatus.values)
        existing_plan_cost_codes = set(
            Plan.all_objects.filter(cost_code__in=[c.cost_code_id for c in cost_codes]).values_list(
                "cost_code_id", flat=True
            )
        )

        pending = []
        for cost_code in cost_codes:
            if cost_code.cost_code_id in existing_plan_cost_codes:
                continue
            pending.append(
                Plan(cost_code_id=cost_code.cost_code_id, process_id=cost_code.process_id)
            )
            if len(pending) >= BATCH:
                Plan.all_objects.bulk_create(pending)
                pending = []
        if pending:
            Plan.all_objects.bulk_create(pending)

        plan_ids = list(
            Plan.all_objects.filter(cost_code__in=[c.cost_code_id for c in cost_codes]).values_list(
                "plan_id", flat=True
            )
        )

        done = PlanVersion.objects.filter(plan_id__in=plan_ids[:1]).exists()
        if done:
            self.stdout.write("Plan versions already present; skipping.")
            return

        pending = []
        for count, plan_id in enumerate(plan_ids, start=1):
            for version_number in range(1, versions_per_plan + 1):
                # Only the newest version's status is ever read by the list, so
                # older versions get a settled status and the top one varies.
                status = (
                    random.choice(statuses)
                    if version_number == versions_per_plan
                    else PlanStatus.APPROVED
                )
                pending.append(
                    PlanVersion(plan_id=plan_id, version_number=version_number, status=status)
                )
            if len(pending) >= BATCH:
                PlanVersion.objects.bulk_create(pending)
                pending = []
                self.stdout.write(f"  plan versions: {count:,}/{len(plan_ids):,}", ending="\r")
        if pending:
            PlanVersion.objects.bulk_create(pending)
        self.stdout.write("")

    # ------------------------------------------------------------------ clear

    @transaction.atomic
    def clear(self):
        estate = Estate.all_objects.filter(estate_name=LOAD_ESTATE).first()
        if estate is None:
            self.stdout.write("Nothing to clear.")
            return

        cost_code_ids = list(
            CostCode.all_objects.filter(estate=estate).values_list("cost_code_id", flat=True)
        )
        plan_ids = list(
            Plan.all_objects.filter(cost_code_id__in=cost_code_ids).values_list(
                "plan_id", flat=True
            )
        )
        PlanVersion.objects.filter(plan_id__in=plan_ids).delete()
        Plan.all_objects.filter(plan_id__in=plan_ids).delete()
        CostCode.all_objects.filter(estate=estate).delete()
        Subprocess.all_objects.filter(subprocess_name__startswith="Load Process").delete()
        Process.all_objects.filter(process_name__startswith="Load Process").delete()
        BuLead.all_objects.filter(lead_name__startswith="Load Lead").delete()
        estate.delete()
        self.stdout.write(self.style.SUCCESS("Load test data removed."))
