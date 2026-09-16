"""
Test scheduling and outcomes (Phase 8).

A test is scheduled against a cost code's current plan version. Its status is
a consequence of what happened, not something a user sets:

    Scheduled     created, or rescheduled
    In Progress   a call tree run has been started for it
    Completed     an outcome was recorded
    Cancelled     cancelled while Scheduled or In Progress

Recording an outcome closes the test. A report can be attached to the outcome;
it is stored like any other document and reached through the same signed link.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apps.accounts.services import record_audit
from apps.calltree.engine import start_run
from apps.core.exceptions import DomainError, InvalidStateTransition
from apps.core.models import AuditLog
from apps.documents.models import EntityDocument
from apps.documents.uploads import attach, store_upload
from apps.organization.models import CostCode
from apps.plans.models import PlanVersion
from apps.testing.models import Test, TestOutcome

logger = logging.getLogger(__name__)

REPORT_DOCUMENT_TYPE = "TEST_FINAL_REPORT"


class NotACallTreeTest(DomainError):
    default_detail = "Only a Call Tree Test can be executed through the call tree."
    default_code = "not_a_call_tree_test"


class TestClosed(InvalidStateTransition):
    default_detail = "This test is closed and cannot be changed."
    default_code = "test_closed"


def current_version(cost_code: CostCode) -> PlanVersion | None:
    return PlanVersion.objects.filter(plan__cost_code=cost_code).order_by("-version_number").first()


def _assert_open(test: Test) -> None:
    if test.status in (Test.Status.COMPLETED, Test.Status.CANCELLED):
        raise TestClosed()


@transaction.atomic
def schedule_test(cost_code: CostCode, *, actor, data: dict) -> Test:
    version = current_version(cost_code)
    if version is None:
        raise DomainError("This cost code has no plan version to test against.")
    test = Test.objects.create(
        plan_version=version,
        test_type=data["test_type"],
        scheduled_date=data.get("scheduled_date"),
        scheduled_time=data.get("scheduled_time"),
        comments=data.get("comments", ""),
        initiated_by=actor,
        status=Test.Status.SCHEDULED,
    )
    record_audit(
        action=AuditLog.Action.RECORD_CREATED,
        actor=actor,
        entity_type="Test",
        entity_id=test.pk,
        detail={"test_type": test.test_type, "scheduled_date": str(test.scheduled_date or "")},
    )
    return test


def reschedule_test(test: Test, *, actor, data: dict) -> Test:
    _assert_open(test)
    changed = {}
    for field in ("test_type", "scheduled_date", "scheduled_time", "comments"):
        if field in data and getattr(test, field) != data[field]:
            changed[field] = str(data[field] or "")
            setattr(test, field, data[field])
    if changed:
        test.save(update_fields=list(changed))
        record_audit(
            action=AuditLog.Action.RECORD_UPDATED,
            actor=actor,
            entity_type="Test",
            entity_id=test.pk,
            detail=changed,
        )
    return test


def cancel_test(test: Test, *, actor) -> Test:
    _assert_open(test)
    test.status = Test.Status.CANCELLED
    test.save(update_fields=["status"])
    record_audit(
        action=AuditLog.Action.STATUS_TRANSITION,
        actor=actor,
        entity_type="Test",
        entity_id=test.pk,
        detail={"to": test.status},
    )
    return test


@transaction.atomic
def start_call_tree(test: Test, *, actor, simulation: bool) -> Test:
    _assert_open(test)
    if test.test_type != Test.TestType.CALL_TREE:
        raise NotACallTreeTest()
    if test.call_tree_run_id and test.call_tree_run.status in ("PENDING", "RUNNING"):
        raise DomainError("A call tree is already running for this test.", code="call_tree_running")
    cost_code = test.plan_version.plan.cost_code
    run = start_run(
        cost_code=cost_code,
        plan_version=test.plan_version,
        initiated_by=actor,
        simulation=simulation,
        call_tree_type=test.test_type,
    )
    test.call_tree_run = run
    test.status = Test.Status.IN_PROGRESS
    test.save(update_fields=["call_tree_run", "status"])
    record_audit(
        action=AuditLog.Action.STATUS_TRANSITION,
        actor=actor,
        entity_type="Test",
        entity_id=test.pk,
        detail={"to": test.status, "call_tree_run_id": run.pk, "simulation": simulation},
    )
    return test


@transaction.atomic
def record_outcome(test: Test, *, actor, data: dict, report=None) -> TestOutcome:
    _assert_open(test)
    outcome = TestOutcome.objects.create(
        test=test,
        conducted_date=data.get("conducted_date"),
        conducted_time=data.get("conducted_time"),
        result=data.get("result", ""),
        final_status=data["final_status"],
    )
    if report is not None:
        document = store_upload(report, actor=actor)
        outcome.final_report_document = document
        outcome.save(update_fields=["final_report_document"])
        attach(
            document,
            entity_type=EntityDocument.EntityType.TEST_OUTCOME,
            entity_id=outcome.pk,
            document_type=REPORT_DOCUMENT_TYPE,
        )

    test.status = Test.Status.COMPLETED
    test.save(update_fields=["status"])
    record_audit(
        action=AuditLog.Action.STATUS_TRANSITION,
        actor=actor,
        entity_type="Test",
        entity_id=test.pk,
        detail={"to": test.status, "final_status": outcome.final_status, "report": bool(report)},
    )
    return outcome


def report_attachment(outcome: TestOutcome) -> EntityDocument | None:
    if not outcome.final_report_document_id:
        return None
    return EntityDocument.objects.filter(
        document_id=outcome.final_report_document_id,
        entity_type=EntityDocument.EntityType.TEST_OUTCOME,
        entity_id=outcome.pk,
    ).first()
