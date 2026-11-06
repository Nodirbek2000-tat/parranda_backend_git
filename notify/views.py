from django.contrib.auth import authenticate
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import HasBotToken
from accounts.models import User
from accounts.serializers import user_json
from audit.models import log
from cases import stages as chain
from cases.models import CaseStage, VisitStatus
from cases.services import in_growing_period, my_stages_q

from . import services
from .models import Notification


def _limit(request, default, maximum):
    try:
        return max(1, min(int(request.query_params.get("limit", default)), maximum))
    except ValueError:
        return default


def notification_json(n: Notification) -> dict:
    return {"id": n.pk, "text": n.text, "button_path": n.button_path, "status": n.status,
            "created_at": n.created_at, "read_at": n.read_at}


class InboxView(APIView):
    def get(self, request):
        rows = Notification.objects.filter(user=request.user)[: _limit(request, 100, 500)]
        return Response([notification_json(n) for n in rows])


class ReadView(APIView):
    def post(self, request, pk):
        n = get_object_or_404(Notification, pk=pk, user=request.user)
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=["read_at"])
        return Response(notification_json(n))


# --- Bot API (X-Bot-Token) ----------------------------------------------------------------------

class BotView(APIView):
    authentication_classes = []
    permission_classes = [HasBotToken]
    throttle_classes = []


def _linked_user(telegram_id) -> User:
    return get_object_or_404(User.objects.alive().filter(is_active=True), telegram_id=telegram_id)


class LinkSerializer(serializers.Serializer):
    telegram_id = serializers.IntegerField(min_value=1)
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)


class BotLinkView(BotView):
    @transaction.atomic
    def post(self, request):
        ser = LinkSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        user = authenticate(username=d["username"].strip(), password=d["password"])
        if user is None or user.deleted_at is not None:
            return Response({"detail": "Login yoki parol noto‘g‘ri"}, status=401)
        # One Telegram account belongs to one user: move it if it was linked elsewhere.
        User.objects.filter(telegram_id=d["telegram_id"]).exclude(pk=user.pk).update(telegram_id=None)
        user.telegram_id = d["telegram_id"]
        user.save(update_fields=["telegram_id"])
        log(user, "telegram.link", user, after={"telegram_id": d["telegram_id"]})
        return Response(user_json(user))


class BotUnlinkView(BotView):
    def post(self, request):
        ser = serializers.IntegerField(min_value=1)
        try:
            telegram_id = ser.run_validation(request.data.get("telegram_id"))
        except serializers.ValidationError as exc:
            return Response({"telegram_id": exc.detail}, status=400)
        user = get_object_or_404(User, telegram_id=telegram_id)
        user.telegram_id = None
        user.token_version += 1  # Web App sessions opened from Telegram end too
        user.save(update_fields=["telegram_id", "token_version"])
        log(user, "telegram.unlink", user)
        return Response({"detail": "Telegram uzildi"})


class BotMeView(BotView):
    def get(self, request, telegram_id):
        return Response(user_json(_linked_user(telegram_id)))


class BotTasksView(BotView):
    def get(self, request, telegram_id):
        user = _linked_user(telegram_id)
        stages = (CaseStage.objects.filter(my_stages_q(user)).select_related("case")
                  .order_by("due_at", "case_id")[:50])
        items = [
            {
                "kind": "case", "id": st.case_id, "code": st.case.code, "name": st.case.name,
                "title": f"{st.number}. {chain.get(st.number).title}",
                "stage_number": st.number, "stage_title": chain.get(st.number).title,
                "stage_status": st.status, "stage_status_display": st.get_status_display(),
                "action": "approve" if st.status == "SUBMITTED" else "submit",
                "due_at": st.due_at, "is_overdue": st.is_overdue, "path": f"/cases/{st.case_id}",
            }
            for st in stages
        ]
        return Response(items + _request_tasks(user) + _visit_tasks(user))


def _request_tasks(user) -> list[dict]:
    from cases.growing import request_can, visible_requests
    from cases.models import RequestStatus

    open_statuses = (RequestStatus.PENDING_LOGISTICS, RequestStatus.PENDING_ADMIN, RequestStatus.APPROVED,
                     RequestStatus.DELIVERED)
    rows = visible_requests(user).filter(status__in=open_statuses).select_related("case").order_by("id")[:50]
    items, growing = [], {}
    for req in rows:
        if req.case_id not in growing:
            growing[req.case_id] = in_growing_period(req.case)
        can = request_can(user, req, growing=growing[req.case_id])
        action = next((name for name in ("deliver", "confirm", "approve") if can[name]), None)
        if action is None:
            continue
        items.append({
            "kind": "request", "id": req.pk, "case_id": req.case_id, "code": req.case.code, "name": req.case.name,
            "title": f"{req.get_kind_display()} so‘rovi #{req.pk}", "status": req.status,
            "status_display": req.get_status_display(), "action": action, "due_at": None, "is_overdue": False,
            "path": f"/requests/{req.pk}",
        })
    return items


def _visit_tasks(user) -> list[dict]:
    from cases.models import VetVisit

    rows = (VetVisit.objects.filter(vet=user, status=VisitStatus.PLANNED, case__deleted_at__isnull=True)
            .select_related("case").order_by("planned_date", "id")[:50])
    return [
        {
            "kind": "visit", "id": v.pk, "case_id": v.case_id, "code": v.case.code, "name": v.case.name,
            "title": f"Veterinar tashrifi: {v.get_kind_display()}" + (f" ({v.week}-hafta)" if v.week else ""),
            "status": v.status, "status_display": v.get_status_display(), "planned_date": v.planned_date,
            "due_at": None, "is_overdue": v.is_overdue, "path": f"/visits/{v.pk}",
        }
        for v in rows
    ]


class BotPendingView(BotView):
    def get(self, request):
        rows = services.pending_for_bot(_limit(request, 50, 100))
        return Response([{"id": n.pk, "chat_id": n.user.telegram_id, "text": n.text, "button_path": n.button_path}
                         for n in rows])


class ResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="")


class BotResultView(BotView):
    def post(self, request, pk):
        ser = ResultSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            n = services.record_result(pk, ok=ser.validated_data["ok"], error=ser.validated_data["error"] or "")
        except Notification.DoesNotExist:
            return Response({"detail": "Bildirishnoma topilmadi"}, status=404)
        return Response({"id": n.pk, "status": n.status, "attempts": n.attempts})

