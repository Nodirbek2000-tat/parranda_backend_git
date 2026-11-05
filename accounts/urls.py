from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter(trailing_slash=False)
router.register("users", views.UserViewSet, basename="user")

urlpatterns = [
    path("auth/login", views.LoginView.as_view(), name="auth-login"),
    path("auth/refresh", views.RefreshView.as_view(), name="auth-refresh"),
    path("auth/telegram", views.TelegramAuthView.as_view(), name="auth-telegram"),
    path("auth/me", views.MeView.as_view(), name="auth-me"),
    path("auth/logout", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/change-password", views.ChangePasswordView.as_view(), name="auth-change-password"),
    *router.urls,
]
