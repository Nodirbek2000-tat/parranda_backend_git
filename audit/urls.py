from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter(trailing_slash=False)
router.register("audit-logs", views.AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls
