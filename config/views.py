from django.db import connection
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Public probe: 200 when the database answers, 503 otherwise."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            db_ok = True
        except Exception:  # any driver error = database unavailable
            db_ok = False
        body = {"status": "ok" if db_ok else "error", "db": "ok" if db_ok else "error",
                "time": timezone.now().isoformat()}
        return Response(body, status=200 if db_ok else 503)
