from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter(trailing_slash=False)
router.register("households", views.HouseholdViewSet, basename="household")
router.register("batches", views.BatchViewSet, basename="batch")
router.register("movements", views.MovementViewSet, basename="movement")
router.register("daily-reports", views.DailyReportViewSet, basename="daily-report")

urlpatterns = [
    path("my/batches/<id:pk>", views.MyBatchDetailView.as_view(), name="my-batch-detail"),
    *router.urls,
]
