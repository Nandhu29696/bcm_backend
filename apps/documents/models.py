"""
Document storage and polymorphic attachment (AD-7).

`Document` holds the file — content-addressed, so uploading the same bytes twice
reuses the row. `EntityDocument` attaches it to any entity.

S1: `entity_documents` had a composite primary key of
(document_id, entity_type, entity_id). Two problems: a composite PK cannot be
targeted by a ForeignKey and is awkward to address from a REST API, and — the real
bug — the key omitted `document_type`, so one document could not be attached to one
entity under two different types (e.g. both RECOVERY_PLAN and TEST_REPORT). Now a
surrogate PK with a four-column unique constraint.
"""

from django.conf import settings
from django.db import models


class DocumentTemplate(models.Model):
    """A versioned rendering template (Phase 7.2).

    A generated document records which template version produced it, so an
    approved plan document from last year can be traced to the layout that was
    current then — the legacy system kept approved-template-version and
    download-status tables for the same reason. Bumping `RENDERER_VERSION` in
    `apps.documents.generation` requires a new row here; a test enforces it.
    """

    document_template_id = models.BigAutoField(primary_key=True)
    template_code = models.CharField(max_length=80)
    version_number = models.PositiveIntegerField()
    description = models.CharField(max_length=255, blank=True)
    active_flag = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "document_templates"
        ordering = ["template_code", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["template_code", "version_number"], name="uq_document_template_version"
            )
        ]

    def __str__(self):
        return f"{self.template_code} v{self.version_number}"


class Document(models.Model):
    document_id = models.BigAutoField(primary_key=True)
    file_name = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=500, unique=True)
    mime_type = models.CharField(max_length=150, blank=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="uploaded_by",
        related_name="documents_uploaded",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # Set for generated documents only; uploads have no template.
    template = models.ForeignKey(
        DocumentTemplate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        db_column="document_template_id",
        related_name="documents",
    )

    class Meta:
        db_table = "documents"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.file_name


class EntityDocument(models.Model):
    """Attaches a document to any entity.

    Deliberately not a Django GenericForeignKey: `entity_type` values are stable
    strings owned by this application (PLAN_VERSION, EXEMPTION, TEST_OUTCOME, ...),
    not contenttype ids, so they survive app renames and are readable in the
    database.
    """

    class EntityType(models.TextChoices):
        PLAN_VERSION = "PLAN_VERSION", "Plan version"
        EXEMPTION = "EXEMPTION", "Exemption"
        TEST = "TEST", "Test"
        TEST_OUTCOME = "TEST_OUTCOME", "Test outcome"
        CRISIS_EVENT = "CRISIS_EVENT", "Crisis event"
        COST_CODE = "COST_CODE", "Cost code"
        HELP_RESOURCE = "HELP_RESOURCE", "Help resource"
        REPORT_REQUEST = "REPORT_REQUEST", "Report request"

    entity_document_id = models.BigAutoField(primary_key=True)
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        db_column="document_id",
        related_name="attachments",
    )
    entity_type = models.CharField(max_length=80, choices=EntityType.choices)
    entity_id = models.BigIntegerField()
    document_type = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = "entity_documents"
        constraints = [
            models.UniqueConstraint(
                fields=["document", "entity_type", "entity_id", "document_type"],
                name="uq_entity_document",
            )
        ]
        indexes = [models.Index(fields=["entity_type", "entity_id"], name="idx_entdoc_entity")]

    def __str__(self):
        return f"{self.entity_type}:{self.entity_id} -> doc {self.document_id}"
