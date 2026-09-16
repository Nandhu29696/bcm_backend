from rest_framework import serializers

from apps.calltree.models import CallAttempt, CallTreeMember, CallTreeRun


class RunSummarySerializer(serializers.ModelSerializer):
    """Enough for a list row or an event card: counts, not attempts."""

    members = serializers.SerializerMethodField()
    reached = serializers.SerializerMethodField()
    initiated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CallTreeRun
        fields = [
            "call_tree_run_id",
            "broadcast_id",
            "call_tree_type",
            "status",
            "simulation_flag",
            "providers_enabled",
            "started_at",
            "completed_at",
            "members",
            "reached",
            "initiated_by_name",
        ]

    def get_members(self, run) -> int:
        return len(run.members.all())

    def get_reached(self, run) -> int:
        return sum(1 for m in run.members.all() if m.reached_flag)

    def get_initiated_by_name(self, run) -> str:
        return run.initiated_by.display_name if run.initiated_by_id else ""


class AttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = CallAttempt
        fields = [
            "call_attempt_id",
            "channel",
            "attempt_number",
            "attempt_status",
            "status_code",
            "attempted_at",
            "response_key",
            "call_duration_seconds",
            "comments",
        ]


class MemberSerializer(serializers.ModelSerializer):
    attempts = AttemptSerializer(many=True, read_only=True)
    stage = serializers.SerializerMethodField()

    class Meta:
        model = CallTreeMember
        fields = [
            "call_tree_member_id",
            "member_name",
            "member_email",
            "phone_number",
            "sequence_number",
            "escalation_level",
            "stage",
            "reached_flag",
            "reached_channel",
            "completed_at",
            "attempts",
        ]

    def get_stage(self, member) -> str:
        from apps.calltree.models import MemberStage

        try:
            return MemberStage(member.escalation_level).label
        except ValueError:
            return ""


class RunDetailSerializer(RunSummarySerializer):
    members_detail = MemberSerializer(source="members", many=True, read_only=True)
    cost_code_label = serializers.CharField(
        source="cost_code.cost_code", read_only=True, default=""
    )
    cost_code_id = serializers.IntegerField(read_only=True)

    class Meta(RunSummarySerializer.Meta):
        fields = RunSummarySerializer.Meta.fields + [
            "cost_code_id",
            "cost_code_label",
            "members_detail",
        ]
