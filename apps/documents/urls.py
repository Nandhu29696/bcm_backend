"""Generated document routes (Phase 7)."""

from django.urls import path

from apps.documents import attachments, views

app_name = "documents"

urlpatterns = [
    path(
        "plan-versions/<int:plan_version_id>/documents/",
        views.VersionDocumentsView.as_view(),
        name="version-documents",
    ),
    path(
        "plan-versions/<int:plan_version_id>/documents/generate/",
        views.RegenerateView.as_view(),
        name="version-documents-generate",
    ),
    # Uploads a plan author attaches to a version (the network diagram).
    path(
        "plan-versions/<int:plan_version_id>/attachments/",
        attachments.VersionAttachmentsView.as_view(),
        name="version-attachments",
    ),
    path(
        "plan-versions/<int:plan_version_id>/attachments/<int:entity_document_id>/",
        attachments.VersionAttachmentDetailView.as_view(),
        name="version-attachment-detail",
    ),
    path(
        "documents/<int:entity_document_id>/link/",
        views.DocumentLinkView.as_view(),
        name="document-link",
    ),
    path(
        "documents/<int:entity_document_id>/download/",
        views.DocumentDownloadView.as_view(),
        name="document-download",
    ),
]
