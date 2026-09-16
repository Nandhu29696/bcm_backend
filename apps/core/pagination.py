"""Pagination classes (AD-10)."""

from django.conf import settings
from rest_framework.pagination import CursorPagination, PageNumberPagination


class StandardPageNumberPagination(PageNumberPagination):
    """Default pagination for list endpoints."""

    page_size_query_param = "page_size"

    @property
    def max_page_size(self):
        return settings.API_MAX_PAGE_SIZE


class StandardCursorPagination(CursorPagination):
    """For high-volume, append-heavy tables.

    Use on `call_attempts` and `question_answers`, where page-number pagination
    drifts as rows are inserted mid-scroll.
    """

    page_size_query_param = "page_size"
    ordering = "-created_at"

    @property
    def max_page_size(self):
        return settings.API_MAX_PAGE_SIZE
