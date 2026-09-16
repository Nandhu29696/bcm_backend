"""
Seed the real question bank from the legacy export.

Source: `tables/static_BCM_Sections.csv` (6 sections) and
`tables/static_BCM_Sections_Questions.csv` (15 questions).

Two things about that second file are easy to get wrong:
  * the question text is in `Question_Description`, not `Title`;
  * `Title` and `Question` both hold the display order, and `Section_ID` the section.

The legacy export carries no answer types, no option lists and no dependency
information, so ANSWER_SPEC below supplies them. It is keyed by legacy question ID
and is the authoritative mapping — the CSV supplies wording, this supplies
behaviour.

Idempotent: re-running updates in place and rewires dependencies.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.management.commands._csvutil import read_legacy_csv, to_int
from apps.questionnaire.models import (
    AnswerType,
    DependencyOperator,
    Question,
    QuestionOption,
    Section,
)

YES_NO = [("YES", "Yes"), ("NO", "No")]

# Tab order in the editor, by legacy section id. The legacy export orders the
# sections by id (Basic, RTO, MBCO, RPO, BIA, MAO); the editor shows the
# contractual numbers together and BIA last, because BIA is answered in its own
# part of the plan rather than alongside the questionnaire tabs.
SECTION_ORDER: dict[int, int] = {
    1: 1,  # Basic Questions
    6: 2,  # MAO
    2: 3,  # RTO
    3: 4,  # MBCO
    4: 5,  # RPO
    5: 6,  # BIA
}

# legacy question id -> behaviour.
#   code            : stable question code exposed by the API
#   answer_type     : drives the dynamic renderer
#   options         : explicit option list (omit when lookup_type is set)
#   lookup_type     : source options from the lookups catalogue instead
#   required        : counts toward section completion when visible
#   depends_on      : (legacy question id, operator, value) - conditional visibility
#   evidence        : offer a supporting-file upload. {"when": <option code>} limits it
#                     to that answer; {"required": True} makes the file part of
#                     what counts as answered.
ANSWER_SPEC: dict[int, dict] = {
    # --- Basic Questions -----------------------------------------------------
    1: {
        "code": "BASIC-001",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
    },
    5: {
        "code": "BASIC-002",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": False,
        "depends_on": (1, DependencyOperator.EQUALS, "YES"),
    },
    7: {
        "code": "BASIC-003",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": False,
        "depends_on": (1, DependencyOperator.EQUALS, "NO"),
    },
    8: {
        "code": "BASIC-004",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
        # A penalty clause is a claim worth backing: the contract excerpt.
        "evidence": {"when": "YES", "required": True},
    },
    # --- RTO / MBCO / MAO: contractual numbers, in hours or percent ----------
    # Each offers the contract excerpt that states the number.
    2: {"code": "RTO-001", "answer_type": AnswerType.NUMBER, "required": True, "evidence": {}},
    3: {"code": "MBCO-001", "answer_type": AnswerType.NUMBER, "required": True, "evidence": {}},
    15: {"code": "MAO-001", "answer_type": AnswerType.NUMBER, "required": True, "evidence": {}},
    # --- RPO ------------------------------------------------------------------
    4: {
        "code": "RPO-001",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
    },
    6: {
        "code": "RPO-002",
        "answer_type": AnswerType.NUMBER,
        "required": False,
        "depends_on": (4, DependencyOperator.EQUALS, "YES"),
        "evidence": {},
    },
    # --- BIA ------------------------------------------------------------------
    11: {
        "code": "BIA-001",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "lookup_type": "Primary sites",
        "required": True,
    },
    12: {
        "code": "BIA-002",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
    },
    13: {
        "code": "BIA-003",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
    },
    14: {
        "code": "BIA-004",
        "answer_type": AnswerType.SUBFORM,
        "required": False,
        "lookup_type": "Subcontractor",
        "depends_on": (13, DependencyOperator.EQUALS, "YES"),
    },
    # The internal / external dependency questions. Each Yes opens a sub-form
    # (BIA-007 / BIA-008 below) naming the functions or vendors and the service
    # each provides, the same way BIA-004 does for subcontractors.
    9: {
        "code": "BIA-005",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
    },
    10: {
        "code": "BIA-006",
        "answer_type": AnswerType.SINGLE_CHOICE,
        "options": YES_NO,
        "required": True,
    },
}

# Questions that are not in the legacy export, keyed by question code. They
# live in a legacy section and depend on a legacy question (by legacy id), so
# they are seeded after ANSWER_SPEC and wired in the same pass.
#   section : legacy section id
ADDED_QUESTIONS: dict[str, dict] = {
    "BIA-007": {
        "section": 5,
        "text": "Which corporate functions, and what service does each provide?",
        "answer_type": AnswerType.SUBFORM,
        "lookup_type": "Corporate function",
        "required": False,
        "depends_on": (9, DependencyOperator.EQUALS, "YES"),
        "display_order": 51,
    },
    "BIA-008": {
        "section": 5,
        "text": "Which vendors, and what service does each provide?",
        "answer_type": AnswerType.SUBFORM,
        "lookup_type": "Vendor",
        "required": False,
        "depends_on": (10, DependencyOperator.EQUALS, "YES"),
        "display_order": 61,
    },
}


def _evidence_fields(spec: dict) -> dict:
    evidence = spec.get("evidence")
    if evidence is None:
        return {"evidence_flag": False, "evidence_when_value": "", "evidence_required": False}
    return {
        "evidence_flag": True,
        "evidence_when_value": evidence.get("when", ""),
        "evidence_required": bool(evidence.get("required", False)),
    }


class Command(BaseCommand):
    help = "Seed the 6 sections, the 15 legacy questions and the added ones. Safe to re-run."

    @transaction.atomic
    def handle(self, *args, **options):
        sections = self._seed_sections()
        questions = self._seed_questions(sections)
        self._wire_dependencies(questions)
        self._report()

    def _seed_sections(self) -> dict[int, Section]:
        sections: dict[int, Section] = {}
        for row in read_legacy_csv("static_BCM_Sections.csv"):
            legacy_id = to_int(row.get("ID"))
            if legacy_id is None:
                continue
            section, _ = Section.all_objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "section_name": row.get("Title", ""),
                    "display_order": SECTION_ORDER.get(legacy_id, 10 + legacy_id),
                    "active_flag": True,
                },
            )
            sections[legacy_id] = section
        self.stdout.write(f"  sections: {len(sections)}")
        return sections

    def _seed_questions(self, sections: dict[int, Section]) -> dict[int, Question]:
        questions: dict[int, Question] = {}
        unspecified = []

        for row in read_legacy_csv("static_BCM_Sections_Questions.csv"):
            legacy_id = to_int(row.get("ID"))
            section = sections.get(to_int(row.get("Section_ID")))
            if legacy_id is None or section is None:
                continue

            spec = ANSWER_SPEC.get(legacy_id)
            if spec is None:
                # A question the legacy export has but ANSWER_SPEC does not cover.
                # Default to free text rather than guessing a choice list.
                unspecified.append(legacy_id)
                spec = {
                    "code": f"Q-{legacy_id:03d}",
                    "answer_type": AnswerType.TEXT,
                    "required": False,
                }

            question, _ = Question.all_objects.update_or_create(
                legacy_id=legacy_id,
                defaults={
                    "section": section,
                    "question_code": spec["code"],
                    # The real wording lives in Question_Description.
                    "question_text": row.get("Question_Description", ""),
                    "answer_type": spec["answer_type"],
                    "required_flag": spec.get("required", False),
                    "display_order": to_int(row.get("Question")) or 0,
                    "lookup_type": spec.get("lookup_type", ""),
                    **_evidence_fields(spec),
                    "active_flag": True,
                },
            )
            questions[legacy_id] = question
            self._seed_options(question, spec.get("options", []))

        for code, spec in ADDED_QUESTIONS.items():
            section = sections.get(spec["section"])
            if section is None:
                continue
            question, _ = Question.all_objects.update_or_create(
                question_code=code,
                legacy_id=None,
                defaults={
                    "section": section,
                    "question_text": spec["text"],
                    "answer_type": spec["answer_type"],
                    "required_flag": spec.get("required", False),
                    "display_order": spec["display_order"],
                    "lookup_type": spec.get("lookup_type", ""),
                    **_evidence_fields(spec),
                    "active_flag": True,
                },
            )
            questions[code] = question

        if unspecified:
            self.stdout.write(
                self.style.WARNING(
                    f"  no ANSWER_SPEC for legacy question ids {unspecified} - defaulted to TEXT"
                )
            )
        self.stdout.write(f"  questions: {len(questions)}")
        return questions

    def _seed_options(self, question: Question, options: list[tuple[str, str]]):
        for order, (code, label) in enumerate(options, start=1):
            QuestionOption.objects.update_or_create(
                question=question,
                option_code=code,
                defaults={
                    "option_label": label,
                    "display_order": order,
                    "active_flag": True,
                },
            )

    def _wire_dependencies(self, questions: dict[int, Question]):
        """Second pass — a question can depend on one created later."""
        wired = 0
        for key, spec in {**ANSWER_SPEC, **ADDED_QUESTIONS}.items():
            dependency = spec.get("depends_on")
            question = questions.get(key)
            if not dependency or question is None:
                continue
            parent_legacy_id, operator, value = dependency
            parent = questions.get(parent_legacy_id)
            if parent is None:
                self.stdout.write(
                    self.style.WARNING(f"  question {key} depends on missing {parent_legacy_id}")
                )
                continue
            question.depends_on_question = parent
            question.depends_on_operator = operator
            question.depends_on_value = value
            question.save(
                update_fields=[
                    "depends_on_question",
                    "depends_on_operator",
                    "depends_on_value",
                ]
            )
            wired += 1
        self.stdout.write(f"  conditional questions wired: {wired}")

    def _report(self):
        self.stdout.write("")
        for section in Section.objects.order_by("display_order"):
            self.stdout.write(f"  [{section.section_name}]")
            for question in section.questions.order_by("display_order"):
                flags = []
                if question.required_flag:
                    flags.append("required")
                if question.is_conditional:
                    flags.append(
                        f"if {question.depends_on_question.question_code}"
                        f" {question.depends_on_operator} {question.depends_on_value}"
                    )
                if question.lookup_type:
                    flags.append(f"lookup={question.lookup_type}")
                if question.evidence_flag:
                    flag = "evidence required" if question.evidence_required else "evidence"
                    if question.evidence_when_value:
                        flag += f" when {question.evidence_when_value}"
                    flags.append(flag)
                suffix = f"  ({', '.join(flags)})" if flags else ""
                text = question.question_text
                if len(text) > 62:
                    text = text[:62] + "..."
                self.stdout.write(
                    f"    {question.question_code:<11} {question.answer_type:<14} {text}{suffix}"
                )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Questionnaire seeded."))
