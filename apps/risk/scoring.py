"""
Risk score derivation (Phase 5.4) — the single source of truth.

`inherent_risk_score`, `residual_risk_score` and `risk_level` are never accepted
from a client. They are computed here, from the three ratings, and written on
every save. The API, the UI and the document export all read the stored result
of this function, which is what makes them agree.

The formula
-----------
    inherent = likelihood x severity
    residual = inherent - control_effectiveness
    level    = High      if residual >= RISK_LEVEL_THRESHOLDS["high"]
               Moderate  if residual >= RISK_LEVEL_THRESHOLDS["moderate"]
               Low       otherwise

Where it came from: the legacy export contains exactly one populated risk row
(`tables/tran_BCP_Plan_RA_Risk Details.csv`), with Likelihood "Possible (2)",
Severity "High (3)", Control "Ineffective (2)", Inherent_Risk_Score 6, Risk
Score 4 and Risk Level Low. Multiplication reproduces the 6; subtraction of the
control rating reproduces the 4; "Low" at 4 sets the moderate threshold above 4.
That is one data point, so the formula is an inference, and the thresholds are
configuration rather than code. Confirm both with the business before Phase 9
dashboards are built on them.

The ratings themselves come from the lookup catalogue (Likelihood, Severity
Rating, Control Effectiveness), which carries the weight in `points`. The legacy
catalogue holds every entry twice — once with points, once without — so only
weighted rows are offered and accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from apps.lookups.models import LookupCategory, LookupType

LEVEL_HIGH = "High"
LEVEL_MODERATE = "Moderate"
LEVEL_LOW = "Low"

#: The three catalogue types a risk is rated against.
RATING_TYPES = {
    "likelihood_rating": LookupType.LIKELIHOOD,
    "severity_rating": LookupType.SEVERITY_RATING,
    "control_effectiveness_rating": LookupType.CONTROL_EFFECTIVENESS,
}


@dataclass(frozen=True)
class RatingOption:
    points: Decimal
    label: str


def rating_options(lookup_type: str) -> list[RatingOption]:
    """Weighted catalogue entries for one rating, ascending by weight.

    Unweighted duplicates (points NULL) are excluded: they carry no score and
    would make "Rare (1)" appear twice in the dropdown.
    """
    seen: set[Decimal] = set()
    options: list[RatingOption] = []
    for row in LookupCategory.objects.filter(
        category_type=lookup_type, points__isnull=False
    ).order_by("points", "category_name"):
        if row.points in seen:
            continue
        seen.add(row.points)
        options.append(RatingOption(points=row.points, label=row.category_name))
    return options


def allowed_points(lookup_type: str) -> set[Decimal]:
    return {option.points for option in rating_options(lookup_type)}


def label_for(lookup_type: str, points: Decimal | None) -> str | None:
    if points is None:
        return None
    for option in rating_options(lookup_type):
        if option.points == points:
            return option.label
    return None


@dataclass(frozen=True)
class Scores:
    inherent: Decimal | None
    residual: Decimal | None
    level: str


def compute_scores(
    likelihood: Decimal | None,
    severity: Decimal | None,
    control_effectiveness: Decimal | None,
) -> Scores:
    """Derive the scores. Any missing rating leaves the scores unset.

    Partially rated risks are legitimate while a plan is being drafted; they
    simply have no score yet rather than a misleading one.
    """
    if likelihood is None or severity is None:
        return Scores(inherent=None, residual=None, level="")

    inherent = (Decimal(likelihood) * Decimal(severity)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if control_effectiveness is None:
        return Scores(inherent=inherent, residual=None, level="")

    residual = (inherent - Decimal(control_effectiveness)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if residual < 0:
        residual = Decimal("0.00")

    return Scores(inherent=inherent, residual=residual, level=level_for(residual))


def level_for(residual: Decimal) -> str:
    thresholds = settings.RISK_LEVEL_THRESHOLDS
    if residual >= Decimal(str(thresholds["high"])):
        return LEVEL_HIGH
    if residual >= Decimal(str(thresholds["moderate"])):
        return LEVEL_MODERATE
    return LEVEL_LOW


def apply_scores(risk) -> None:
    """Write the derived scores onto a Risk instance (does not save)."""
    scores = compute_scores(
        risk.likelihood_rating, risk.severity_rating, risk.control_effectiveness_rating
    )
    risk.inherent_risk_score = scores.inherent
    risk.residual_risk_score = scores.residual
    risk.risk_level = scores.level
