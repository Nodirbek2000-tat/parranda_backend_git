from django.urls import path

from . import views

urlpatterns = [
    path("settings", views.SettingsView.as_view(), name="settings"),
    path("files", views.FileUploadView.as_view(), name="file-upload"),
    path("files/<id:pk>", views.FileDownloadView.as_view(), name="file-download"),
    path("files/<id:pk>/meta", views.FileMetaView.as_view(), name="file-meta"),
    path("search", views.SearchView.as_view(), name="search"),
    path("system/wipe", views.SystemWipeView.as_view(), name="system-wipe"),
]
