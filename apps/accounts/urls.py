"""Authentication and access-control routes."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.accounts import profile, sso_views, views

app_name = "accounts"

router = DefaultRouter()
router.register("roles", views.RoleViewSet, basename="role")
router.register("employees", views.EmployeeLookupViewSet, basename="employee")
router.register("admin/users", views.UserAdminViewSet, basename="admin-user")
router.register("admin/user-roles", views.UserRoleViewSet, basename="admin-user-role")
router.register(
    "admin/user-estate-scopes",
    views.UserEstateScopeViewSet,
    basename="admin-user-estate-scope",
)

auth_patterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("otp/verify/", views.OtpVerifyView.as_view(), name="otp-verify"),
    path("otp/resend/", views.OtpResendView.as_view(), name="otp-resend"),
    path("refresh/", views.RefreshView.as_view(), name="refresh"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("me/avatar/", profile.AvatarView.as_view(), name="me-avatar"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path(
        "password/reset/",
        views.PasswordResetRequestView.as_view(),
        name="password-reset",
    ),
    path(
        "password/reset/confirm/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("password/change/", views.PasswordChangeView.as_view(), name="password-change"),
    # SSO
    path("providers/", sso_views.SsoProvidersView.as_view(), name="sso-providers"),
    path(
        "<str:provider>/authorize/",
        sso_views.SsoAuthorizeView.as_view(),
        name="sso-authorize",
    ),
    path(
        "<str:provider>/callback/",
        sso_views.SsoCallbackView.as_view(),
        name="sso-callback",
    ),
]

urlpatterns = [
    path("auth/", include((auth_patterns, "auth"))),
    path("admin/employees/bulk-upload/", views.EmployeeBulkUploadView.as_view(), name="admin-employee-bulk-upload"),
    path("", include(router.urls)),
]
