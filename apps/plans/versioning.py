"""
Copy-on-write plan versioning (AD-4) — Phase 3.4.

An Approved plan version is immutable. Changing anything means copying it to a new
version, which is what gives journey step 4 its "current / previous versions" list
and what makes an approval mean something a year later: the document generated in
Phase 7 can always be traced back to content that has not moved since.

The whole copy happens in one transaction. A half-copied version is worse than no
copy at all — it looks complete in the UI and silently loses a section.

What carries forward and what does not
--------------------------------------
The dividing line is *plan content* versus *things that happened*.

Content describes the plan's current answer to "how would we recover?", so it is
the starting point for the next cycle and is copied: answers, section statuses,
BIA descriptions, critical contacts, network requirements, risks and their
actions, recovery strategies, and the coordinator roster.

Events record something that occurred against one specific version — a status
transition, a crisis, a call tree run, a test, an exemption request, a review
comment. Copying those would fabricate history: the new version would claim tests
it never ran and exemptions nobody requested. They stay where they happened, and
the history timeline (Phase 3.5) reads them per version.

Two exclusions are less obvious and so are stated explicitly:

* `cmsc_members` — the crisis roster hangs off the **cost code**, not the version
  (`plan_version` there is nullable and incidental). Copying it would duplicate
  every person on the roster on each new version.
* `entity_documents` with `entity_type=PLAN_VERSION` — a generated document
  belongs to the version it was generated from and must never be re-pointed at a
  new one. Whether *uploaded* attachments should follow a copy is a Phase 7
  question, deliberately left open rather than silently decided here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import models, transaction
from django.db.models import Max

from apps.assessments.models import (
    BiaCriticalContact,
    BiaServiceDescription,
    NetworkRequirement,
    QuestionAnswer,
    SectionStatus,
)
from apps.plans.models import (
    CoordinatorAssignment,
    Plan,
    PlanStatus,
    PlanStatusHistory,
    PlanVersion,
)
from apps.risk.models import RecoveryStrategy, Risk, RiskAction

logger = logging.getLogger(__name__)


class PlanVersionError(Exception):
    """A versioning rule was violated. Surfaces as 400, not 500."""

    def __init__(self, message: str, code: str = "plan_version_error"):
        super().__init__(message)
        self.code = code


#: Statuses a version can be copied *from*. Copying an in-flight version would
#: produce two live versions of the same plan and no way to say which is current.
COPYABLE_STATUSES = frozenset({PlanStatus.APPROVED, PlanStatus.EXEMPTED})

#: Statuses that mean a version is still live. A plan may have at most one.
OPEN_STATUSES = frozenset(
    {
        PlanStatus.NOT_STARTED,
        PlanStatus.WORK_IN_PROGRESS,
        PlanStatus.PENDING_BU_LEAD_REVIEW,
        PlanStatus.REWORK,
    }
)


@dataclass(frozen=True)
class ChildRelation:
    """A grandchild of the plan version, reached through a copied parent row."""

    label: str
    model: type[models.Model]
    parent_field: str


@dataclass(frozen=True)
class CarriedRelation:
    """A table copied wholesale from one plan version to the next."""

    label: str
    model: type[models.Model]
    children: tuple[ChildRelation, ...] = field(default_factory=tuple)


#: The complete carried-forward set. Adding a content table to the schema means
#: adding it here — `test_every_plan_version_child_is_classified` fails otherwise,
#: so a new table cannot be forgotten silently.
CARRIED_RELATIONS: tuple[CarriedRelation, ...] = (
    CarriedRelation("answers", QuestionAnswer),
    CarriedRelation("section_statuses", SectionStatus),
    CarriedRelation("service_descriptions", BiaServiceDescription),
    CarriedRelation("critical_contacts", BiaCriticalContact),
    CarriedRelation("network_requirements", NetworkRequirement),
    CarriedRelation("recovery_strategies", RecoveryStrategy),
    CarriedRelation("coordinator_assignments", CoordinatorAssignment),
    CarriedRelation(
        "risks",
        Risk,
        children=(ChildRelation("risk_actions", RiskAction, parent_field="risk"),),
    ),
)

#: Tables that reference a plan version and are deliberately NOT copied. Listed so
#: the exclusion is a decision on the record rather than an oversight; the
#: classification test reads this too.
NOT_CARRIED = {
    "plans.PlanStatusHistory": "the previous version's own audit trail",
    "assessments.QuestionComment": "review conversation about that specific cycle",
    "crisis.CrisisEvent": "an event that actually occurred",
    "crisis.CmscMember": "roster belongs to the cost code, not the version",
    "calltree.CallTreeRun": "a run that actually happened",
    "testing.Test": "a test that was actually performed",
    "exemptions.Exemption": "a request raised against that specific version",
}


def _clone(instance: models.Model, **overrides) -> models.Model:
    """Build an unsaved duplicate of `instance` with a fresh primary key.

    Copies raw column values via `attname` (`plan_version_id`, not
    `plan_version`), so no related object is fetched per row. `auto_now_add`
    columns are left out and re-stamped on save — a copy is created now, and
    claiming otherwise would misdate the audit trail.
    """
    data = {}
    for model_field in instance._meta.concrete_fields:
        if model_field.primary_key:
            continue
        if getattr(model_field, "auto_now_add", False) or getattr(model_field, "auto_now", False):
            continue
        data[model_field.attname] = getattr(instance, model_field.attname)
    data.update(overrides)
    return type(instance)(**data)


def _copy_relation(spec: CarriedRelation, source: PlanVersion, target: PlanVersion) -> int:
    """Copy one relation, and any grandchildren, returning the row count."""
    rows = list(spec.model.objects.filter(plan_version=source))
    if not rows:
        return 0

    if not spec.children:
        # No grandchildren, so the new primary keys are never needed. MySQL does
        # not return them from a bulk insert anyway.
        spec.model.objects.bulk_create([_clone(row, plan_version_id=target.pk) for row in rows])
        return len(rows)

    # Saved one at a time precisely because the new keys ARE needed, to re-parent
    # the grandchildren. `bulk_create` cannot supply them on MySQL.
    total = 0
    for row in rows:
        clone = _clone(row, plan_version_id=target.pk)
        clone.save()
        total += 1
        for child in spec.children:
            children = list(child.model.objects.filter(**{child.parent_field: row}))
            if children:
                child.model.objects.bulk_create(
                    [
                        _clone(grandchild, **{f"{child.parent_field}_id": clone.pk})
                        for grandchild in children
                    ]
                )
                total += len(children)
    return total


def next_version_number(plan: Plan) -> int:
    highest = PlanVersion.objects.filter(plan=plan).aggregate(highest=Max("version_number"))[
        "highest"
    ]
    return (highest or 0) + 1


def _copy_uploads(source: PlanVersion, target: PlanVersion) -> int:
    """Carry the author's uploads — evidence behind answers, the network
    diagram — to the new version. They attach through `entity_documents`
    rather than a foreign key, so `CARRIED_RELATIONS` cannot see them.

    Generated outputs (the approved BCP document) are deliberately left behind:
    they belong to the version that was approved. The files themselves are
    content-addressed and shared; only the attachment rows are duplicated.
    """
    from apps.documents.attachments import ATTACHMENT_TYPES
    from apps.documents.models import EntityDocument
    from apps.questionnaire.evidence import EVIDENCE_TYPE_PREFIX

    rows = EntityDocument.objects.filter(
        entity_type=EntityDocument.EntityType.PLAN_VERSION, entity_id=source.pk
    ).filter(
        models.Q(document_type__startswith=EVIDENCE_TYPE_PREFIX)
        | models.Q(document_type__in=list(ATTACHMENT_TYPES))
    )
    created = 0
    for row in rows:
        _, made = EntityDocument.objects.get_or_create(
            document=row.document,
            entity_type=EntityDocument.EntityType.PLAN_VERSION,
            entity_id=target.pk,
            document_type=row.document_type,
        )
        created += int(made)
    return created


@transaction.atomic
def copy_plan_version(
    source: PlanVersion,
    *,
    actor=None,
    comments: str = "",
) -> PlanVersion:
    """Deep-copy `source` into a new editable version of the same plan.

    Returns the new version, in `Work in Progress`. The source is left completely
    untouched — that is the property the whole design exists to provide, and
    `test_the_source_version_is_untouched` asserts it directly.
    """
    # Lock the plan row for the duration. Two coordinators pressing "new version"
    # at the same moment would otherwise both read the same highest version number
    # and race; one would hit the (plan, version_number) unique constraint and see
    # an opaque IntegrityError instead of simply queueing behind the other.
    plan = Plan.all_objects.select_for_update().get(pk=source.plan_id)

    if source.status not in COPYABLE_STATUSES:
        raise PlanVersionError(
            f"A version can only be copied once it is {' or '.join(sorted(COPYABLE_STATUSES))}. "
            f"This one is {source.status}.",
            code="source_not_copyable",
        )

    open_version = (
        PlanVersion.objects.filter(plan=plan, status__in=OPEN_STATUSES)
        .order_by("-version_number")
        .first()
    )
    if open_version is not None:
        raise PlanVersionError(
            f"Version {open_version.version_number} of this plan is still open "
            f"({open_version.status}). Finish or exempt it before starting a new one.",
            code="open_version_exists",
        )

    target = PlanVersion.objects.create(
        plan=plan,
        version_number=next_version_number(plan),
        plan_mode=source.plan_mode,
        review_mode=source.review_mode,
        status=PlanStatus.WORK_IN_PROGRESS,
        published_flag=False,
        copied_flag=True,
        # Approval belongs to the version that was approved, never to its copy.
        approved_by=None,
        approved_at=None,
        created_by=actor,
        updated_by=actor,
    )

    copied = {spec.label: _copy_relation(spec, source, target) for spec in CARRIED_RELATIONS}
    copied["uploads"] = _copy_uploads(source, target)

    PlanStatusHistory.objects.create(
        plan_version=target,
        status=PlanStatus.WORK_IN_PROGRESS,
        comments=comments or f"Copied from version {source.version_number} ({source.status}).",
        changed_by=actor,
    )

    logger.info(
        "Copied plan version %s -> %s (v%s): %s",
        source.pk,
        target.pk,
        target.version_number,
        copied,
    )
    return target


def ensure_current_version(plan: Plan, *, actor=None) -> PlanVersion:
    """Return the plan's newest version, creating a `Not Started` one if it has none.

    Phase 3.6. A cost code with a plan but no version is a dead end in the UI —
    every row action needs a version to act on. Creating it lazily, on first
    request, avoids seeding an empty version for thousands of cost codes nobody
    has opened.
    """
    existing = PlanVersion.objects.filter(plan=plan).order_by("-version_number").first()
    if existing is not None:
        return existing

    with transaction.atomic():
        locked = Plan.all_objects.select_for_update().get(pk=plan.pk)
        # Re-check under the lock: two concurrent first-opens would otherwise both
        # find nothing and both create version 1.
        existing = PlanVersion.objects.filter(plan=locked).order_by("-version_number").first()
        if existing is not None:
            return existing

        version = PlanVersion.objects.create(
            plan=locked,
            version_number=1,
            status=PlanStatus.NOT_STARTED,
            created_by=actor,
            updated_by=actor,
        )
        PlanStatusHistory.objects.create(
            plan_version=version,
            status=PlanStatus.NOT_STARTED,
            comments="Plan version created.",
            changed_by=actor,
        )
        return version
