"""
Concurrent saves (a regression from the browser).

Transactional, so the fixtures are committed and visible to the worker threads.
Named to sort last: a transactional test flushes the database at teardown, and
the questionnaire fixtures re-seed themselves if they find it empty.
"""

import threading

import pytest
from django.core.management import call_command
from django.db import connection

from apps.assessments.models import QuestionAnswer
from apps.questionnaire.models import Question
from apps.questionnaire.services import save_answer


@pytest.mark.django_db(transaction=True)
def test_concurrent_saves_of_one_question_do_not_collide(version, author):
    """No then Yes in quick succession raced to insert and 500ed.

    Reproduced in the browser by the sub-form E2E test: two writes for one
    question, both finding no existing row, both inserting, the second hitting
    the unique key. The service now locks the version row so the second waits
    and updates the first's row instead.
    """
    if not Question.objects.exists():
        call_command("seed_reference_data", verbosity=0)
        call_command("seed_questionnaire", verbosity=0)
    question = Question.objects.get(question_code="BIA-003")
    errors = []

    def worker(value):
        try:
            save_answer(version=version, question=question, answer={"value": value}, actor=author)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(v,)) for v in ("NO", "YES")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert QuestionAnswer.all_objects.filter(plan_version=version, question=question).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_edits_do_not_conflict_on_the_status_move(version, author):
    """Regression from the browser: two answers saved 3ms apart on a Not Started
    version. Both carried a stale Not Started instance; the second's
    mark_in_progress decided from it and then failed the WIP -> WIP check.
    """
    from apps.plans.models import PlanStatus, PlanStatusHistory, PlanVersion

    if not Question.objects.exists():
        call_command("seed_reference_data", verbosity=0)
        call_command("seed_questionnaire", verbosity=0)
    PlanVersion.objects.filter(pk=version.pk).update(status=PlanStatus.NOT_STARTED)
    version.refresh_from_db()
    q1 = Question.objects.get(question_code="BASIC-001")
    q4 = Question.objects.get(question_code="BASIC-004")
    errors = []

    def worker(question, value):
        try:
            save_answer(version=version, question=question, answer={"value": value}, actor=author)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(q1, "NO")),
        threading.Thread(target=worker, args=(q4, "YES")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    version.refresh_from_db()
    assert version.status == PlanStatus.WORK_IN_PROGRESS
    assert list(
        PlanStatusHistory.objects.filter(plan_version=version).values_list("status", flat=True)
    ) == [PlanStatus.WORK_IN_PROGRESS]
