"""
The plan editor's data layer (Phase 4).

    build_questionnaire   the whole tree with answers merged, one round trip (4.1)
    save_answer           validate, upsert, recompute completion             (4.2, 4.6)
    clear_answer
    recompute_section_statuses                                                (4.6)

Section completion is DERIVED, never hand-set: a section is Completed when every
*visible* required question has a valid answer, In Progress when any question is
answered, otherwise Not Started. Hidden branches do not count — a "No" to Q1
hides Q2, and Q2 must not then hold the section at 80% forever. A question that
requires evidence for its answer (the penalty clause behind a "Yes") is not
answered until the file is there. It is recomputed on every save and stored in
`section_statuses` so the estate rollup and the document export read a column
rather than re-running the questionnaire.

One answer per (version, question, context). The unique key on
`question_answers` also includes the respondent, so the *schema* would permit a
second row when a second person answers the same question; this module does
not. The latest writer becomes the respondent. Two rows for one question would
mean the editor shows one and the export the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Prefetch

from apps.assessments.models import (
    AnswerContext,
    QuestionAnswer,
    QuestionComment,
    SectionStatus,
    SectionStatusValue,
)
from apps.plans.models import PlanVersion
from apps.questionnaire.answers import (
    AnswerValidationError,
    detail_options_for,
    is_answered,
    options_for,
    subform_schema,
    validate_answer,
)
from apps.questionnaire.evidence import evidence_by_question, evidence_payload
from apps.questionnaire.models import AnswerType, Question, QuestionOption, Section
from apps.questionnaire.visibility import compute_visibility


class PlanNotEditable(Exception):
    """The version is Approved or Exempted (AD-4). Surfaces as 409."""


def ordered_questions() -> list[Question]:
    """Every live question in evaluation order, with options prefetched."""
    return list(
        Question.objects.filter(section__active_flag=True)
        .select_related("section", "depends_on_question")
        .prefetch_related(
            Prefetch("options", queryset=QuestionOption.objects.filter(active_flag=True))
        )
        .order_by("section__display_order", "section_id", "display_order", "question_id")
    )


def answers_by_question(version: PlanVersion, context: str) -> dict[int, QuestionAnswer]:
    return {
        answer.question_id: answer
        for answer in QuestionAnswer.objects.filter(
            plan_version=version, answer_context=context
        ).select_related("respondent_user")
    }


@dataclass
class SectionSummary:
    section: Section
    status: str
    required_visible: int = 0
    required_answered: int = 0
    answered: int = 0
    questions: list[Question] = field(default_factory=list)

    @property
    def percent(self) -> int:
        if self.required_visible == 0:
            return 100 if self.status == SectionStatusValue.COMPLETED else 0
        return round(100 * self.required_answered / self.required_visible)


def answer_is_complete(
    question: Question, answer: QuestionAnswer | None, evidenced: set[int]
) -> bool:
    """Answered, and — where the answer calls for required evidence — backed by a file."""
    if answer is None or not is_answered(question, answer.answer_json):
        return False
    if question.evidence_required and question.wants_evidence(answer.answer_json):
        return question.question_id in evidenced
    return True


def summarise_sections(
    questions: list[Question],
    answers: dict[int, QuestionAnswer],
    visibility: dict[int, bool],
    evidenced: set[int] | None = None,
) -> list[SectionSummary]:
    """Completion per section from visibility, answers and evidence — the 4.6 rule.

    `evidenced` is the set of question ids that have at least one evidence file.
    """
    evidenced = evidenced or set()
    by_section: dict[int, SectionSummary] = {}
    for question in questions:
        summary = by_section.setdefault(
            question.section_id,
            SectionSummary(section=question.section, status=SectionStatusValue.NOT_STARTED),
        )
        summary.questions.append(question)
        if not visibility.get(question.question_id, False):
            continue
        answer = answers.get(question.question_id)
        answered = answer_is_complete(question, answer, evidenced)
        if answered:
            summary.answered += 1
        if question.required_flag:
            summary.required_visible += 1
            if answered:
                summary.required_answered += 1

    for summary in by_section.values():
        if summary.required_visible > 0 and summary.required_answered == summary.required_visible:
            summary.status = SectionStatusValue.COMPLETED
        elif summary.required_visible == 0 and summary.answered > 0:
            # A section with no required questions is complete once touched.
            summary.status = SectionStatusValue.COMPLETED
        elif summary.answered > 0:
            summary.status = SectionStatusValue.IN_PROGRESS
        else:
            summary.status = SectionStatusValue.NOT_STARTED
    return list(by_section.values())


def build_questionnaire(version: PlanVersion, context: str = AnswerContext.BCP) -> dict:
    """The full tree for the editor, answers merged, in one round trip (4.1)."""
    questions = ordered_questions()
    answers = answers_by_question(version, context)
    visibility = compute_visibility(questions, {qid: a.answer_json for qid, a in answers.items()})
    evidence = evidence_by_question(version)
    sections = summarise_sections(questions, answers, visibility, set(evidence))
    comment_counts = _comment_counts(version)

    return {
        "plan_version_id": version.pk,
        "context": context,
        "editable": version.is_editable,
        "status": version.status,
        "contexts": [choice.value for choice in AnswerContext],
        "sections": [
            {
                "section_id": s.section.section_id,
                "section_name": s.section.section_name,
                "group": s.section.group,
                "status": s.status,
                "percent": s.percent,
                "required_visible": s.required_visible,
                "required_answered": s.required_answered,
                "questions": [
                    _question_payload(
                        q,
                        answers.get(q.question_id),
                        visibility,
                        comment_counts,
                        evidence.get(q.question_id, []),
                    )
                    for q in s.questions
                ],
            }
            for s in sections
        ],
    }


def _question_payload(
    question: Question,
    answer: QuestionAnswer | None,
    visibility: dict[int, bool],
    comment_counts: dict[int, int],
    evidence_files: list,
) -> dict:
    payload = {
        "question_id": question.question_id,
        "question_code": question.question_code,
        "question_text": question.question_text,
        "question_description": question.question_description,
        "answer_type": question.answer_type,
        "required": question.required_flag,
        "visible": visibility.get(question.question_id, False),
        "depends_on": (
            {
                "question_id": question.depends_on_question_id,
                "operator": question.depends_on_operator,
                "value": question.depends_on_value,
            }
            if question.depends_on_question_id
            else None
        ),
        # A SUBFORM's options are the catalogue entries its lookup column offers.
        "options": (
            [o.__dict__ for o in options_for(question)]
            if question.answer_type
            in (AnswerType.SINGLE_CHOICE, AnswerType.MULTI_CHOICE, AnswerType.SUBFORM)
            else []
        ),
        "detail_options": [o.__dict__ for o in detail_options_for(question)],
        "subform_schema": (
            subform_schema(question) if question.answer_type == AnswerType.SUBFORM else []
        ),
        "answer": answer.answer_json if answer else None,
        "answered_by": answer.respondent_user.display_name if answer else None,
        "answered_at": answer.updated_at if answer else None,
        "comment_count": comment_counts.get(question.question_id, 0),
        "evidence": evidence_payload(question, evidence_files),
    }
    return payload


def _comment_counts(version: PlanVersion) -> dict[int, int]:
    from django.db.models import Count

    rows = (
        QuestionComment.objects.filter(plan_version=version, question__isnull=False)
        .values("question_id")
        .annotate(total=Count("question_comment_id"))
    )
    return {row["question_id"]: row["total"] for row in rows}


@transaction.atomic
def recompute_section_statuses(
    version: PlanVersion, *, actor=None, context: str = AnswerContext.BCP
) -> list[SectionSummary]:
    """Derive and persist every section's status for the version (4.6).

    Completion is computed over the BCP context — the plan proper. The other
    contexts are supplementary views of the same questions and do not gate
    submission.
    """
    questions = ordered_questions()
    answers = answers_by_question(version, context)
    visibility = compute_visibility(questions, {qid: a.answer_json for qid, a in answers.items()})
    summaries = summarise_sections(
        questions, answers, visibility, set(evidence_by_question(version))
    )

    for summary in summaries:
        SectionStatus.objects.update_or_create(
            plan_version=version,
            section=summary.section,
            defaults={"status": summary.status, "changed_by": actor},
        )
    return summaries


def _assert_editable(version: PlanVersion) -> None:
    if not version.is_editable:
        raise PlanNotEditable(
            f"Version {version.version_number} is {version.status} and cannot be changed. "
            "Create a new version to make changes."
        )


@transaction.atomic
def save_answer(
    *,
    version: PlanVersion,
    question: Question,
    answer: object,
    actor,
    context: str = AnswerContext.BCP,
    comments: str | None = None,
) -> tuple[QuestionAnswer, list[SectionSummary]]:
    """Validate and upsert one answer, then recompute completion (4.2, 4.6).

    Raises `AnswerValidationError` for a bad shape and `PlanNotEditable` for an
    approved version. Both are caller-facing, never 500s.
    """
    _assert_editable(version)
    normalised = validate_answer(question, answer)

    from apps.plans.workflow import mark_in_progress

    version = mark_in_progress(version, actor=actor)

    # Serialise saves per version. Two writes for the same question in flight at
    # once — a user clicking No then Yes before the first reply lands — would
    # both find no existing row, both insert, and the second would hit the
    # unique key with an IntegrityError. Locking the version row makes the second
    # wait, see the first's row, and update it instead.
    PlanVersion.objects.select_for_update().get(pk=version.pk)

    existing = (
        QuestionAnswer.all_objects.filter(
            plan_version=version, question=question, answer_context=context
        )
        .order_by("-updated_at")
        .first()
    )
    if existing is None:
        row = QuestionAnswer.objects.create(
            plan_version=version,
            question=question,
            respondent_user=actor,
            answer_context=context,
            answer_json=normalised,
            comments=comments or "",
            created_by=actor,
            updated_by=actor,
        )
    else:
        row = existing
        row.answer_json = normalised
        row.respondent_user = actor
        row.updated_by = actor
        row.active_flag = True
        if comments is not None:
            row.comments = comments
        row.save()

    version.updated_by = actor
    version.save(update_fields=["updated_by", "updated_at"])
    return row, recompute_section_statuses(version, actor=actor, context=context)


@transaction.atomic
def clear_answer(
    *, version: PlanVersion, question: Question, actor, context: str = AnswerContext.BCP
) -> list[SectionSummary]:
    _assert_editable(version)
    PlanVersion.objects.select_for_update().get(pk=version.pk)
    QuestionAnswer.objects.filter(
        plan_version=version, question=question, answer_context=context
    ).update(active_flag=False, updated_by=actor)
    return recompute_section_statuses(version, actor=actor, context=context)


__all__ = [
    "AnswerValidationError",
    "PlanNotEditable",
    "build_questionnaire",
    "clear_answer",
    "recompute_section_statuses",
    "save_answer",
]
