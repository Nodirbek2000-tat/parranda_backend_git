from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import role_required
from accounts.models import Role

from .models import AuditLog


class AuditListView(APIView):
    permission_classes = [role_required(Role.SUPERADMIN)]

    def get(self, request):
        qs = AuditLog.objects.select_related("actor")
        params = request.query_params
        if params.get("object_type"):
            qs = qs.filter(object_type=params["object_type"])
        if params.get("object_id"):
            qs = qs.filter(object_id=params["object_id"])
        if params.get("action"):
            qs = qs.filter(action__startswith=params["action"])
        if params.get("actor", "").isdigit():
            qs = qs.filter(actor_id=params["actor"])
        try:
            limit = max(1, min(int(params.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        return Response([
            {
                "id": row.pk, "action": row.action, "object_type": row.object_type, "object_id": row.object_id,
                "actor": {"id": row.actor_id, "full_name": row.actor.full_name, "role": row.actor.role}
                if row.actor_id else None,
                "before": row.before, "after": row.after, "created_at": row.created_at,
            }
            for row in qs[:limit]
        ])
