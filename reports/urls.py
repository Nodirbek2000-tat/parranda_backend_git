"""URLs of the reports app, included under ``/api/`` by ``config/urls.py`` (no trailing slashes)."""

from django.urls import path, re_path

from . import views

urlpatterns = [
    path("reports", views.ReportListView.as_view(), name="report-list"),
    re_path(r"^reports/(?P<key>[a-z_]{1,40})$", views.ReportDetailView.as_view(), name="report-detail"),
    path("households/regions", views.HouseholdRegionsView.as_view(), name="household-regions"),
]
