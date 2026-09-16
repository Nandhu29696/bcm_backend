"""
Scored reference catalogue.

Despite the table names, `question_categories` / `question_subcategories` are NOT
the questionnaire hierarchy (ARCHITECTURE.md §4.1) — they are a lookup catalogue
with scores. The legacy data holds rows like ("Rare (1)", "Likelihood", 1), across
category types: Likelihood, Severity Rating, Control Effectiveness, Risk Level,
Risk Name, Resource Type, Mitigation Status, Contingency Status, Core Strategy,
Tactical Strategy, Primary sites, Secondary Site, Vendor, Subcontractor,
Corporate function, BCP exemption.

These drive the risk-scoring dropdowns (and supply the numeric weights risk scores
are derived from in Phase 5) and several BIA question option lists.

The db_table names are kept for migration cleanliness; the misleading names are not
exposed through the API.
"""

from django.db import models

from apps.core.models import TimeStampedModel


class LookupType(models.TextChoices):
    """Known values of `LookupCategory.category_type`.

    Not enforced as a constraint — the catalogue is data, and new types are added
    by administrators without a deploy. Listed so code can reference them safely.
    """

    LIKELIHOOD = "Likelihood", "Likelihood"
    SEVERITY_RATING = "Severity Rating", "Severity Rating"
    CONTROL_EFFECTIVENESS = "Control Effectiveness", "Control Effectiveness"
    RISK_LEVEL = "Risk Level", "Risk Level"
    RISK_NAME = "Risk Name", "Risk Name"
    RESOURCE_TYPE = "Resource Type", "Resource Type"
    MITIGATION_STATUS = "Mitigation Status", "Mitigation Status"
    CONTINGENCY_STATUS = "Contingency Status", "Contingency Status"
    CORE_STRATEGY = "Core Strategy", "Core Strategy"
    TACTICAL_STRATEGY = "Tactical Strategy", "Tactical Strategy"
    PRIMARY_SITES = "Primary sites", "Primary sites"
    SECONDARY_SITE = "Secondary Site", "Secondary Site"
    VENDOR = "Vendor", "Vendor"
    SUBCONTRACTOR = "Subcontractor", "Subcontractor"
    CORPORATE_FUNCTION = "Corporate function", "Corporate function"
    BCP_EXEMPTION = "BCP exemption", "BCP exemption"


class LookupCategory(models.Model):
    """A catalogue entry: a named value within a category type, carrying a score."""

    category_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    category_name = models.CharField(max_length=200)
    category_type = models.CharField(max_length=50, blank=True, db_index=True)
    # The numeric weight. Risk scoring in Phase 5 derives from these, so the value
    # is authoritative — never re-derive a score from the label text.
    points = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "question_categories"
        ordering = ["category_type", "points", "category_name"]
        verbose_name = "lookup category"
        verbose_name_plural = "lookup categories"

    def __str__(self):
        return f"{self.category_type}: {self.category_name}"


class LookupValue(TimeStampedModel):
    """A child value under a catalogue entry (e.g. "FSL Owned Site")."""

    subcategory_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    category = models.ForeignKey(
        LookupCategory,
        on_delete=models.CASCADE,
        db_column="category_id",
        related_name="values",
    )
    subcategory_name = models.CharField(max_length=200)

    class Meta:
        db_table = "question_subcategories"
        ordering = ["subcategory_name"]
        verbose_name = "lookup value"
        verbose_name_plural = "lookup values"

    def __str__(self):
        return self.subcategory_name
