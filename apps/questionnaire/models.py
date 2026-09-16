"""
The question bank.

sections -> questions -> question_options is the real questionnaire (as opposed to
the `lookups` catalogue, which the table names make confusingly similar).

The live question set is 6 sections / 15 questions and it BRANCHES — questions
reference each other ("If Yes to Q1...", "If the response to Q1 is 'No' then...").
S8 adds the dependency columns so branching is data rather than frontend code:
adding or rewiring a question is a data change with no deploy.
"""

from django.db import models

from apps.core.models import SoftDeleteModel


class AnswerType(models.TextChoices):
    SINGLE_CHOICE = "SINGLE_CHOICE", "Single choice"
    MULTI_CHOICE = "MULTI_CHOICE", "Multiple choice"
    TEXT = "TEXT", "Free text"
    NUMBER = "NUMBER", "Number"
    DATE = "DATE", "Date"
    # Renders a structured child form rather than a scalar answer. Used by BIA
    # "If Yes to the above question Update below fields".
    SUBFORM = "SUBFORM", "Sub-form"


class DependencyOperator(models.TextChoices):
    EQUALS = "EQUALS", "Equals"
    NOT_EQUALS = "NOT_EQUALS", "Not equals"
    IN = "IN", "In"


#: Legacy id of the BIA section. Its questions are answered in the plan's BIA
#: part, not on the questionnaire tabs — see `Section.group`.
BIA_SECTION_LEGACY_ID = 5


class SectionGroup(models.TextChoices):
    QUESTIONNAIRE = "questionnaire", "Questionnaire"
    BIA = "bia", "BIA"


class Section(SoftDeleteModel):
    """A tab in the plan editor. Live sections: Basic Questions, MAO, RTO, MBCO,
    RPO on the questionnaire tabs; BIA in the plan's BIA part."""

    section_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    subcategory = models.ForeignKey(
        "lookups.LookupValue",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="subcategory_id",
        related_name="sections",
    )
    section_name = models.CharField(max_length=200)
    answer_status = models.CharField(max_length=50, blank=True)
    display_order = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta(SoftDeleteModel.Meta):
        db_table = "sections"
        ordering = ["display_order", "section_id"]

    def __str__(self):
        return self.section_name

    @property
    def group(self) -> str:
        """Where the editor shows this section (see `SectionGroup`)."""
        if self.legacy_id == BIA_SECTION_LEGACY_ID or self.section_name.strip().upper() == "BIA":
            return SectionGroup.BIA
        return SectionGroup.QUESTIONNAIRE


class Question(SoftDeleteModel):
    question_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    section = models.ForeignKey(
        Section,
        on_delete=models.CASCADE,
        db_column="section_id",
        related_name="questions",
    )
    question_code = models.CharField(max_length=100, blank=True)
    question_text = models.TextField()
    question_description = models.TextField(blank=True)
    answer_type = models.CharField(
        max_length=50, choices=AnswerType.choices, default=AnswerType.TEXT
    )
    required_flag = models.BooleanField(default=False)
    display_order = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    # --- S8: conditional visibility -------------------------------------------
    # When depends_on_question is set, this question is only shown (and only
    # counted toward section completion) if the referenced question's answer
    # satisfies the operator. A hidden branch must never block a section reaching
    # 100% — see Phase 4.6.
    depends_on_question = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="depends_on_question_id",
        related_name="dependent_questions",
    )
    depends_on_operator = models.CharField(
        max_length=20, choices=DependencyOperator.choices, blank=True
    )
    # Comma-separated when the operator is IN.
    depends_on_value = models.CharField(max_length=255, blank=True)

    # --- S8: lookup-sourced options -------------------------------------------
    # When set, options come from the lookups catalogue with this category_type
    # (e.g. "Primary sites", "Vendor") instead of from question_options.
    lookup_type = models.CharField(max_length=50, blank=True)

    # --- Evidence -----------------------------------------------------------
    # A question can ask for a supporting file (a contract excerpt for the RTO,
    # the penalty clause). `evidence_flag` offers the upload; `evidence_when_value`
    # limits it to one answer (an option code such as YES; blank = always);
    # `evidence_required` makes the file part of what counts as answered, so a
    # section cannot complete without it. Data, not code, like the branching.
    evidence_flag = models.BooleanField(default=False)
    evidence_when_value = models.CharField(max_length=80, blank=True)
    evidence_required = models.BooleanField(default=False)

    class Meta(SoftDeleteModel.Meta):
        db_table = "questions"
        ordering = ["section", "display_order", "question_id"]
        indexes = [models.Index(fields=["section"], name="idx_question_section")]

    def __str__(self):
        return self.question_code or f"Q{self.question_id}"

    @property
    def is_conditional(self) -> bool:
        return self.depends_on_question_id is not None

    @property
    def sources_options_from_lookup(self) -> bool:
        return bool(self.lookup_type)

    def wants_evidence(self, answer_json: object) -> bool:
        """Does this answer call for a supporting file?"""
        if not self.evidence_flag:
            return False
        if not self.evidence_when_value:
            return True
        return (
            isinstance(answer_json, dict) and answer_json.get("value") == self.evidence_when_value
        )


class QuestionOption(models.Model):
    """A selectable option.

    Answers store `option_code`, never a FK to this row — options get deactivated
    and reworded over time, and a historical answer must keep its meaning (AD-5).
    """

    option_id = models.BigAutoField(primary_key=True)
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        db_column="question_id",
        related_name="options",
    )
    option_code = models.CharField(max_length=80)
    option_label = models.CharField(max_length=255)
    display_order = models.IntegerField(null=True, blank=True)
    active_flag = models.BooleanField(default=True)

    class Meta:
        db_table = "question_options"
        ordering = ["question", "display_order", "option_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["question", "option_code"], name="uq_question_option_code"
            )
        ]

    def __str__(self):
        return f"{self.option_code}: {self.option_label}"
