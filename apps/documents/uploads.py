"""
User uploads (Phase 8): test reports, later help documents.

Same content-addressed store as generated documents, under `uploads/` and with
no template. Validation is by extension and size - the list is configuration,
because what counts as an acceptable report is a business decision.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from django.conf import settings

from apps.core.exceptions import DomainError
from apps.documents.generation import media_root
from apps.documents.models import Document, EntityDocument


class UploadRejected(DomainError):
    default_code = "upload_rejected"


def validate_upload(upload) -> None:
    suffix = Path(upload.name or "").suffix.lower().lstrip(".")
    allowed = [ext.lower().lstrip(".") for ext in settings.ALLOWED_UPLOAD_EXTENSIONS]
    if suffix not in allowed:
        raise UploadRejected(
            f"Files of type '{suffix or 'unknown'}' are not accepted. Allowed: {', '.join(allowed)}."
        )
    if upload.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise UploadRejected(f"The file is larger than {settings.MAX_UPLOAD_SIZE_MB} MB.")


def store_upload(upload, *, actor=None) -> Document:
    validate_upload(upload)
    content = upload.read()
    digest = hashlib.sha256(content).hexdigest()
    suffix = Path(upload.name).suffix.lower()
    storage_key = f"uploads/{digest[:2]}/{digest}{suffix}"

    existing = Document.objects.filter(storage_key=storage_key).first()
    if existing is not None:
        return existing

    path = media_root() / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    mime = upload.content_type or mimetypes.guess_type(upload.name)[0] or "application/octet-stream"
    return Document.objects.create(
        file_name=Path(upload.name).name[:255],
        storage_key=storage_key,
        mime_type=mime,
        file_size_bytes=len(content),
        checksum_sha256=digest,
        uploaded_by=actor,
    )


def attach(
    document: Document, *, entity_type: str, entity_id: int, document_type: str
) -> EntityDocument:
    attachment, _ = EntityDocument.objects.get_or_create(
        document=document, entity_type=entity_type, entity_id=entity_id, document_type=document_type
    )
    return attachment
