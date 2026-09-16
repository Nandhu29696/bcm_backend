"""Structured BIA sections (Phase 5.1-5.3)."""

from __future__ import annotations

from rest_framework import serializers

from apps.accounts.models import Employee
from apps.assessments.models import (
    BiaCriticalContact,
    BiaServiceDescription,
    NetworkRequirement,
    RequirementType,
)
from apps.organization.models import CostCode, Process, Subprocess


class EmployeeRefField(serializers.PrimaryKeyRelatedField):
    """An employee by id, rendered with a name so the table needs no lookup."""

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", Employee.objects.all())
        kwargs.setdefault("allow_null", True)
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def to_representation(self, value):
        employee = value if isinstance(value, Employee) else Employee.objects.get(pk=value.pk)
        return {"id": employee.pk, "name": employee.full_name, "email": employee.email}


class BiaServiceDescriptionSerializer(serializers.ModelSerializer):
    """Recovery targets for one process/subprocess: MAO, MBCO, RTO, RPO (5.1).

    `process`, `subprocess` and `cost_code` default to the plan's own when
    omitted — a service description almost always describes the plan's process.
    """

    process = serializers.PrimaryKeyRelatedField(queryset=Process.objects.all(), required=False)
    subprocess = serializers.PrimaryKeyRelatedField(
        queryset=Subprocess.objects.all(), allow_null=True, required=False
    )
    cost_code = serializers.PrimaryKeyRelatedField(
        queryset=CostCode.objects.all(), allow_null=True, required=False
    )
    owner_employee = EmployeeRefField()
    process_name = serializers.CharField(source="process.process_name", read_only=True)
    subprocess_name = serializers.CharField(
        source="subprocess.subprocess_name", read_only=True, default=None
    )

    class Meta:
        model = BiaServiceDescription
        fields = [
            "service_description_id",
            "plan_version_id",
            "process",
            "process_name",
            "subprocess",
            "subprocess_name",
            "cost_code",
            "owner_employee",
            "process_description",
            "mao",
            "mbco",
            "rto",
            "rpo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["service_description_id", "plan_version_id"]

    def validate(self, attrs):
        version = self.context.get("plan_version")
        default_process = version.plan.process if version is not None else None
        process = attrs.get("process") or getattr(self.instance, "process", None) or default_process
        subprocess = attrs.get("subprocess", getattr(self.instance, "subprocess", None))
        if subprocess is not None and process is not None and subprocess.process_id != process.pk:
            raise serializers.ValidationError(
                {
                    "subprocess": f"'{subprocess.subprocess_name}' does not belong to '{process.process_name}'."
                }
            )
        for name in ("mao", "mbco", "rto", "rpo"):
            if name in attrs:
                attrs[name] = attrs[name].strip()
        return attrs

    def create(self, validated_data):
        version = validated_data["plan_version"]
        cost_code = version.plan.cost_code
        validated_data.setdefault("process", version.plan.process)
        validated_data.setdefault("cost_code", cost_code)
        if "subprocess" not in validated_data:
            validated_data["subprocess"] = cost_code.subprocess
        return super().create(validated_data)


class BiaCriticalContactSerializer(serializers.ModelSerializer):
    """Who must be reachable, and what they need to work (5.2)."""

    employee = EmployeeRefField()

    class Meta:
        model = BiaCriticalContact
        fields = [
            "critical_contact_id",
            "plan_version_id",
            "employee",
            "contact_type",
            "shift_timings",
            "primary_phone",
            "alternate_phone",
            "seat_count",
            "voice_non_voice",
            "asset_id",
            "asset_make",
            "hardware_software",
            "created_at",
        ]
        read_only_fields = ["critical_contact_id", "plan_version_id"]

    def validate_seat_count(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Seat count cannot be negative.")
        return value

    def validate(self, attrs):
        # A contact with neither a person nor a phone is not a contact.
        employee = attrs.get("employee", getattr(self.instance, "employee", None))
        phone = attrs.get("primary_phone", getattr(self.instance, "primary_phone", ""))
        if employee is None and not (phone or "").strip():
            raise serializers.ValidationError(
                {"primary_phone": "Name an employee or give a primary phone number."}
            )
        return attrs


class NetworkRequirementSerializer(serializers.ModelSerializer):
    """Connectivity the process depends on (5.3)."""

    employee = EmployeeRefField()
    requirement_type = serializers.ChoiceField(choices=RequirementType.choices)

    class Meta:
        model = NetworkRequirement
        fields = [
            "network_requirement_id",
            "plan_version_id",
            "employee",
            "requirement_type",
            "source_ip",
            "destination_ip",
            "port_number",
            "connectivity_type",
            "comments",
            "created_at",
        ]
        read_only_fields = ["network_requirement_id", "plan_version_id"]

    def validate(self, attrs):
        source = attrs.get("source_ip", getattr(self.instance, "source_ip", ""))
        destination = attrs.get("destination_ip", getattr(self.instance, "destination_ip", ""))
        if not (source or "").strip() and not (destination or "").strip():
            raise serializers.ValidationError(
                {"destination_ip": "Give at least a source or a destination."}
            )
        return attrs
