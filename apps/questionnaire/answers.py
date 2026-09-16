"""
Answer shapes, option sources and validation (AD-5, Phase 4.2).

`question_answers.answer_json` is JSON, which means the database enforces nothing
about it. Everything that would normally be a column type lives here instead,
keyed by the question's `answer_type`:

    SINGLE_CHOICE  {"value": "<code>"}            + optional {"detail": [<code>, ...]}
    MULTI_CHOICE   {"value": ["<code>", ...]}
    TEXT           {"value": "<text>"}
    NUMBER         {"value": <number>}
    DATE           {"value": "YYYY-MM-DD"}
    SUBFORM        {"rows": [{<field>: <value>, ...}, ...]}

Choice answers store option CODES, never labels or option ids, so a reworded or
retired option cannot change what an old answer meant.

`detail` on a SINGLE_CHOICE exists for the two BIA questions that pair a Yes/No
with a lookup-catalogue pick ("Does this service need corporate function
support?" — and if so, which). It is only accepted when the question has a
`lookup_type` as well as its own options, and only alongside a "YES".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apps.lookups.models import LookupCategory, LookupValue
from apps.questionnaire.models import AnswerType, Question


class AnswerValidationError(ValueError):
    """The answer does not fit the question. Surfaces as a 400 with `field_errors`."""

    def __init__(self, message: str, code: str = "invalid_answer"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Option:
    code: str
    label: str


#: Prefixes that keep lookup-sourced codes distinct from question_options codes.
LOOKUP_CATEGORY_PREFIX = "LC:"
LOOKUP_VALUE_PREFIX = "LV:"


def lookup_options(lookup_type: str) -> list[Option]:
    """Options for a lookup-sourced question.

    The legacy catalogue is shaped two ways and this hides that from the rest of
    the application. For "Primary sites" there is one category, named after the
    type, whose child values are the sites — so the values are the options. For
    Vendor, Subcontractor and Corporate function the *categories* are the named
    entities (CBRE, Citrix, Admin...) and their child values are the services
    each provides — so the categories are the options.
    """
    categories = list(
        LookupCategory.objects.filter(category_type=lookup_type).order_by("category_name")
    )
    if len(categories) == 1 and categories[0].category_name.lower() == lookup_type.lower():
        return [
            Option(f"{LOOKUP_VALUE_PREFIX}{value.subcategory_id}", value.subcategory_name)
            for value in LookupValue.objects.filter(category=categories[0]).order_by(
                "subcategory_name"
            )
        ]
    return [
        Option(f"{LOOKUP_CATEGORY_PREFIX}{category.category_id}", category.category_name)
        for category in categories
    ]


def explicit_options(question: Question) -> list[Option]:
    return [
        Option(option.option_code, option.option_label)
        for option in question.options.all()
        if option.active_flag
    ]


def options_for(question: Question) -> list[Option]:
    """The choices offered for a question's primary `value`."""
    if question.options.exists() or not question.lookup_type:
        return explicit_options(question)
    return lookup_options(question.lookup_type)


def detail_options_for(question: Question) -> list[Option]:
    """The secondary lookup pick, for questions that have both. Empty otherwise."""
    if question.lookup_type and question.options.exists():
        return lookup_options(question.lookup_type)
    return []


#: Field definitions for SUBFORM questions, keyed by `lookup_type`. Exposed to
#: the client so the renderer stays generic. The subcontractor fields are an
#: assumption — the legacy export names the section but not its columns. The
#: corporate-function and vendor forms are the dependency lists: who, and what
#: service they provide.
SUBFORM_SCHEMAS: dict[str, list[dict]] = {
    "Corporate function": [
        {
            "name": "corporate_function",
            "label": "Corporate function",
            "type": "lookup",
            "required": True,
        },
        {"name": "service", "label": "Service provided", "type": "text", "required": True},
    ],
    "Vendor": [
        {"name": "vendor", "label": "Vendor", "type": "lookup", "required": True},
        {"name": "service", "label": "Service provided", "type": "text", "required": True},
    ],
    "Subcontractor": [
        {"name": "subcontractor", "label": "Subcontractor", "type": "lookup", "required": True},
        {"name": "service", "label": "Service provided", "type": "text", "required": True},
        {"name": "contact_name", "label": "Contact name", "type": "text", "required": False},
        {"name": "contact_email", "label": "Contact email", "type": "text", "required": False},
        {"name": "contact_phone", "label": "Contact phone", "type": "text", "required": False},
        {
            "name": "criticality",
            "label": "Criticality",
            "type": "choice",
            "required": True,
            "options": [
                {"code": "HIGH", "label": "High"},
                {"code": "MEDIUM", "label": "Medium"},
                {"code": "LOW", "label": "Low"},
            ],
        },
    ],
}


def subform_schema(question: Question) -> list[dict]:
    return SUBFORM_SCHEMAS.get(question.lookup_type, [])


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

MAX_TEXT_LENGTH = 4000


def _require_mapping(answer: object) -> Mapping:
    if not isinstance(answer, Mapping):
        raise AnswerValidationError("An answer must be an object.", "invalid_shape")
    return answer


def validate_answer(question: Question, answer: object) -> dict:
    """Return a normalised answer or raise `AnswerValidationError`.

    Normalisation matters as much as rejection: "  8 " and 8 must save the same
    way, or completion maths and the document export see two different answers.
    """
    data = _require_mapping(answer)
    answer_type = question.answer_type

    if answer_type == AnswerType.SINGLE_CHOICE:
        return _validate_single_choice(question, data)
    if answer_type == AnswerType.MULTI_CHOICE:
        return _validate_multi_choice(question, data)
    if answer_type == AnswerType.TEXT:
        return _validate_text(data)
    if answer_type == AnswerType.NUMBER:
        return _validate_number(data)
    if answer_type == AnswerType.DATE:
        return _validate_date(data)
    if answer_type == AnswerType.SUBFORM:
        return _validate_subform(question, data)
    raise AnswerValidationError(f"Unsupported answer type {answer_type}.", "unsupported_type")


def _codes(options: list[Option]) -> set[str]:
    return {option.code for option in options}


def _validate_single_choice(question: Question, data: Mapping) -> dict:
    value = data.get("value")
    if not isinstance(value, str) or not value:
        raise AnswerValidationError("Choose one option.", "missing_value")
    allowed = _codes(options_for(question))
    if value not in allowed:
        raise AnswerValidationError(
            f"'{value}' is not one of the offered options.", "unknown_option"
        )

    result: dict = {"value": value}
    detail = data.get("detail")
    if detail is not None:
        detail_allowed = _codes(detail_options_for(question))
        if not detail_allowed:
            raise AnswerValidationError("This question has no detail selection.", "no_detail")
        if not isinstance(detail, list) or not all(isinstance(d, str) for d in detail):
            raise AnswerValidationError("Detail must be a list of option codes.", "invalid_detail")
        unknown = [d for d in detail if d not in detail_allowed]
        if unknown:
            raise AnswerValidationError(
                f"Unknown detail option(s): {', '.join(unknown)}.", "unknown_option"
            )
        # A detail pick only means something with a "Yes"; drop it otherwise so a
        # flipped answer does not carry stale selections along.
        if value == "YES" and detail:
            result["detail"] = sorted(set(detail))
    return result


def _validate_multi_choice(question: Question, data: Mapping) -> dict:
    value = data.get("value")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AnswerValidationError("Choose one or more options.", "missing_value")
    allowed = _codes(options_for(question))
    unknown = [v for v in value if v not in allowed]
    if unknown:
        raise AnswerValidationError(f"Unknown option(s): {', '.join(unknown)}.", "unknown_option")
    return {"value": sorted(set(value))}


def _validate_text(data: Mapping) -> dict:
    value = data.get("value")
    if not isinstance(value, str):
        raise AnswerValidationError("Enter some text.", "missing_value")
    value = value.strip()
    if len(value) > MAX_TEXT_LENGTH:
        raise AnswerValidationError(f"Text is limited to {MAX_TEXT_LENGTH} characters.", "too_long")
    return {"value": value}


def _validate_number(data: Mapping) -> dict:
    value = data.get("value")
    if isinstance(value, bool) or value is None or value == "":
        raise AnswerValidationError("Enter a number.", "missing_value")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise AnswerValidationError("Enter a valid number.", "not_a_number") from exc
    if not number.is_finite():
        raise AnswerValidationError("Enter a valid number.", "not_a_number")
    if number < 0:
        raise AnswerValidationError("The number cannot be negative.", "negative")
    # Stored as int when whole, float otherwise — JSON has no Decimal, and
    # "8" and 8.0 must compare equal after a round trip.
    return {"value": int(number) if number == number.to_integral_value() else float(number)}


def _validate_date(data: Mapping) -> dict:
    value = data.get("value")
    if not isinstance(value, str) or not value:
        raise AnswerValidationError("Enter a date.", "missing_value")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise AnswerValidationError("Dates must be YYYY-MM-DD.", "invalid_date") from exc
    return {"value": parsed.isoformat()}


def _validate_subform(question: Question, data: Mapping) -> dict:
    schema = subform_schema(question)
    if not schema:
        raise AnswerValidationError(
            f"No sub-form is defined for '{question.lookup_type}'.", "no_schema"
        )
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise AnswerValidationError("A sub-form answer is a list of rows.", "invalid_shape")

    lookup_codes = _codes(lookup_options(question.lookup_type))
    cleaned = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise AnswerValidationError(f"Row {index} is not an object.", "invalid_shape")
        out = {}
        for field in schema:
            name = field["name"]
            raw = row.get(name)
            if raw in (None, ""):
                if field["required"]:
                    raise AnswerValidationError(
                        f"Row {index}: '{field['label']}' is required.", "missing_field"
                    )
                continue
            if field["type"] == "lookup":
                if raw not in lookup_codes:
                    raise AnswerValidationError(
                        f"Row {index}: unknown {field['label'].lower()} '{raw}'.",
                        "unknown_option",
                    )
                out[name] = raw
            elif field["type"] == "choice":
                allowed = {o["code"] for o in field["options"]}
                if raw not in allowed:
                    raise AnswerValidationError(
                        f"Row {index}: '{raw}' is not a valid {field['label'].lower()}.",
                        "unknown_option",
                    )
                out[name] = raw
            else:
                if not isinstance(raw, str):
                    raise AnswerValidationError(
                        f"Row {index}: '{field['label']}' must be text.", "invalid_field"
                    )
                out[name] = raw.strip()[:MAX_TEXT_LENGTH]
        cleaned.append(out)
    return {"rows": cleaned}


def is_answered(question: Question, answer_json: object) -> bool:
    """Does this answer count toward completion? Empty shapes do not."""
    if not isinstance(answer_json, Mapping):
        return False
    if question.answer_type == AnswerType.SUBFORM:
        rows = answer_json.get("rows")
        return isinstance(rows, list) and len(rows) > 0
    value = answer_json.get("value")
    return value not in (None, "", [])
