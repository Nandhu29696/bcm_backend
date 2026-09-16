"""Duplicate catalogue rows collapse onto one survivor without orphaning anything."""

import pytest
from django.core.management import call_command

from apps.lookups.models import LookupCategory, LookupValue
from apps.lookups.services import dedupe_catalogue, duplicate_groups

pytestmark = pytest.mark.django_db


@pytest.fixture
def duplicated():
    weighted = LookupCategory.objects.create(
        category_type="Likelihood", category_name="Rare (1)", points=1
    )
    unweighted = LookupCategory.objects.create(category_type="Likelihood", category_name="Rare (1)")
    first = LookupCategory.objects.create(category_type="Subcontractor", category_name="Guarding")
    second = LookupCategory.objects.create(category_type="Subcontractor", category_name="Guarding ")
    LookupValue.objects.create(category=second, subcategory_name="Night shift")
    LookupCategory.objects.create(category_type="Subcontractor", category_name="Cleaning")
    return {"weighted": weighted, "unweighted": unweighted, "first": first, "second": second}


def test_weighted_row_survives_and_values_move(duplicated):
    assert len(duplicate_groups()) == 2
    result = dedupe_catalogue()
    assert result["retired"] == 2 and result["groups"] == 2 and result["values_moved"] == 1
    assert LookupCategory.objects.filter(pk=duplicated["unweighted"].pk).exists() is False
    assert LookupCategory.objects.filter(pk=duplicated["second"].pk).exists() is False
    assert (
        LookupValue.objects.get(subcategory_name="Night shift").category_id
        == duplicated["first"].pk
    )
    assert LookupCategory.objects.filter(category_type="Subcontractor").count() == 2
    assert duplicate_groups() == []
    assert dedupe_catalogue()["retired"] == 0  # idempotent


def test_answers_pointing_at_a_retired_row_are_rewritten(
    duplicated, org, actor, approved_version, question
):
    from apps.assessments.models import QuestionAnswer

    retired = duplicated["second"]
    answer = QuestionAnswer.objects.filter(plan_version=approved_version).first()
    answer.answer_json = {
        "value": "YES",
        "detail": [f"cat:{retired.pk}", f"cat:{duplicated['first'].pk}"],
    }
    answer.save(update_fields=["answer_json"])
    other = QuestionAnswer.objects.create(
        plan_version=approved_version,
        question=question,
        respondent_user=actor,
        answer_context="BIA",
        answer_json={"value": f"cat:{duplicated['unweighted'].pk}"},
    )
    result = dedupe_catalogue()
    assert result["answers_rewritten"] == 2
    answer.refresh_from_db()
    assert answer.answer_json["detail"] == [
        f"cat:{duplicated['first'].pk}",
        f"cat:{duplicated['first'].pk}",
    ]
    other.refresh_from_db()
    assert other.answer_json["value"] == f"cat:{duplicated['weighted'].pk}"


def test_command_dry_run_changes_nothing(duplicated, capsys):
    call_command("dedupe_lookup_catalogue", "--dry-run")
    assert LookupCategory.objects.count() == 5
    call_command("dedupe_lookup_catalogue")
    assert LookupCategory.objects.count() == 3
