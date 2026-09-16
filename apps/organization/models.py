"""
Organisation master data.

The cost code is the join point of the whole model: process, subprocess, BU lead,
estate, location, region, center and LOB all hang off it, and it is the unit of
business that gets a continuity plan.

`legacy_id` columns are dormant back-references to the system being replaced. There
is no data migration (the legacy export contains no population data), so they are
carried for traceability only.
"""

from django.db import models

from apps.core.models import BaseModel


class Region(BaseModel):
    region_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    region_name = models.CharField(max_length=150, unique=True)
    geography = models.CharField(max_length=150, blank=True)

    class Meta(BaseModel.Meta):
        db_table = "regions"
        ordering = ["region_name"]

    def __str__(self):
        return self.region_name


class Estate(BaseModel):
    """The landing entity after login — journey step 2."""

    estate_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    estate_name = models.CharField(max_length=150, unique=True)

    class Meta(BaseModel.Meta):
        db_table = "estates"
        ordering = ["estate_name"]

    def __str__(self):
        return self.estate_name


class Location(BaseModel):
    location_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    location_name = models.CharField(max_length=150)
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="region_id",
        related_name="locations",
    )

    class Meta(BaseModel.Meta):
        db_table = "locations"
        ordering = ["location_name"]

    def __str__(self):
        return self.location_name


class Center(BaseModel):
    center_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    center_name = models.CharField(max_length=150)
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="location_id",
        related_name="centers",
    )
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="region_id",
        related_name="centers",
    )

    class Meta(BaseModel.Meta):
        db_table = "centers"
        ordering = ["center_name"]

    def __str__(self):
        return self.center_name


class Lob(BaseModel):
    """Line of business."""

    lob_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    lob_name = models.CharField(max_length=150, unique=True)

    class Meta(BaseModel.Meta):
        db_table = "lob"
        verbose_name = "LOB"
        verbose_name_plural = "LOBs"
        ordering = ["lob_name"]

    def __str__(self):
        return self.lob_name


class BuClassification(BaseModel):
    bu_classification_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    classification_name = models.CharField(max_length=150)

    class Meta(BaseModel.Meta):
        db_table = "bu_classifications"
        ordering = ["classification_name"]

    def __str__(self):
        return self.classification_name


class BuLead(BaseModel):
    """Business unit lead — the approver in journey step 7.

    `employee_id_legacy` is the legacy employee number, not an FK. The real link to
    a person is via `Employee.bu_lead`.
    """

    bu_lead_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    employee_id_legacy = models.BigIntegerField(null=True, blank=True)
    lead_name = models.CharField(max_length=200)
    email = models.EmailField(max_length=320, blank=True, db_index=True)

    class Meta(BaseModel.Meta):
        db_table = "bu_leads"
        ordering = ["lead_name"]

    def __str__(self):
        return self.lead_name


class EmployeeGroup(BaseModel):
    employee_group_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    group_name = models.CharField(max_length=150)

    class Meta(BaseModel.Meta):
        db_table = "employee_groups"
        ordering = ["group_name"]

    def __str__(self):
        return self.group_name


class EmployeeGrade(BaseModel):
    employee_grade_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    grade_name = models.CharField(max_length=80)

    class Meta(BaseModel.Meta):
        db_table = "employee_grades"
        ordering = ["grade_name"]

    def __str__(self):
        return self.grade_name


class Process(BaseModel):
    process_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    process_name = models.CharField(max_length=255)
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="region_id",
        related_name="processes",
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="location_id",
        related_name="processes",
    )

    class Meta(BaseModel.Meta):
        db_table = "processes"
        ordering = ["process_name"]
        verbose_name_plural = "processes"

    def __str__(self):
        return self.process_name


class Subprocess(BaseModel):
    subprocess_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    subprocess_name = models.CharField(max_length=255)
    process = models.ForeignKey(
        Process,
        on_delete=models.PROTECT,
        db_column="process_id",
        related_name="subprocesses",
    )

    class Meta(BaseModel.Meta):
        db_table = "subprocesses"
        ordering = ["subprocess_name"]
        verbose_name_plural = "subprocesses"

    def __str__(self):
        return self.subprocess_name


class CostCode(BaseModel):
    """The unit of business that gets a continuity plan — journey step 3.

    The composite indexes below exist for the estate-scoped cost code list, which
    is the most-hit endpoint in the application (Phase 2).
    """

    cost_code_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    cost_code = models.CharField(max_length=80, db_index=True)

    process = models.ForeignKey(
        Process,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="process_id",
        related_name="cost_codes",
    )
    subprocess = models.ForeignKey(
        Subprocess,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="subprocess_id",
        related_name="cost_codes",
    )
    bu_lead = models.ForeignKey(
        BuLead,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="bu_lead_id",
        related_name="cost_codes",
    )
    estate = models.ForeignKey(
        Estate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="estate_id",
        related_name="cost_codes",
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="location_id",
        related_name="cost_codes",
    )
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="region_id",
        related_name="cost_codes",
    )
    center = models.ForeignKey(
        Center,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="center_id",
        related_name="cost_codes",
    )
    lob = models.ForeignKey(
        Lob,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="lob_id",
        related_name="cost_codes",
    )

    class Meta(BaseModel.Meta):
        db_table = "cost_codes"
        ordering = ["cost_code"]
        indexes = [
            models.Index(
                fields=["estate", "active_flag", "cost_code"], name="idx_cc_estate_active_code"
            ),
            models.Index(fields=["estate", "process"], name="idx_cc_estate_process"),
            models.Index(fields=["estate", "bu_lead"], name="idx_cc_estate_bulead"),
            models.Index(fields=["estate", "subprocess"], name="idx_cc_estate_subproc"),
            models.Index(fields=["estate", "region"], name="idx_cc_estate_region"),
        ]

    def __str__(self):
        return self.cost_code
