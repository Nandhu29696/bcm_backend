"""
Conditional visibility (Phase 4.3) — the server half of one shared rule.

A question with `depends_on_question` is shown, and counted toward section
completion, only when the referenced question's answer satisfies the operator.
The client evaluates the same rule for rendering (`src/features/editor/
visibility.ts`); this module evaluates it for completion. The two must agree, so
the semantics are pinned down here and mirrored there line for line:

  * The dependency's answer is reduced to a set of codes: a SINGLE_CHOICE answer
    `{"value": "YES"}` is `{"YES"}`; a MULTI_CHOICE `{"value": ["A", "B"]}` is
    `{"A", "B"}`. Anything else (unanswered, empty, a number) is the empty set.
  * EQUALS      -> the expected value is in the set.
  * NOT_EQUALS  -> the set is non-empty and the expected value is not in it.
                   An unanswered dependency does NOT satisfy NOT_EQUALS: "if the
                   response to Q1 is not Yes" presumes a response.
  * IN          -> the set intersects the comma-separated expected values.
  * A question whose dependency is itself hidden is hidden. Chains resolve in
    one pass because questions are evaluated in section/display order and a
    dependency always precedes its dependants — the seeder enforces that.
  * Comparison is on the stored option CODE, never on the label (AD-5).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from apps.questionnaire.models import DependencyOperator, Question


def answer_codes(answer_json: object) -> frozenset[str]:
    """The set of option codes an answer represents. Empty when not a choice."""
    if not isinstance(answer_json, Mapping):
        return frozenset()
    value = answer_json.get("value")
    if isinstance(value, str):
        return frozenset({value}) if value else frozenset()
    if isinstance(value, list):
        return frozenset(item for item in value if isinstance(item, str) and item)
    return frozenset()


def rule_satisfied(operator: str, expected: str, codes: frozenset[str]) -> bool:
    if operator == DependencyOperator.EQUALS:
        return expected in codes
    if operator == DependencyOperator.NOT_EQUALS:
        return bool(codes) and expected not in codes
    if operator == DependencyOperator.IN:
        wanted = {part.strip() for part in expected.split(",") if part.strip()}
        return bool(codes & wanted)
    # An unknown operator hides the question rather than showing it: a
    # mis-seeded rule should fail closed, not silently widen the questionnaire.
    return False


def compute_visibility(
    questions: Iterable[Question], answers: Mapping[int, object]
) -> dict[int, bool]:
    """Visibility per question id, given `{question_id: answer_json}`.

    `questions` must be in evaluation order (section, display_order) so that a
    dependency is decided before anything that depends on it.
    """
    visible: dict[int, bool] = {}
    for question in questions:
        parent_id = question.depends_on_question_id
        if parent_id is None:
            visible[question.question_id] = True
            continue
        if not visible.get(parent_id, False):
            visible[question.question_id] = False
            continue
        visible[question.question_id] = rule_satisfied(
            question.depends_on_operator,
            question.depends_on_value,
            answer_codes(answers.get(parent_id)),
        )
    return visible
