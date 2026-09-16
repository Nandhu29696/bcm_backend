"""
The visibility rule (4.3) and answer validation (4.2) — pure functions, no HTTP.
"""

import pytest

from apps.questionnaire.answers import (
    AnswerValidationError,
    is_answered,
    lookup_options,
    options_for,
    validate_answer,
)
from apps.questionnaire.models import DependencyOperator
from apps.questionnaire.visibility import answer_codes, compute_visibility, rule_satisfied

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# Rule semantics — these must match src/features/editor/visibility.ts
# --------------------------------------------------------------------------- #


def test_answer_codes_from_single_and_multi_choice():
    assert answer_codes({"value": "YES"}) == {"YES"}
    assert answer_codes({"value": ["A", "B"]}) == {"A", "B"}


def test_answer_codes_is_empty_for_non_choice_answers():
    assert answer_codes(None) == frozenset()
    assert answer_codes({"value": 8}) == frozenset()
    assert answer_codes({"value": ""}) == frozenset()
    assert answer_codes({"rows": []}) == frozenset()


@pytest.mark.parametrize(
    ("operator", "expected", "codes", "result"),
    [
        (DependencyOperator.EQUALS, "YES", {"YES"}, True),
        (DependencyOperator.EQUALS, "YES", {"NO"}, False),
        (DependencyOperator.EQUALS, "YES", set(), False),
        (DependencyOperator.NOT_EQUALS, "YES", {"NO"}, True),
        (DependencyOperator.NOT_EQUALS, "YES", {"YES"}, False),
        # An unanswered dependency does not satisfy NOT_EQUALS.
        (DependencyOperator.NOT_EQUALS, "YES", set(), False),
        (DependencyOperator.IN, "A, B", {"B"}, True),
        (DependencyOperator.IN, "A,B", {"C"}, False),
        ("BOGUS", "YES", {"YES"}, False),  # fails closed
    ],
)
def test_rule_satisfied(operator, expected, codes, result):
    assert rule_satisfied(operator, expected, frozenset(codes)) is result


def test_visibility_follows_the_real_branches(questions):
    """BASIC-002 needs Q1 = Yes; BASIC-003 needs Q1 = No; both hidden when unanswered."""
    from apps.questionnaire.services import ordered_questions

    q = questions
    ordered = ordered_questions()

    unanswered = compute_visibility(ordered, {})
    assert unanswered[q["BASIC-001"].pk] is True
    assert unanswered[q["BASIC-002"].pk] is False
    assert unanswered[q["BASIC-003"].pk] is False

    yes = compute_visibility(ordered, {q["BASIC-001"].pk: {"value": "YES"}})
    assert yes[q["BASIC-002"].pk] is True
    assert yes[q["BASIC-003"].pk] is False

    no = compute_visibility(ordered, {q["BASIC-001"].pk: {"value": "NO"}})
    assert no[q["BASIC-002"].pk] is False
    assert no[q["BASIC-003"].pk] is True


def test_a_dependant_of_a_hidden_question_is_hidden(questions):
    """Chains: if the parent is hidden, so is the child, whatever its own rule."""
    from apps.questionnaire.services import ordered_questions

    q = questions
    # BIA-004 depends on BIA-003 = YES. Hide BIA-003 by making it depend on
    # BASIC-001 = YES, then leave BASIC-001 unanswered.
    bia3 = q["BIA-003"]
    bia3.depends_on_question = q["BASIC-001"]
    bia3.depends_on_operator = DependencyOperator.EQUALS
    bia3.depends_on_value = "YES"
    bia3.save()

    visible = compute_visibility(ordered_questions(), {q["BIA-003"].pk: {"value": "YES"}})
    assert visible[q["BIA-003"].pk] is False
    assert visible[q["BIA-004"].pk] is False


# --------------------------------------------------------------------------- #
# Option sources
# --------------------------------------------------------------------------- #


@pytest.fixture
def yes_no_with_lookup(questions):
    """A Yes/No question that also offers a catalogue pick as `detail`.

    No seeded question has this shape any more — the dependency questions open
    a sub-form instead — but the answer shape is still supported.
    """
    question = questions["BIA-005"]
    question.lookup_type = "Corporate function"
    question.save(update_fields=["lookup_type"])
    return question


def test_explicit_options_win_over_lookup(yes_no_with_lookup):
    """Yes/No options AND a lookup_type: the primary value is Yes/No."""
    codes = [o.code for o in options_for(yes_no_with_lookup)]
    assert codes == ["YES", "NO"]


def test_lookup_only_question_offers_catalogue_values(questions):
    """BIA-001 has no options rows; its choices come from "Primary sites"."""
    labels = {o.label for o in options_for(questions["BIA-001"])}
    assert labels == {"Client Site", "FSL Owned Site", "Non FSL Owned Site"}
    assert all(o.code.startswith("LV:") for o in options_for(questions["BIA-001"]))


def test_entity_shaped_lookups_offer_the_categories():
    """For Vendor the catalogue's *categories* are the vendors; values are services."""
    options = lookup_options("Vendor")
    labels = {o.label for o in options}
    assert "CBRE" in labels
    assert all(o.code.startswith("LC:") for o in options)


# --------------------------------------------------------------------------- #
# Validation per answer type
# --------------------------------------------------------------------------- #


def test_single_choice_accepts_a_known_code(questions):
    assert validate_answer(questions["BASIC-001"], {"value": "YES"}) == {"value": "YES"}


def test_single_choice_rejects_a_label_or_unknown_code(questions):
    """Codes, never labels (AD-5)."""
    with pytest.raises(AnswerValidationError) as error:
        validate_answer(questions["BASIC-001"], {"value": "Yes"})
    assert error.value.code == "unknown_option"


def test_single_choice_rejects_missing_value(questions):
    with pytest.raises(AnswerValidationError):
        validate_answer(questions["BASIC-001"], {})


def test_detail_pick_is_kept_with_yes_and_validated(yes_no_with_lookup):
    corp = lookup_options("Corporate function")[0].code
    result = validate_answer(yes_no_with_lookup, {"value": "YES", "detail": [corp, corp]})
    assert result == {"value": "YES", "detail": [corp]}


def test_detail_pick_is_dropped_with_no(yes_no_with_lookup):
    """A flipped answer must not carry stale selections along."""
    corp = lookup_options("Corporate function")[0].code
    assert validate_answer(yes_no_with_lookup, {"value": "NO", "detail": [corp]}) == {"value": "NO"}


def test_dependency_subforms_take_who_and_service(questions):
    corp = lookup_options("Corporate function")[0].code
    result = validate_answer(
        questions["BIA-007"], {"rows": [{"corporate_function": corp, "service": " Payroll "}]}
    )
    assert result == {"rows": [{"corporate_function": corp, "service": "Payroll"}]}

    vendor = lookup_options("Vendor")[0].code
    with pytest.raises(AnswerValidationError) as error:
        validate_answer(questions["BIA-008"], {"rows": [{"vendor": vendor}]})
    assert error.value.code == "missing_field"
    with pytest.raises(AnswerValidationError) as error:
        validate_answer(questions["BIA-008"], {"rows": [{"vendor": corp, "service": "x"}]})
    assert error.value.code == "unknown_option"


def test_detail_pick_is_refused_where_there_is_no_lookup(questions):
    with pytest.raises(AnswerValidationError) as error:
        validate_answer(questions["BASIC-001"], {"value": "YES", "detail": ["X"]})
    assert error.value.code == "no_detail"


def test_number_normalises_strings_and_rejects_junk(questions):
    rto = questions["RTO-001"]
    assert validate_answer(rto, {"value": " 8 "}) == {"value": 8}
    assert validate_answer(rto, {"value": "8.5"}) == {"value": 8.5}
    assert validate_answer(rto, {"value": 24}) == {"value": 24}
    for junk in ("eight", "", None, True, "NaN", -1):
        with pytest.raises(AnswerValidationError):
            validate_answer(rto, {"value": junk})


def test_text_is_stripped_and_bounded(questions):
    q = questions["RTO-001"]
    q.answer_type = "TEXT"
    assert validate_answer(q, {"value": "  hello  "}) == {"value": "hello"}
    with pytest.raises(AnswerValidationError):
        validate_answer(q, {"value": "x" * 5000})


def test_date_must_be_iso(questions):
    q = questions["RTO-001"]
    q.answer_type = "DATE"
    assert validate_answer(q, {"value": "2026-09-14"}) == {"value": "2026-09-14"}
    with pytest.raises(AnswerValidationError):
        validate_answer(q, {"value": "14/09/2026"})


def test_subform_rows_are_validated_against_the_schema(questions):
    sub = lookup_options("Subcontractor")[0].code
    result = validate_answer(
        questions["BIA-004"],
        {"rows": [{"subcontractor": sub, "service": " Catering ", "criticality": "HIGH"}]},
    )
    assert result == {
        "rows": [{"subcontractor": sub, "service": "Catering", "criticality": "HIGH"}]
    }


def test_subform_rejects_a_missing_required_field(questions):
    sub = lookup_options("Subcontractor")[0].code
    with pytest.raises(AnswerValidationError) as error:
        validate_answer(questions["BIA-004"], {"rows": [{"subcontractor": sub}]})
    assert error.value.code == "missing_field"


def test_subform_rejects_an_unknown_subcontractor(questions):
    with pytest.raises(AnswerValidationError) as error:
        validate_answer(
            questions["BIA-004"],
            {"rows": [{"subcontractor": "LC:999999", "service": "x", "criticality": "LOW"}]},
        )
    assert error.value.code == "unknown_option"


def test_is_answered_treats_empty_shapes_as_unanswered(questions):
    assert is_answered(questions["BASIC-001"], {"value": "YES"}) is True
    assert is_answered(questions["BASIC-001"], {"value": ""}) is False
    assert is_answered(questions["RTO-001"], {"value": 0}) is True
    assert is_answered(questions["BIA-004"], {"rows": []}) is False
    assert is_answered(questions["BIA-004"], {"rows": [{}]}) is True
    assert is_answered(questions["BASIC-001"], None) is False
