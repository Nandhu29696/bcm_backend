"""
Risk score derivation (Phase 5.4).

The exit criterion: scores are reproducible from the lookup weights and the
same wherever they are read. That is what makes them derived on the server on
every save, never accepted from a client.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.lookups.models import LookupType
from apps.risk.models import Risk
from apps.risk.scoring import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MODERATE,
    allowed_points,
    compute_scores,
    label_for,
    rating_options,
)

pytestmark = pytest.mark.django_db


def risks_url(version):
    return reverse("risk:risk-list", args=[version.plan_version_id])


def risk_url(version, risk_id):
    return reverse("risk:risk-detail", args=[version.plan_version_id, risk_id])


# --------------------------------------------------------------------------- #
# The formula, pinned to the one legacy row
# --------------------------------------------------------------------------- #


def test_the_legacy_row_is_reproduced():
    """Possible (2) x High (3) with Ineffective (2) controls: 6, 4, Low."""
    scores = compute_scores(Decimal(2), Decimal(3), Decimal(2))
    assert scores.inherent == Decimal("6.00")
    assert scores.residual == Decimal("4.00")
    assert scores.level == LEVEL_LOW


@pytest.mark.parametrize(
    ("likelihood", "severity", "control", "inherent", "residual", "level"),
    [
        (3, 3, 1, "9.00", "8.00", LEVEL_HIGH),
        (3, 3, 3, "9.00", "6.00", LEVEL_MODERATE),
        (3, 2, 1, "6.00", "5.00", LEVEL_MODERATE),
        (2, 2, 1, "4.00", "3.00", LEVEL_LOW),
        (1, 1, 3, "1.00", "0.00", LEVEL_LOW),  # floored at zero
    ],
)
def test_scores_across_the_grid(likelihood, severity, control, inherent, residual, level):
    scores = compute_scores(Decimal(likelihood), Decimal(severity), Decimal(control))
    assert (str(scores.inherent), str(scores.residual), scores.level) == (inherent, residual, level)


def test_a_partially_rated_risk_has_no_score_rather_than_a_wrong_one():
    assert compute_scores(None, Decimal(3), Decimal(2)).inherent is None
    partial = compute_scores(Decimal(2), Decimal(3), None)
    assert partial.inherent == Decimal("6.00")
    assert partial.residual is None
    assert partial.level == ""


def test_thresholds_come_from_settings(settings):
    settings.RISK_LEVEL_THRESHOLDS = {"moderate": 2, "high": 4}
    assert compute_scores(Decimal(2), Decimal(3), Decimal(2)).level == LEVEL_HIGH


# --------------------------------------------------------------------------- #
# The catalogue as the source of weights
# --------------------------------------------------------------------------- #


def test_rating_options_are_the_weighted_catalogue_entries_deduplicated():
    """The legacy catalogue holds each entry twice, once without points."""
    options = rating_options(LookupType.LIKELIHOOD)
    assert [(str(o.points), o.label) for o in options] == [
        ("1.00", "Rare (1)"),
        ("2.00", "Possible (2)"),
        ("3.00", "Frequent (3)"),
    ]


def test_allowed_points_and_labels():
    assert allowed_points(LookupType.CONTROL_EFFECTIVENESS) == {Decimal(1), Decimal(2), Decimal(3)}
    assert label_for(LookupType.SEVERITY_RATING, Decimal(3)) == "High (3)"
    assert label_for(LookupType.SEVERITY_RATING, Decimal(7)) is None


# --------------------------------------------------------------------------- #
# Through the API: derived on save, refused as input
# --------------------------------------------------------------------------- #


def test_scores_are_derived_on_create(author_client, version):
    response = author_client.post(
        risks_url(version),
        {
            "risk_name": "Power failure",
            "likelihood_rating": "2",
            "severity_rating": "3",
            "control_effectiveness_rating": "2",
        },
    )
    assert response.status_code == 201, response.data
    assert response.data["inherent_risk_score"] == "6.00"
    assert response.data["residual_risk_score"] == "4.00"
    assert response.data["risk_level"] == LEVEL_LOW
    assert response.data["likelihood_label"] == "Possible (2)"


def test_scores_are_rederived_on_update(author_client, version):
    created = author_client.post(
        risks_url(version),
        {
            "risk_name": "Power failure",
            "likelihood_rating": "2",
            "severity_rating": "3",
            "control_effectiveness_rating": "2",
        },
    )
    response = author_client.patch(
        risk_url(version, created.data["risk_id"]), {"control_effectiveness_rating": "1"}
    )
    assert response.status_code == 200
    assert response.data["residual_risk_score"] == "5.00"
    assert response.data["risk_level"] == LEVEL_MODERATE


def test_a_client_supplied_score_is_refused_not_ignored(author_client, version):
    response = author_client.post(
        risks_url(version),
        {
            "risk_name": "Power failure",
            "likelihood_rating": "2",
            "severity_rating": "3",
            "control_effectiveness_rating": "2",
            "residual_risk_score": "0.01",
            "risk_level": "Low",
        },
    )
    assert response.status_code == 400
    assert "residual_risk_score" in response.data["field_errors"]
    assert not Risk.objects.filter(plan_version=version).exists()


def test_a_rating_outside_the_catalogue_is_refused(author_client, version):
    response = author_client.post(
        risks_url(version), {"risk_name": "x", "likelihood_rating": "7", "severity_rating": "3"}
    )
    assert response.status_code == 400
    assert "likelihood_rating" in response.data["field_errors"]


def test_stored_scores_match_the_function_exactly(author_client, version):
    """What the API returns is what the row holds is what the function says."""
    author_client.post(
        risks_url(version),
        {
            "risk_name": "Power failure",
            "likelihood_rating": "3",
            "severity_rating": "3",
            "control_effectiveness_rating": "1",
        },
    )
    risk = Risk.objects.get(plan_version=version)
    expected = compute_scores(
        risk.likelihood_rating, risk.severity_rating, risk.control_effectiveness_rating
    )
    assert (risk.inherent_risk_score, risk.residual_risk_score, risk.risk_level) == (
        expected.inherent,
        expected.residual,
        expected.level,
    )
    listed = author_client.get(risks_url(version)).data[0]
    assert Decimal(listed["residual_risk_score"]) == expected.residual


def test_the_register_is_ordered_by_residual_score_descending(author_client, version):
    for name, likelihood, severity, control in (
        ("low", 1, 1, 3),
        ("high", 3, 3, 1),
        ("mid", 2, 2, 1),
    ):
        author_client.post(
            risks_url(version),
            {
                "risk_name": name,
                "likelihood_rating": str(likelihood),
                "severity_rating": str(severity),
                "control_effectiveness_rating": str(control),
            },
        )
    assert [r["risk_name"] for r in author_client.get(risks_url(version)).data] == [
        "high",
        "mid",
        "low",
    ]
