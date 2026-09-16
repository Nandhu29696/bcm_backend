"""
Identity, people and access control.

AD-2: `user_accounts` is the AUTH_USER_MODEL and `employees` is a 1:1 profile. The
schema already separated identity from HR record — `user_accounts.employee_id` is
unique and nullable — and that separation is load-bearing: not every employee has a
login, and an SSO user can arrive before an HR record exists.

Changing AUTH_USER_MODEL after the first migration requires rebuilding the database,
so this module must be complete before any `makemigrations` runs.
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel, TimeStampedModel


class Employee(BaseModel):
    """HR record. Not an identity — see UserAccount."""

    employee_id = models.BigAutoField(primary_key=True)
    legacy_row_id = models.BigIntegerField(unique=True, null=True, blank=True)
    employee_number = models.CharField(max_length=30, unique=True)
    full_name = models.CharField(max_length=200)
    email = models.EmailField(max_length=320, blank=True, db_index=True)
    designation = models.CharField(max_length=150, blank=True)
    domain_name = models.CharField(max_length=150, blank=True)
    gender = models.CharField(max_length=30, blank=True)
    contact_number = models.CharField(max_length=50, blank=True)
    employment_status = models.CharField(max_length=50, blank=True)
    date_of_joining = models.DateField(null=True, blank=True)
    last_working_date = models.DateField(null=True, blank=True)

    # Two self-references on one model. Without explicit related_names these
    # collide (fields.E304).
    manager_employee = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="manager_employee_id",
        related_name="direct_reports",
    )
    supervisor_employee = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="supervisor_employee_id",
        related_name="supervisees",
    )

    bu_lead = models.ForeignKey(
        "organization.BuLead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="bu_lead_id",
        related_name="employees",
    )
    bu_classification = models.ForeignKey(
        "organization.BuClassification",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="bu_classification_id",
        related_name="employees",
    )
    center = models.ForeignKey(
        "organization.Center",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="center_id",
        related_name="employees",
    )
    process = models.ForeignKey(
        "organization.Process",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="process_id",
        related_name="employees",
    )
    subprocess = models.ForeignKey(
        "organization.Subprocess",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="subprocess_id",
        related_name="employees",
    )
    cost_code = models.ForeignKey(
        "organization.CostCode",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="cost_code_id",
        related_name="employees",
    )
    current_location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="current_location_id",
        related_name="employees_currently_at",
    )
    estate = models.ForeignKey(
        "organization.Estate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="estate_id",
        related_name="employees",
    )
    location = models.ForeignKey(
        "organization.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="location_id",
        related_name="employees",
    )
    region = models.ForeignKey(
        "organization.Region",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="region_id",
        related_name="employees",
    )
    lob = models.ForeignKey(
        "organization.Lob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="lob_id",
        related_name="employees",
    )
    employee_group = models.ForeignKey(
        "organization.EmployeeGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_group_id",
        related_name="employees",
    )
    employee_grade = models.ForeignKey(
        "organization.EmployeeGrade",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_grade_id",
        related_name="employees",
    )

    source_created_at = models.DateTimeField(null=True, blank=True)
    source_modified_at = models.DateTimeField(null=True, blank=True)

    class Meta(BaseModel.Meta):
        db_table = "employees"
        ordering = ["full_name"]
        indexes = [models.Index(fields=["cost_code"], name="idx_emp_cost_code")]

    def __str__(self):
        return f"{self.full_name} ({self.employee_number})"


class UserAccountManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("A user account requires an email address.")
        email = self.normalize_email(email)
        extra.setdefault("display_name", email.split("@")[0])
        user = self.model(email=email, **extra)
        # SSO-only accounts have no usable password.
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("user_status", UserAccount.Status.ACTIVE)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra)


class UserAccount(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Identity. AUTH_USER_MODEL.

    `user_status` is the domain state (an SSO user with no HR record lands as
    PENDING); `is_active` is Django's authentication gate. They are deliberately
    separate — a PENDING user can still log in, they just resolve to an empty scope,
    which surfaces misconfiguration instead of disguising it as a bad password.
    """

    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        PENDING = "Pending", "Pending"
        SUSPENDED = "Suspended", "Suspended"
        DISABLED = "Disabled", "Disabled"

    class AuthProvider(models.TextChoices):
        LOCAL = "local", "Local"
        GOOGLE = "google", "Google"
        MICROSOFT = "microsoft", "Microsoft"

    user_id = models.BigAutoField(primary_key=True)
    employee = models.OneToOneField(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="employee_id",
        related_name="user_account",
    )
    username = models.CharField(max_length=150, blank=True)
    email = models.EmailField(max_length=320, unique=True)
    display_name = models.CharField(max_length=200)
    user_status = models.CharField(max_length=30, choices=Status.choices, default=Status.ACTIVE)

    # S11 — SSO identity. provider_subject is the IdP's stable subject claim; it is
    # what identifies the user, not the email, which can be reassigned.
    auth_provider = models.CharField(
        max_length=20, choices=AuthProvider.choices, default=AuthProvider.LOCAL
    )
    provider_subject = models.CharField(max_length=255, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # Per-user second factor. Administrators switch it; the global
    # REQUIRE_OTP_FOR_LOGIN (pinned on in production) is the master switch that
    # lets tests and demo runs turn OTP off for everyone.
    mfa_enabled = models.BooleanField(default=True)
    # Profile (self-service). The avatar is stored as a small data URL (JPEG,
    # resized to 192px) so the profile payload carries it and an <img> needs no
    # authenticated fetch. Kept off the employee record: HR data is not the
    # user's to edit.
    phone_number = models.CharField(max_length=30, blank=True)
    job_title = models.CharField(max_length=150, blank=True)
    avatar_data_url = models.TextField(blank=True)

    objects = UserAccountManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]

    class Meta:
        db_table = "user_accounts"
        ordering = ["display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["auth_provider", "provider_subject"],
                condition=~models.Q(provider_subject=""),
                name="uq_user_provider_subject",
            )
        ]

    def __str__(self):
        return self.display_name or self.email

    @property
    def role_codes(self) -> set[str]:
        """Role codes held by this user — the primitive AD-3 permissions key on."""
        return set(
            self.user_roles.filter(role__active_flag=True).values_list("role__role_code", flat=True)
        )


class Role(models.Model):
    role_id = models.BigAutoField(primary_key=True)
    legacy_id = models.BigIntegerField(unique=True, null=True, blank=True)
    role_code = models.CharField(max_length=80, unique=True)
    role_name = models.CharField(max_length=150)
    active_flag = models.BooleanField(default=True)

    class Meta:
        db_table = "roles"
        ordering = ["role_name"]

    def __str__(self):
        return self.role_code


class UserRole(models.Model):
    """S2 — surrogate PK replacing the composite (user_id, role_id).

    Django 5.2 does support CompositePrimaryKey, but a surrogate key is still the
    right call: composite PKs cannot be targeted by a ForeignKey and are awkward to
    address from a REST API.
    """

    user_role_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="user_roles",
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        db_column="role_id",
        related_name="user_roles",
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    assigned_by = models.ForeignKey(
        UserAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="assigned_by",
        related_name="roles_assigned",
    )

    class Meta:
        db_table = "user_roles"
        constraints = [models.UniqueConstraint(fields=["user", "role"], name="uq_user_role")]

    def __str__(self):
        return f"{self.user} -> {self.role}"


class UserEstateScope(models.Model):
    """S7 — which estates a user may see (journey step 2).

    Nothing in the original schema linked a user to an estate, so AD-3 scoping had
    nothing to filter on. A role-level all-estates bypass (BCM admin) short-circuits
    this table rather than requiring a row per estate.
    """

    user_estate_scope_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="estate_scopes",
    )
    estate = models.ForeignKey(
        "organization.Estate",
        on_delete=models.CASCADE,
        db_column="estate_id",
        related_name="user_scopes",
    )
    active_flag = models.BooleanField(default=True)
    granted_by = models.ForeignKey(
        UserAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="granted_by",
        related_name="estate_scopes_granted",
    )
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "user_estate_scopes"
        constraints = [models.UniqueConstraint(fields=["user", "estate"], name="uq_user_estate")]

    def __str__(self):
        return f"{self.user} -> {self.estate}"


class OtpChallenge(models.Model):
    """S9 — email OTP second factor (Phase 1.3).

    Only the hash is stored, never the code itself.
    """

    class Purpose(models.TextChoices):
        LOGIN_2FA = "LOGIN_2FA", "Login second factor"
        PASSWORD_RESET = "PASSWORD_RESET", "Password reset"

    otp_challenge_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="otp_challenges",
    )
    code_hash = models.CharField(max_length=128)
    purpose = models.CharField(max_length=40, choices=Purpose.choices)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "otp_challenges"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "purpose", "consumed_at"], name="idx_otp_user_purpose"),
        ]

    def __str__(self):
        return f"{self.purpose} for {self.user}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at
