"""
Seed a demo dataset.

Replaces `database/sample_data.sql` and `database/ten_rows_sample_data.sql`, both
of which are now retired: they insert explicit primary keys 1-10 (so they collide
on a second run) and neither can hash a password, which makes them useless for
seeding logins.

Plan versions are deliberately spread across all six statuses so the Phase 2 cost
code list has something real to filter on.

Idempotent. Run `seed_reference_data` first — this depends on roles existing.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Employee, Role, UserAccount, UserEstateScope, UserRole
from apps.organization.models import (
    BuClassification,
    BuLead,
    Center,
    CostCode,
    EmployeeGrade,
    EmployeeGroup,
    Estate,
    Lob,
    Location,
    Process,
    Region,
    Subprocess,
)
from apps.plans.models import (
    CoordinatorAssignment,
    Plan,
    PlanStatus,
    PlanStatusHistory,
    PlanVersion,
)

DEMO_PASSWORD = "Passw0rd!23"

REGIONS = [("South Asia", "Asia"), ("North America", "Americas"), ("Western Europe", "Europe")]
ESTATES = ["Bangalore Estate", "Chennai Estate", "London Estate"]
LOCATIONS = [("Bangalore", 0), ("Chennai", 0), ("London", 2)]
LOBS = ["Operations", "Technology", "Finance"]
CLASSIFICATIONS = ["Business Unit", "Support Function"]
GROUPS = ["Corporate Employee", "Contract Employee"]
GRADES = ["F2", "M1", "S1"]
BU_LEADS = [
    ("Priya Business Lead", "priya.lead@example.com"),
    ("Kavitha Tech Lead", "kavitha.tech@example.com"),
    ("Ramesh Finance Lead", "ramesh.finance@example.com"),
]
PROCESSES = [
    ("Customer Support Operations", 0, 0),
    ("IT Infrastructure Support", 0, 1),
    ("Finance Operations", 0, 1),
    ("Marketing Operations", 2, 2),
]
SUBPROCESSES = [
    ("Customer Contact Handling", 0),
    ("Network Support", 1),
    ("Accounts Payable", 2),
    ("Digital Campaigns", 3),
]

# (full_name, employee_number, designation)
EMPLOYEES = [
    ("Priya Business Lead", "1100001", "Business Unit Lead"),
    ("Arun Coordinator", "1100002", "BCM Coordinator"),
    ("Meera Analyst", "1100003", "Business Continuity Analyst"),
    ("Rahul BCM Officer", "1100004", "BCM Officer"),
    ("Sneha Risk Analyst", "1100005", "Risk Analyst"),
    ("Vikram IT Support", "1100008", "IT Support Engineer"),
    ("Lakshmi Quality Auditor", "1100013", "Quality Auditor"),
]

# (email, display_name, employee index, role_code, is_staff)
USERS = [
    ("admin@example.com", "BCM Administrator", None, "BCM_ADMIN", True),
    ("arun.coordinator@example.com", "Arun Coordinator", 1, "BCM_COORDINATOR", False),
    ("priya.lead@example.com", "Priya Business Lead", 0, "BCM_BU_LEAD", False),
    ("meera.analyst@example.com", "Meera Analyst", 2, "BCM_REVIEWER", False),
    ("lakshmi.audit@example.com", "Lakshmi Quality Auditor", 6, "BCM_AUDITOR", False),
    ("vikram.itsupport@example.com", "Vikram IT Support", 5, "BCM_VIEWER", False),
]

#: Demo users deliberately scoped to one estate rather than all of them, so the
#: estate list differs by who is signed in and data scoping (AD-3) is visible
#: rather than merely asserted in tests. The auditor is excluded on purpose —
#: BCM_AUDITOR is an all-estates role, so a scope row would change nothing.
SINGLE_ESTATE_USERS = {"vikram.itsupport@example.com", "meera.analyst@example.com"}

#: The status trail that leads to each seeded status, following
#: ALLOWED_TRANSITIONS. A version seeded straight into "Approved" with no
#: history would contradict the Phase 3 exit criterion that the timeline matches
#: the status trail exactly — and would render an empty History panel.
STATUS_TRAILS = {
    PlanStatus.NOT_STARTED: [PlanStatus.NOT_STARTED],
    PlanStatus.WORK_IN_PROGRESS: [PlanStatus.NOT_STARTED, PlanStatus.WORK_IN_PROGRESS],
    PlanStatus.PENDING_BU_LEAD_REVIEW: [
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
    ],
    PlanStatus.APPROVED: [
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.APPROVED,
    ],
    PlanStatus.REWORK: [
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.REWORK,
    ],
    PlanStatus.EXEMPTED: [
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.EXEMPTED,
    ],
}

HISTORY_COMMENTS = {
    PlanStatus.NOT_STARTED: "Plan version created.",
    PlanStatus.PENDING_BU_LEAD_REVIEW: "Submitted for BU lead review.",
    PlanStatus.APPROVED: "Reviewed and approved.",
    PlanStatus.REWORK: "RTO justification needs more detail.",
    PlanStatus.EXEMPTED: "Exemption approved: process is being decommissioned.",
}
APPROVER_STEPS = {PlanStatus.APPROVED, PlanStatus.REWORK}

# One plan version per cost code, cycling the six real statuses.
STATUS_CYCLE = [
    PlanStatus.APPROVED,
    PlanStatus.WORK_IN_PROGRESS,
    PlanStatus.PENDING_BU_LEAD_REVIEW,
    PlanStatus.NOT_STARTED,
    PlanStatus.REWORK,
    PlanStatus.EXEMPTED,
]


class Command(BaseCommand):
    help = "Seed a demo dataset with working logins. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cost-codes",
            type=int,
            default=12,
            help="How many cost codes to generate (default 12).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not Role.objects.exists():
            self.stderr.write(self.style.ERROR("No roles found. Run seed_reference_data first."))
            return

        org = self._seed_organization(options["cost_codes"])
        employees = self._seed_employees(org)
        self._seed_users(employees, org)
        self._seed_plans(org, employees)
        self._seed_rosters(org, employees)
        self._seed_plan_content(org, employees)
        self._seed_help()
        self._report()

    # -- organisation ------------------------------------------------------- #

    def _seed_organization(self, cost_code_count):
        regions = [
            Region.all_objects.update_or_create(
                region_name=name, defaults={"geography": geo, "active_flag": True}
            )[0]
            for name, geo in REGIONS
        ]
        estates = [
            Estate.all_objects.update_or_create(estate_name=name, defaults={"active_flag": True})[0]
            for name in ESTATES
        ]
        locations = [
            Location.all_objects.update_or_create(
                location_name=name,
                defaults={"region": regions[region_idx], "active_flag": True},
            )[0]
            for name, region_idx in LOCATIONS
        ]
        centers = [
            Center.all_objects.update_or_create(
                center_name=f"{loc.location_name} Central",
                defaults={"location": loc, "region": loc.region, "active_flag": True},
            )[0]
            for loc in locations
        ]
        lobs = [
            Lob.all_objects.update_or_create(lob_name=n, defaults={"active_flag": True})[0]
            for n in LOBS
        ]
        classifications = [
            BuClassification.all_objects.update_or_create(
                classification_name=n, defaults={"active_flag": True}
            )[0]
            for n in CLASSIFICATIONS
        ]
        groups = [
            EmployeeGroup.all_objects.update_or_create(
                group_name=n, defaults={"active_flag": True}
            )[0]
            for n in GROUPS
        ]
        grades = [
            EmployeeGrade.all_objects.update_or_create(
                grade_name=n, defaults={"active_flag": True}
            )[0]
            for n in GRADES
        ]
        bu_leads = [
            BuLead.all_objects.update_or_create(
                lead_name=name, defaults={"email": email, "active_flag": True}
            )[0]
            for name, email in BU_LEADS
        ]
        processes = [
            Process.all_objects.update_or_create(
                process_name=name,
                defaults={
                    "region": regions[region_idx],
                    "location": locations[loc_idx],
                    "active_flag": True,
                },
            )[0]
            for name, region_idx, loc_idx in PROCESSES
        ]
        subprocesses = [
            Subprocess.all_objects.update_or_create(
                subprocess_name=name,
                defaults={"process": processes[proc_idx], "active_flag": True},
            )[0]
            for name, proc_idx in SUBPROCESSES
        ]

        cost_codes = []
        for i in range(cost_code_count):
            process = processes[i % len(processes)]
            subprocess = subprocesses[i % len(subprocesses)]
            estate = estates[i % len(estates)]
            location = locations[i % len(locations)]
            cost_code, _ = CostCode.all_objects.update_or_create(
                cost_code=f"65-DEMO{i + 1:02d}",
                defaults={
                    "process": process,
                    "subprocess": subprocess,
                    "bu_lead": bu_leads[i % len(bu_leads)],
                    "estate": estate,
                    "location": location,
                    "region": location.region,
                    "center": centers[i % len(centers)],
                    "lob": lobs[i % len(lobs)],
                    "active_flag": True,
                },
            )
            cost_codes.append(cost_code)

        return {
            "regions": regions,
            "estates": estates,
            "locations": locations,
            "centers": centers,
            "lobs": lobs,
            "classifications": classifications,
            "groups": groups,
            "grades": grades,
            "bu_leads": bu_leads,
            "processes": processes,
            "subprocesses": subprocesses,
            "cost_codes": cost_codes,
        }

    # -- people ------------------------------------------------------------- #

    def _seed_employees(self, org):
        employees = []
        for i, (full_name, number, designation) in enumerate(EMPLOYEES):
            cost_code = org["cost_codes"][i % len(org["cost_codes"])]
            employee, _ = Employee.all_objects.update_or_create(
                employee_number=number,
                defaults={
                    "full_name": full_name,
                    "email": f"{full_name.split()[0].lower()}@example.com",
                    "designation": designation,
                    "employment_status": "Active",
                    "bu_lead": org["bu_leads"][i % len(org["bu_leads"])],
                    "bu_classification": org["classifications"][i % 2],
                    "center": cost_code.center,
                    "process": cost_code.process,
                    "subprocess": cost_code.subprocess,
                    "cost_code": cost_code,
                    "estate": cost_code.estate,
                    "location": cost_code.location,
                    "region": cost_code.region,
                    "lob": cost_code.lob,
                    "employee_group": org["groups"][i % 2],
                    "employee_grade": org["grades"][i % 3],
                    "active_flag": True,
                },
            )
            employees.append(employee)

        # Reporting lines, set after creation so the targets exist.
        for employee in employees[1:]:
            employee.manager_employee = employees[0]
            employee.save(update_fields=["manager_employee"])

        return employees

    def _seed_users(self, employees, org):
        roles = {r.role_code: r for r in Role.objects.all()}

        for email, display_name, employee_idx, role_code, is_staff in USERS:
            user, _ = UserAccount.objects.update_or_create(
                email=email,
                defaults={
                    "display_name": display_name,
                    "username": email.split("@")[0],
                    "employee": employees[employee_idx] if employee_idx is not None else None,
                    "user_status": UserAccount.Status.ACTIVE,
                    "auth_provider": UserAccount.AuthProvider.LOCAL,
                    "is_staff": is_staff,
                    "is_superuser": is_staff,
                    "is_active": True,
                },
            )
            user.set_password(DEMO_PASSWORD)
            user.save(update_fields=["password"])

            UserRole.objects.update_or_create(user=user, role=roles[role_code])

            # Admins bypass estate scoping by role, so they get no explicit rows.
            if role_code == "BCM_ADMIN":
                continue

            # Not everyone gets every estate. With all users scoped to all three,
            # the estate list looks identical whoever signs in and scoping cannot
            # be demonstrated — or spotted when it breaks. `SINGLE_ESTATE_USERS`
            # sees only the first estate, so the difference is visible on the
            # landing screen.
            granted = org["estates"][:1] if email in SINGLE_ESTATE_USERS else org["estates"]
            for estate in granted:
                UserEstateScope.objects.update_or_create(
                    user=user, estate=estate, defaults={"active_flag": True}
                )
            UserEstateScope.objects.filter(user=user).exclude(estate__in=granted).delete()

    # -- plans -------------------------------------------------------------- #

    def _seed_plans(self, org, employees):
        coordinator = employees[1]
        creator = UserAccount.objects.filter(email="arun.coordinator@example.com").first()
        approver = UserAccount.objects.filter(email="priya.lead@example.com").first()

        for i, cost_code in enumerate(org["cost_codes"]):
            plan, _ = Plan.all_objects.update_or_create(
                process=cost_code.process,
                cost_code=cost_code,
                defaults={"owner_employee": coordinator, "active_flag": True},
            )
            status = STATUS_CYCLE[i % len(STATUS_CYCLE)]
            is_approved = status == PlanStatus.APPROVED
            # get_or_create, not update_or_create: once the workflow has moved a
            # version on (submitted, approved, copied), a re-seed must not drag
            # it back to its starting status. Only brand-new rows get one.
            version, _ = PlanVersion.objects.get_or_create(
                plan=plan,
                version_number=1,
                defaults={
                    "plan_mode": "New Plan",
                    "review_mode": "Annual Review",
                    "status": status,
                    "published_flag": is_approved,
                    "copied_flag": False,
                    "created_by": creator,
                    "approved_by": approver if is_approved else None,
                },
            )
            CoordinatorAssignment.all_objects.update_or_create(
                plan_version=version,
                employee=coordinator,
                coordinator_type="Primary",
                defaults={"estate": cost_code.estate, "active_flag": True},
            )
            # Written once: a trail is append-only, so a re-run must not extend it.
            if not PlanStatusHistory.objects.filter(plan_version=version).exists():
                for step in STATUS_TRAILS[status]:
                    PlanStatusHistory.objects.create(
                        plan_version=version,
                        status=step,
                        comments=HISTORY_COMMENTS.get(step, ""),
                        changed_by=approver if step in APPROVER_STEPS else creator,
                    )

    # -- CMSC rosters (Phase 8) --------------------------------------------- #

    def _seed_rosters(self, org, employees):
        """Four committee members per cost code.

        Phone endings are chosen so a simulated call tree shows every path: two
        reached by voice, one by Teams, one by nobody (escalated by email).
        """
        from apps.crisis.models import CmscMember

        roster = [
            ("Ravi Menon", "ravi.menon", "9100000003", "Priya Lead", "priya.lead@example.com"),
            ("Deepa Nair", "deepa.nair", "9100000001", "Priya Lead", "priya.lead@example.com"),
            ("Kiran Rao", "kiran.rao", "9100000006", "Ravi Menon", "ravi.menon@example.com"),
            ("Sunil Verma", "sunil.verma", "9100000009", "Ravi Menon", "ravi.menon@example.com"),
        ]
        for cost_code in org["cost_codes"]:
            for name, handle, phone, manager, manager_email in roster:
                CmscMember.all_objects.update_or_create(
                    cost_code=cost_code,
                    member_email=f"{handle}@example.com",
                    defaults={
                        "member_name": name,
                        "country_code": "+91",
                        "phone_number": phone,
                        "reporting_manager_name": manager,
                        "reporting_manager_email": manager_email,
                        "center": getattr(cost_code.center, "center_name", "") or "",
                        "process": cost_code.process,
                        "region": cost_code.region,
                        "bu_lead": cost_code.bu_lead,
                        "active_flag": True,
                    },
                )

    # -- structured plan content (Phases 5-7) ------------------------------ #

    def _seed_plan_content(self, org, employees):
        """BIA rows, a risk register and a strategy on every Approved / in-review
        version, so documents, the dashboard and the reports have something to show.
        Written once per version: an existing service description means done."""
        import datetime as dt
        from decimal import Decimal

        from apps.assessments.models import (
            BiaCriticalContact,
            BiaServiceDescription,
            NetworkRequirement,
            RequirementType,
        )
        from apps.risk.models import ActionType, RecoveryStrategy, Risk, RiskAction
        from apps.risk.scoring import apply_scores

        owner = employees[1]
        contact = employees[2] if len(employees) > 2 else owner
        versions = PlanVersion.objects.filter(
            plan__cost_code__in=org["cost_codes"],
            # Not Work in Progress: those are the versions people (and the
            # browser suite) edit, and seeded rows there would get in the way.
            status__in=[PlanStatus.APPROVED, PlanStatus.PENDING_BU_LEAD_REVIEW],
        ).select_related("plan__cost_code")
        for index, version in enumerate(versions):
            if BiaServiceDescription.objects.filter(plan_version=version).exists():
                continue
            cc = version.plan.cost_code
            BiaServiceDescription.objects.create(
                plan_version=version,
                process=cc.process,
                subprocess=cc.subprocess,
                cost_code=cc,
                owner_employee=owner,
                process_description=f"{cc.process.process_name if cc.process else 'Process'} for the {cc.cost_code} client account.",
                mao="48",
                mbco="60",
                rto="8",
                rpo="4",
            )
            BiaCriticalContact.objects.create(
                plan_version=version,
                employee=contact,
                contact_type="Primary",
                primary_phone="+91 98000 11111",
                seat_count=12 + index,
                voice_non_voice="Voice",
            )
            NetworkRequirement.objects.create(
                plan_version=version,
                employee=owner,
                requirement_type=RequirementType.BCP_PLAN,
                source_ip="10.10.0.1",
                destination_ip="10.20.0.1",
                port_number="443",
                connectivity_type="MPLS",
            )
            RecoveryStrategy.objects.create(
                plan_version=version,
                owner_employee=owner,
                core_strategy="Relocate to the alternate centre within the RTO.",
                tactical_strategy="Work from home for the first 48 hours; laptops and VPN pre-issued.",
            )
            for name, likelihood, severity, control, due_days, status in (
                ("Power failure at primary site", 3, 3, 1, -10, "Open"),
                ("Key staff unavailable during monsoon", 2, 2, 2, 20, "Open"),
                ("Network link degradation", 2, 3, 3, -40, "Mitigated"),
            )[: 1 + index % 3]:
                risk = Risk(
                    plan_version=version,
                    owner_employee=owner,
                    risk_name=name,
                    description=f"{name}: assessed for {cc.cost_code}.",
                    likelihood_rating=Decimal(likelihood),
                    severity_rating=Decimal(severity),
                    control_effectiveness_rating=Decimal(control),
                    target_closure_date=dt.date.today() + dt.timedelta(days=due_days + 30),
                )
                apply_scores(risk)
                risk.save()
                RiskAction.objects.create(
                    risk=risk,
                    action_type=ActionType.MITIGATION,
                    status=status,
                    description=f"Mitigate: {name.lower()}.",
                    target_date=dt.date.today() + dt.timedelta(days=due_days),
                )
                RiskAction.objects.create(
                    risk=risk,
                    action_type=ActionType.CONTINGENCY,
                    status="Open",
                    description=f"Contingency for {name.lower()}.",
                    target_date=dt.date.today() + dt.timedelta(days=due_days + 15),
                )

    # -- help library (Phase 9) -------------------------------------------- #

    def _seed_help(self):
        """Two guidance documents so the Help page is not empty on first sign-in."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.documents.models import EntityDocument
        from apps.documents.uploads import attach, store_upload
        from apps.helpcenter.models import HelpResource
        from apps.reporting.exports import to_pdf
        from apps.reporting.reports import Table

        admin = UserAccount.objects.filter(email="admin@example.com").first()
        guides = [
            (
                "BCM programme overview",
                "What business continuity management is, who does what, and the annual cycle.",
                "Guides",
                [
                    ["Step", "Who", "What"],
                    ["1", "Coordinator", "Complete the BCP questionnaire"],
                    ["2", "BU lead", "Review and approve"],
                    ["3", "BCM team", "Test the plan"],
                ],
            ),
            (
                "Call tree runbook",
                "How the CMSC call tree escalates and what to do when a member is not reached.",
                "Runbooks",
                [
                    ["Stage", "Channel", "Attempts"],
                    ["1", "Voice", "3"],
                    ["2", "Microsoft Teams", "1"],
                    ["3", "Email", "3 (last to the BU lead)"],
                ],
            ),
        ]
        for index, (title, description, category, rows) in enumerate(guides, start=1):
            if HelpResource.all_objects.filter(title=title).exists():
                continue
            table = Table(title=title, subtitle=description, columns=rows[0], rows=rows[1:])
            upload = SimpleUploadedFile(
                f"{title.lower().replace(' ', '-')}.pdf",
                to_pdf(table),
                content_type="application/pdf",
            )
            document = store_upload(upload, actor=admin)
            resource = HelpResource.objects.create(
                title=title,
                description=description,
                category=category,
                display_order=index,
                document=document,
                created_by=admin,
            )
            attach(
                document,
                entity_type=EntityDocument.EntityType.HELP_RESOURCE,
                entity_id=resource.pk,
                document_type="HELP_DOCUMENT",
            )

    # -- report ------------------------------------------------------------- #

    def _report(self):
        from apps.plans.models import PlanVersion as PV

        self.stdout.write("")
        self.stdout.write(
            f"  estates {Estate.objects.count()}  "
            f"cost codes {CostCode.objects.count()}  "
            f"employees {Employee.objects.count()}  "
            f"users {UserAccount.objects.count()}  "
            f"plans {Plan.objects.count()}"
        )
        self.stdout.write("  plan versions by status:")
        for status, _label in PlanStatus.choices:
            count = PV.objects.filter(status=status).count()
            if count:
                self.stdout.write(f"    {status:<24} {count}")
        self.stdout.write("")
        self.stdout.write("  logins (all share the same demo password):")
        for email, _dn, _ei, role_code, _s in USERS:
            self.stdout.write(f"    {email:<36} {role_code}")
        self.stdout.write(f"    password: {DEMO_PASSWORD}")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data seeded."))
