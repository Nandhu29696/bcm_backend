"""Serializers for cost code actions, plan versions and history (journey step 4)."""

from __future__ import annotations

from rest_framework import serializers

from apps.accounts.models import Employee
from apps.organization.models import (
    BuLead,
    Center,
    CostCode,
    Lob,
    Location,
    Process,
    Region,
    Subprocess,
)
from apps.organization.serializers import named_ref
from apps.plans.models import CoordinatorAssignment, PlanStatusHistory, PlanVersion


class CostCodeDetailSerializer(serializers.ModelSerializer):
    """The read side of the edit drawer."""

    process = serializers.SerializerMethodField()
    subprocess = serializers.SerializerMethodField()
    region = serializers.SerializerMethodField()
    bu_lead = serializers.SerializerMethodField()
    lob = serializers.SerializerMethodField()
    center = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    estate = serializers.SerializerMethodField()

    class Meta:
        model = CostCode
        fields = [
            "cost_code_id",
            "cost_code",
            "estate",
            "process",
            "subprocess",
            "region",
            "bu_lead",
            "lob",
            "center",
            "location",
            "active_flag",
            "created_at",
            "updated_at",
        ]

    def get_process(self, obj):
        return named_ref(obj.process, "process_id", "process_name")

    def get_subprocess(self, obj):
        return named_ref(obj.subprocess, "subprocess_id", "subprocess_name")

    def get_region(self, obj):
        return named_ref(obj.region, "region_id", "region_name")

    def get_bu_lead(self, obj):
        return named_ref(obj.bu_lead, "bu_lead_id", "lead_name")

    def get_lob(self, obj):
        return named_ref(obj.lob, "lob_id", "lob_name")

    def get_center(self, obj):
        return named_ref(obj.center, "center_id", "center_name")

    def get_location(self, obj):
        return named_ref(obj.location, "location_id", "location_name")

    def get_estate(self, obj):
        return named_ref(obj.estate, "estate_id", "estate_name")


class CostCodeUpdateSerializer(serializers.ModelSerializer):
    """The write side (Phase 3.1), with referential validation.

    The organisation hierarchy is only partly expressed by foreign keys: nothing
    in the schema stops a cost code naming a subprocess that belongs to a
    different process, or a centre in a different region. Those combinations are
    silently wrong — the row saves, the filters still match it, and the estate
    report quietly double-counts. So the relationships are checked here.

    `estate` is deliberately not editable. Moving a cost code between estates
    would move it out of the editor's own scope mid-request and take its plans,
    risks and assignments with it; that is a migration, not an edit.
    """

    process = serializers.PrimaryKeyRelatedField(
        queryset=Process.objects.all(), allow_null=True, required=False
    )
    subprocess = serializers.PrimaryKeyRelatedField(
        queryset=Subprocess.objects.all(), allow_null=True, required=False
    )
    region = serializers.PrimaryKeyRelatedField(
        queryset=Region.objects.all(), allow_null=True, required=False
    )
    bu_lead = serializers.PrimaryKeyRelatedField(
        queryset=BuLead.objects.all(), allow_null=True, required=False
    )
    lob = serializers.PrimaryKeyRelatedField(
        queryset=Lob.objects.all(), allow_null=True, required=False
    )
    center = serializers.PrimaryKeyRelatedField(
        queryset=Center.objects.all(), allow_null=True, required=False
    )
    location = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects.all(), allow_null=True, required=False
    )

    class Meta:
        model = CostCode
        fields = [
            "cost_code",
            "process",
            "subprocess",
            "region",
            "bu_lead",
            "lob",
            "center",
            "location",
        ]

    def validate_cost_code(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A cost code is required.")

        estate_id = self.instance.estate_id
        clash = (
            CostCode.objects.filter(estate_id=estate_id, cost_code__iexact=value)
            .exclude(pk=self.instance.pk)
            .exists()
        )
        if clash:
            raise serializers.ValidationError(
                "Another cost code in this estate already uses that code."
            )
        return value

    def validate(self, attrs):
        """Check the combinations, using the incoming value or the stored one."""

        def resolved(name):
            return attrs[name] if name in attrs else getattr(self.instance, name)

        process = resolved("process")
        subprocess = resolved("subprocess")
        region = resolved("region")
        location = resolved("location")
        center = resolved("center")

        errors = {}

        if subprocess is not None:
            if process is None:
                errors["subprocess"] = "Choose a process before a subprocess."
            elif subprocess.process_id != process.process_id:
                errors["subprocess"] = (
                    f"'{subprocess.subprocess_name}' belongs to "
                    f"'{subprocess.process.process_name}', not '{process.process_name}'."
                )

        if (
            location is not None
            and region is not None
            and location.region_id is not None
            and location.region_id != region.region_id
        ):
            errors["location"] = (
                f"'{location.location_name}' is in a different region to "
                f"'{region.region_name}'."
            )

        if (
            center is not None
            and location is not None
            and center.location_id is not None
            and center.location_id != location.location_id
        ):
            errors["center"] = f"'{center.center_name}' is not in '{location.location_name}'."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class PlanVersionSerializer(serializers.ModelSerializer):
    """A row in the version switcher (Phase 3.3)."""

    is_current = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)
    can_copy = serializers.SerializerMethodField()
    approved_by_name = serializers.CharField(
        source="approved_by.display_name", default=None, read_only=True
    )
    created_by_name = serializers.CharField(
        source="created_by.display_name", default=None, read_only=True
    )
    coordinators = serializers.SerializerMethodField()
    # So the editor can link back to the cost code page without a second lookup.
    cost_code_id = serializers.IntegerField(source="plan.cost_code_id", read_only=True)
    cost_code = serializers.CharField(source="plan.cost_code.cost_code", read_only=True)

    class Meta:
        model = PlanVersion
        fields = [
            "plan_version_id",
            "plan_id",
            "cost_code_id",
            "cost_code",
            "version_number",
            "status",
            "plan_mode",
            "review_mode",
            "published_flag",
            "copied_flag",
            "is_current",
            "is_editable",
            "can_copy",
            "approved_at",
            "approved_by_name",
            "created_by_name",
            "created_at",
            "updated_at",
            "coordinators",
        ]

    def get_is_current(self, version) -> bool:
        # Supplied by the view, which already knows the highest version number for
        # the plan — asking per row would be a query each.
        return version.version_number == self.context.get("current_version_number")

    def get_can_copy(self, version) -> bool:
        from apps.plans.versioning import COPYABLE_STATUSES

        return version.status in COPYABLE_STATUSES and not self.context.get(
            "plan_has_open_version", False
        )

    def get_coordinators(self, version) -> list[dict]:
        return [
            {
                "coordinator_assignment_id": assignment.pk,
                "employee_id": assignment.employee_id,
                "name": assignment.employee.full_name,
                "email": assignment.employee.email,
                "coordinator_type": assignment.coordinator_type,
                "additional_user_flag": assignment.additional_user_flag,
            }
            for assignment in version.coordinator_assignments.all()
            if assignment.active_flag
        ]


class PlanStatusHistorySerializer(serializers.ModelSerializer):
    """One entry in the history timeline (Phase 3.5)."""

    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PlanStatusHistory
        fields = [
            "plan_status_history_id",
            "plan_version_id",
            "status",
            "comments",
            "changed_at",
            "changed_by_name",
        ]

    def get_changed_by_name(self, entry) -> str:
        # A deleted account leaves changed_by NULL (SET_NULL). The entry is still
        # true and must still render — the trail is append-only.
        return entry.changed_by.display_name if entry.changed_by_id else "System"


class CoordinatorAssignmentSerializer(serializers.ModelSerializer):
    employee = serializers.PrimaryKeyRelatedField(queryset=Employee.objects.all())
    name = serializers.CharField(source="employee.full_name", read_only=True)
    email = serializers.CharField(source="employee.email", read_only=True)

    class Meta:
        model = CoordinatorAssignment
        fields = [
            "coordinator_assignment_id",
            "plan_version_id",
            "employee",
            "name",
            "email",
            "coordinator_type",
            "additional_user_flag",
        ]
        read_only_fields = ["coordinator_assignment_id", "plan_version_id"]


class CopyPlanVersionSerializer(serializers.Serializer):
    """Body of the copy action — a reason, recorded on the new version's history."""

    comments = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")
