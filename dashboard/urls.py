from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/admin", views.AdminDashboardView.as_view(), name="dashboard-admin"),
    path("dashboard/owner", views.OwnerDashboardView.as_view(), name="dashboard-owner"),
]
