"""Serializers for the estate and cost code screens (journey steps 2-3)."""

from __future__ import annotations

from rest_framework import serializers

from apps.organization.models import CostCode, Estate
from apps.organization.querysets import CURRENT_STATUS


class NamedRefSerializer(serializers.Serializer):
    """A `{id, name}` pair, for a foreign key the UI shows as a label."""

    id = serializers.IntegerField()
    name = serializers.CharField()


def named_ref(instance, id_attr: str, name_attr: str) -> dict | None:
    if instance is None:
        return None
    return {"id": getattr(instance, id_attr), "name": getattr(instance, name_attr)}


class EstateSerializer(serializers.ModelSerializer):
    """One card in the estate list.

    `cost_code_count` and `status_rollup` are supplied by the view, not computed
    here — a serializer that queried per instance would be an N+1 by construction.
    """

    cost_code_count = serializers.IntegerField(read_only=True)
    status_rollup = serializers.SerializerMethodField()

    class Meta:
        model = Estate
        fields = ["estate_id", "estate_name", "cost_code_count", "status_rollup"]

    def get_status_rollup(self, estate) -> dict:
        rollup = self.context.get("status_rollup", {})
        return rollup.get(estate.estate_id, self.context["empty_rollup"])


class CostCodeListSerializer(serializers.ModelSerializer):
    """A row of the cost code table.

    Flat `{id, name}` refs rather than nested model serializers: the table renders
    a label and filters by id, and nesting full objects would triple the payload
    of the most-requested endpoint in the application for no gain.
    """

    process = serializers.SerializerMethodField()
    subprocess = serializers.SerializerMethodField()
    region = serializers.SerializerMethodField()
    bu_lead = serializers.SerializerMethodField()
    lob = serializers.SerializerMethodField()
    center = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()

    bcp_status = serializers.CharField(source=CURRENT_STATUS, read_only=True)
    current_plan_version_id = serializers.IntegerField(read_only=True, allow_null=True)
    current_version_number = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = CostCode
        fields = [
            "cost_code_id",
            "cost_code",
            "estate_id",
            "process",
            "subprocess",
            "region",
            "bu_lead",
            "lob",
            "center",
            "location",
            "bcp_status",
            "current_plan_version_id",
            "current_version_number",
            "active_flag",
        ]

    def get_process(self, obj) -> dict | None:
        return named_ref(obj.process, "process_id", "process_name")

    def get_subprocess(self, obj) -> dict | None:
        return named_ref(obj.subprocess, "subprocess_id", "subprocess_name")

    def get_region(self, obj) -> dict | None:
        return named_ref(obj.region, "region_id", "region_name")

    def get_bu_lead(self, obj) -> dict | None:
        return named_ref(obj.bu_lead, "bu_lead_id", "lead_name")

    def get_lob(self, obj) -> dict | None:
        return named_ref(obj.lob, "lob_id", "lob_name")

    def get_center(self, obj) -> dict | None:
        return named_ref(obj.center, "center_id", "center_name")

    def get_location(self, obj) -> dict | None:
        return named_ref(obj.location, "location_id", "location_name")


class FilterOptionsSerializer(serializers.Serializer):
    """Facet lists for the filter bar (Phase 2.4)."""

    process = NamedRefSerializer(many=True)
    subprocess = NamedRefSerializer(many=True)
    region = NamedRefSerializer(many=True)
    bu_lead = NamedRefSerializer(many=True)
    bcp_status = serializers.ListField(child=serializers.CharField())
