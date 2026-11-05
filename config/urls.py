"""Root URLs. Every API route lives under ``/api/`` and has no trailing slash."""

from django.contrib import admin
from django.urls import include, path

from common.health import DocsView, HealthView, SchemaView
from common.ids import register_id_converter

register_id_converter()  # ``<id:pk>`` for every app below

# Module teams add routes ONLY in their own <app>/urls.py; this list is complete for API_CONTRACT_V3.
api_patterns = [
    path("health", HealthView.as_view(), name="api-health"),
    path("schema", SchemaView.as_view(), name="api-schema"),
    path("docs", DocsView.as_view(), name="api-docs"),
    path("", include("accounts.urls")),
    path("", include("farm.urls")),
    path("", include("warehouse.urls")),
    path("", include("notifications.urls")),
    path("", include("dashboard.urls")),
    path("", include("stats.urls")),
    path("", include("audit.urls")),
    # V3 apps (API_CONTRACT_V3 §0.2)
    path("", include("core.urls")),
    path("", include("orders.urls")),
    path("", include("logistics.urls")),
    path("", include("vet.urls")),
    path("", include("reports.urls")),
]

# Unknown URLs and unhandled errors also answer in JSON (used when DEBUG is off).
handler404 = "common.views.not_found"
handler500 = "common.views.server_error"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(api_patterns)),
]
