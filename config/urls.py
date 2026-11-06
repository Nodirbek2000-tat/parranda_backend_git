from django.contrib import admin
from django.urls import path

from accounts import views as acc
from audit.views import AuditListView
from cases import files
from cases import views as cases
from notify import views as notify
from stock import views as stock

from .views import HealthView

admin.site.site_header = "NNP — boshqaruv"
admin.site.site_title = "NNP"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health", HealthView.as_view()),
    # Auth
    path("api/auth/login", acc.LoginView.as_view()),
    path("api/auth/refresh", acc.RefreshView.as_view()),
    path("api/auth/logout", acc.LogoutView.as_view()),
    path("api/auth/telegram", acc.TelegramAuthView.as_view()),
    path("api/auth/change-password", acc.ChangePasswordView.as_view()),
    path("api/auth/me", acc.MeView.as_view()),
    # Users
    path("api/users", acc.UserListView.as_view()),
    path("api/users/<int:pk>", acc.UserDetailView.as_view()),
    path("api/users/<int:pk>/reset-password", acc.ResetPasswordView.as_view()),
    # Cases
    path("api/cases", cases.CaseListView.as_view()),
    path("api/cases/<int:pk>", cases.CaseDetailView.as_view()),
    path("api/cases/<int:pk>/stages/<int:n>/submit", cases.StageActionView.as_view(action="submit")),
    path("api/cases/<int:pk>/stages/<int:n>/approve", cases.StageActionView.as_view(action="approve")),
    path("api/cases/<int:pk>/stages/<int:n>/return", cases.StageActionView.as_view(action="return")),
    path("api/cases/<int:pk>/reject", cases.RejectView.as_view()),
    path("api/cases/<int:pk>/owner-account", cases.OwnerAccountView.as_view()),
    path("api/cases/<int:pk>/pdf", cases.CasePdfView.as_view()),
    path("api/dashboard", cases.DashboardView.as_view()),
    # Files
    path("api/files/photo", files.PhotoUploadView.as_view()),
    path("api/files/document", files.DocumentUploadView.as_view()),
    path("api/files/<str:kind>/<int:pk>", files.FileView.as_view()),
    # Stock
    path("api/products", stock.ProductListView.as_view()),
    path("api/products/<int:pk>", stock.ProductDetailView.as_view()),
    path("api/stock/in", stock.StockInView.as_view()),
    path("api/stock/moves", stock.StockMovesView.as_view()),
    path("api/packages", stock.PackageListView.as_view()),
    path("api/packages/<int:pk>", stock.PackageDetailView.as_view()),
    # Notifications and audit
    path("api/notifications", notify.InboxView.as_view()),
    path("api/notifications/<int:pk>/read", notify.ReadView.as_view()),
    path("api/audit", AuditListView.as_view()),
    # Bot API (X-Bot-Token)
    path("api/bot/link", notify.BotLinkView.as_view()),
    path("api/bot/unlink", notify.BotUnlinkView.as_view()),
    path("api/bot/me/<int:telegram_id>", notify.BotMeView.as_view()),
    path("api/bot/tasks/<int:telegram_id>", notify.BotTasksView.as_view()),
    path("api/bot/notifications/pending", notify.BotPendingView.as_view()),
    path("api/bot/notifications/<int:pk>/result", notify.BotResultView.as_view()),
]
