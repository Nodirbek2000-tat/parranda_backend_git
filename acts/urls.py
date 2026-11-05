"""URLs of the acts app, included under ``/api/`` by ``config/urls.py`` (no trailing slashes)."""

from django.urls import path

from . import views

urlpatterns = [
    path("acts", views.ActListView.as_view(), name="acts"),
    path("acts/preview", views.ActPreviewView.as_view(), name="acts-preview"),
    path("acts/<id:pk>", views.ActDetailView.as_view(), name="act"),
    path("acts/<id:pk>/pdf", views.ActPdfView.as_view(), name="act-pdf"),
]
