"""Consistent API error envelope (AD-10)."""

import logging

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class DomainError(drf_exceptions.APIException):
    """Base for business-rule violations raised by the service layer.

    Services raise these; views do not translate them. Subclass with a specific
    `default_code` so the frontend can branch on `code` rather than on message text.
    """

    status_code = 400
    default_detail = "The request could not be completed."
    default_code = "domain_error"


class InvalidStateTransition(DomainError):
    status_code = 409
    default_detail = "That status transition is not permitted."
    default_code = "invalid_state_transition"


def api_exception_handler(exc, context):
    """Return every error as {detail, code, field_errors}.

    A single shape means the frontend has one error path instead of one per
    endpoint. `field_errors` is populated only for validation failures.
    """
    if isinstance(exc, DjangoValidationError):
        exc = drf_exceptions.ValidationError(
            detail=exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        )
    elif isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = drf_exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)

    if response is None:
        # Unhandled exception: let Django's 500 handling and error reporting take
        # over rather than silently flattening a bug into a tidy JSON envelope.
        logger.exception("Unhandled exception in %s", context.get("view"))
        return None

    code = getattr(exc, "default_code", "error")
    field_errors = {}
    detail = response.data

    if isinstance(detail, dict) and not {"detail"} & set(detail):
        field_errors = detail
        message = "Validation failed."
    elif isinstance(detail, dict):
        message = str(detail.get("detail", ""))
    elif isinstance(detail, list):
        message = "Validation failed."
        field_errors = {"non_field_errors": detail}
    else:
        message = str(detail)

    response.data = {
        "detail": message,
        "code": code,
        "field_errors": field_errors,
    }
    return response
