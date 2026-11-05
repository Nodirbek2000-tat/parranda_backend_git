"""URLs of the vet app, included under ``/api/`` by ``config/urls.py`` (no trailing slashes)."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter(trailing_slash=False)
router.register("vet/visits", views.VetVisitViewSet, basename="vet-visit")
router.register("vet/vaccinations", views.VaccinationViewSet, basename="vet-vaccination")
router.register("vet/diseases", views.DiseaseViewSet, basename="vet-disease")
router.register("vet/treatments", views.TreatmentViewSet, basename="vet-treatment")
router.register("vet/requests", views.VetRequestViewSet, basename="vet-request")

urlpatterns = [
    path("vet/calendar", views.VetCalendarView.as_view(), name="vet-calendar"),
    path("vet/summary", views.VetSummaryView.as_view(), name="vet-summary"),
    path("my/vet", views.MyVetView.as_view(), name="my-vet"),
    *router.urls,
]
