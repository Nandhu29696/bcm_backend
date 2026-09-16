"""
Help library — journey step 8.

S6: reconstructed from the legacy `tran_Help_BCM_Team.csv`, which was a flat list
of documents with a name, description and attachment. The file itself lives in
`documents`; this is the catalogue entry that makes it findable.
"""

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class HelpResource(BaseModel):
    help_resource_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=80, blank=True, db_index=True)
    display_order = models.IntegerField(null=True, blank=True)
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="document_id",
        related_name="help_resources",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="help_resources_created",
    )

    class Meta(BaseModel.Meta):
        db_table = "help_resources"
        ordering = ["category", "display_order", "title"]

    def __str__(self):
        return self.title
