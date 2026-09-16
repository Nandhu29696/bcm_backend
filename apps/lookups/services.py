"""
Catalogue hygiene (PENDING #12).

The two legacy exports overlap: every rating appears once with points and once
without, and a few named entries (a corporate function, a subcontractor
service) were simply keyed in twice. `dedupe_catalogue` collapses each
(type, name) group onto one survivor:

  * the weighted row wins (it is the one scoring reads);
  * otherwise the lowest id, which is the first export's row;
  * child values move to the survivor;
  * answers that reference a retired row by `cat:<id>` are rewritten to the
    survivor, so no stored answer is orphaned;
  * the retired rows are deleted.

`seed_reference_data` calls this last, so re-seeding cannot bring the
duplicates back.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from django.db import transaction

from apps.lookups.models import LookupCategory, LookupValue

logger = logging.getLogger(__name__)

CATEGORY_PREFIX = "cat:"


def duplicate_groups() -> list[list[LookupCategory]]:
    groups: dict[tuple[str, str], list[LookupCategory]] = defaultdict(list)
    for row in LookupCategory.objects.order_by("pk"):
        groups[(row.category_type.strip().lower(), row.category_name.strip().lower())].append(row)
    return [rows for rows in groups.values() if len(rows) > 1]


def choose_survivor(rows: list[LookupCategory]) -> LookupCategory:
    weighted = [r for r in rows if r.points is not None]
    return min(weighted or rows, key=lambda r: r.pk)


def _rewrite_answer_references(mapping: dict[int, int]) -> int:
    """`cat:<retired>` -> `cat:<survivor>` inside stored answers. Returns rows changed."""
    from apps.assessments.models import QuestionAnswer

    retired_codes = {
        f"{CATEGORY_PREFIX}{old}": f"{CATEGORY_PREFIX}{new}" for old, new in mapping.items()
    }
    changed = 0
    for answer in QuestionAnswer.objects.filter(answer_json__isnull=False).iterator():
        payload = answer.answer_json
        if not isinstance(payload, dict):
            continue
        touched = False
        value = payload.get("value")
        if isinstance(value, str) and value in retired_codes:
            payload["value"] = retired_codes[value]
            touched = True
        elif isinstance(value, list):
            rewritten = [retired_codes.get(v, v) if isinstance(v, str) else v for v in value]
            if rewritten != value:
                payload["value"] = rewritten
                touched = True
        detail = payload.get("detail")
        if isinstance(detail, list):
            rewritten = [retired_codes.get(v, v) if isinstance(v, str) else v for v in detail]
            if rewritten != detail:
                payload["detail"] = rewritten
                touched = True
        if touched:
            answer.answer_json = payload
            answer.save(update_fields=["answer_json"])
            changed += 1
    return changed


@transaction.atomic
def dedupe_catalogue() -> dict:
    """Collapse duplicate catalogue rows. Returns what was done."""
    mapping: dict[int, int] = {}
    retired: list[LookupCategory] = []
    for rows in duplicate_groups():
        survivor = choose_survivor(rows)
        for row in rows:
            if row.pk != survivor.pk:
                mapping[row.pk] = survivor.pk
                retired.append(row)

    if not mapping:
        return {"groups": 0, "retired": 0, "values_moved": 0, "answers_rewritten": 0}

    values_moved = 0
    for row in retired:
        values_moved += LookupValue.objects.filter(category=row).update(category_id=mapping[row.pk])
    answers_rewritten = _rewrite_answer_references(mapping)
    retired_ids = [r.pk for r in retired]
    LookupCategory.objects.filter(pk__in=retired_ids).delete()

    result = {
        "groups": len({v for v in mapping.values()}),
        "retired": len(retired_ids),
        "values_moved": values_moved,
        "answers_rewritten": answers_rewritten,
        "mapping": mapping,
    }
    logger.info("Catalogue dedupe: %s", result)
    return result
