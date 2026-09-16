"""Serializers for authentication and access control."""

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.accounts.models import Employee, Role, UserAccount, UserEstateScope, UserRole
from apps.accounts.roles import ALL_ESTATE_ROLES


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["role_id", "role_code", "role_name", "active_flag"]
        read_only_fields = ["role_id"]


class EmployeeSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = [
            "employee_id",
            "employee_number",
            "full_name",
            "email",
            "designation",
        ]


class CurrentUserSerializer(serializers.ModelSerializer):
    """The `/auth/me/` payload.

    Includes `role_codes` and `estate_ids` so the frontend can mirror permissions
    in the UI. Mirroring only — hiding a button is not the security boundary; the
    queryset scoping is.
    """

    employee = EmployeeSummarySerializer(read_only=True)
    role_codes = serializers.SerializerMethodField()
    estate_ids = serializers.SerializerMethodField()
    sees_all_estates = serializers.SerializerMethodField()

    class Meta:
        model = UserAccount
        fields = [
            "user_id",
            "email",
            "username",
            "display_name",
            "user_status",
            "auth_provider",
            "is_staff",
            "employee",
            "role_codes",
            "estate_ids",
            "sees_all_estates",
            "mfa_enabled",
            "phone_number",
            "job_title",
            "avatar_data_url",
        ]
        read_only_fields = fields

    def get_role_codes(self, obj) -> list[str]:
        return sorted(obj.role_codes)

    def get_sees_all_estates(self, obj) -> bool:
        return bool(obj.role_codes & ALL_ESTATE_ROLES)

    def get_estate_ids(self, obj) -> list[int]:
        if obj.role_codes & ALL_ESTATE_ROLES:
            return []  # empty means "unrestricted"; see sees_all_estates
        return sorted(
            obj.estate_scopes.filter(active_flag=True, estate__active_flag=True).values_list(
                "estate_id", flat=True
            )
        )


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        request = self.context.get("request")
        user = authenticate(
            request, username=attrs["email"].strip().lower(), password=attrs["password"]
        )
        if user is None:
            # Deliberately identical whether the account is unknown or the
            # password is wrong — anything else is an account enumeration oracle.
            raise serializers.ValidationError({"detail": "Incorrect email or password."})
        if not user.is_active:
            raise serializers.ValidationError({"detail": "This account is disabled."})
        attrs["user"] = user
        return attrs


class OtpVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=12)


class OtpResendSerializer(serializers.Serializer):
    email = serializers.EmailField()


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        try:
            validate_password(value, user=self.context["request"].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class RegistrationSerializer(serializers.Serializer):
    """Self-registration.

    Creates a PENDING account with no roles — the same landing state as an
    unlinked SSO sign-in. Registration proves control of an address; it does not
    grant access to anything.
    """

    email = serializers.EmailField()
    display_name = serializers.CharField(max_length=200)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_email(self, value):
        value = value.strip().lower()
        if UserAccount.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with that email already exists.")
        return value

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def create(self, validated_data):
        employee = Employee.objects.filter(email__iexact=validated_data["email"]).first()
        user = UserAccount.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            display_name=validated_data["display_name"],
            employee=employee,
            user_status=(UserAccount.Status.ACTIVE if employee else UserAccount.Status.PENDING),
        )
        return user


class UserRoleSerializer(serializers.ModelSerializer):
    role_code = serializers.CharField(source="role.role_code", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = UserRole
        fields = ["user_role_id", "user", "user_email", "role", "role_code", "assigned_at"]
        read_only_fields = ["user_role_id", "assigned_at"]


class UserEstateScopeSerializer(serializers.ModelSerializer):
    estate_name = serializers.CharField(source="estate.estate_name", read_only=True)

    class Meta:
        model = UserEstateScope
        fields = [
            "user_estate_scope_id",
            "user",
            "estate",
            "estate_name",
            "active_flag",
            "granted_at",
        ]
        read_only_fields = ["user_estate_scope_id", "granted_at"]


class UserAdminSerializer(serializers.ModelSerializer):
    """The administrator's view of an account (user administration screen).

    Roles and estate scopes are edited through this serializer as plain lists so
    the screen saves with one PATCH; the rows in `user_roles` and
    `user_estate_scopes` are reconciled to the lists (added, removed, or
    reactivated), never duplicated.
    """

    role_codes = serializers.ListField(child=serializers.CharField(), required=False)
    estate_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    employee = EmployeeSummarySerializer(read_only=True)
    employee_id = serializers.PrimaryKeyRelatedField(
        source="employee",
        queryset=Employee.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
    )
    last_login = serializers.DateTimeField(read_only=True)

    class Meta:
        model = UserAccount
        fields = [
            "user_id",
            "email",
            "display_name",
            "user_status",
            "auth_provider",
            "is_active",
            "is_staff",
            "mfa_enabled",
            "employee",
            "employee_id",
            "role_codes",
            "estate_ids",
            "created_at",
            "last_login",
        ]
        read_only_fields = ["user_id", "created_at", "auth_provider", "email"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["role_codes"] = sorted(instance.role_codes)
        data["estate_ids"] = sorted(
            instance.estate_scopes.filter(active_flag=True).values_list("estate_id", flat=True)
        )
        return data

    def validate_role_codes(self, codes):
        known = set(Role.objects.filter(role_code__in=codes).values_list("role_code", flat=True))
        unknown = sorted(set(codes) - known)
        if unknown:
            raise serializers.ValidationError(f"Unknown role code(s): {', '.join(unknown)}")
        return sorted(set(codes))

    def validate_estate_ids(self, ids):
        from apps.organization.models import Estate

        known = set(Estate.objects.filter(pk__in=ids).values_list("pk", flat=True))
        unknown = sorted(set(ids) - known)
        if unknown:
            raise serializers.ValidationError(f"Unknown estate id(s): {unknown}")
        return sorted(set(ids))

    def validate_employee_id(self, employee):  # field name, not source
        if employee is not None:
            taken = UserAccount.objects.filter(employee=employee)
            if self.instance is not None:
                taken = taken.exclude(pk=self.instance.pk)
            if taken.exists():
                raise serializers.ValidationError(
                    "That employee is already linked to another account."
                )
        return employee

    def update(self, instance, validated_data):
        role_codes = validated_data.pop("role_codes", None)
        estate_ids = validated_data.pop("estate_ids", None)
        actor = self.context["request"].user
        instance = super().update(instance, validated_data)

        if role_codes is not None:
            current = {ur.role.role_code: ur for ur in instance.user_roles.select_related("role")}
            for code in set(current) - set(role_codes):
                current[code].delete()
            for code in set(role_codes) - set(current):
                UserRole.objects.create(
                    user=instance, role=Role.objects.get(role_code=code), assigned_by=actor
                )
            instance._cached_role_codes = None

        if estate_ids is not None:
            existing = {s.estate_id: s for s in instance.estate_scopes.all()}
            for estate_id, scope in existing.items():
                wanted = estate_id in estate_ids
                if scope.active_flag != wanted:
                    scope.active_flag = wanted
                    scope.granted_by = actor
                    scope.save(update_fields=["active_flag", "granted_by"])
            for estate_id in set(estate_ids) - set(existing):
                UserEstateScope.objects.create(user=instance, estate_id=estate_id, granted_by=actor)
        return instance
