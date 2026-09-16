"""Infrastructure endpoints."""

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness probe. Does not touch the database."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(summary="Liveness probe", responses={200: dict})
    def get(self, request):
        return Response({"status": "ok"})


class ReadinessView(APIView):
    """Readiness probe. Fails if the database is unreachable.

    Separate from liveness on purpose: a failing dependency should stop traffic
    being routed here, not cause the container to be restarted.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(summary="Readiness probe", responses={200: dict, 503: dict})
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - surfaced as a 503, and logged
            return Response(
                {"status": "unavailable", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ready", "database": "ok"})
