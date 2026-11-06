"""v2.1 API: owner requests, vet assignment and vet visits (shapes: docs/API_V2_NOTES.md "v2.1 additions")."""

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import IsAdmin

from . import growing
from . import serializers as s
from . import services
from .models import VisitStatus
from .services import ChainError
from .views import _detail, chain_response


def _request_response(request, pk, status=200):
    req = get_object_or_404(s.request_queryset(growing.visible_requests(request.user)), pk=pk)
    return Response(s.request_json(req, request.user), status=status)


def _visit_response(request, pk):
    visit = get_object_or_404(s.visit_queryset(growing.visible_visits(request.user)), pk=pk)
    return Response(s.visit_json(visit, request.user))


class RequestListView(APIView):
    def get(self, request):
        params, user = request.query_params, request.user
        qs = growing.visible_requests(user)
        if params.get("status"):
            qs = qs.filter(status__in=[v.strip() for v in params["status"].split(",") if v.strip()])
        if params.get("kind"):
            qs = qs.filter(kind=params["kind"])
        if params.get("case", "").isdigit():
            qs = qs.filter(case_id=int(params["case"]))
        rows = list(s.request_queryset(qs)[:500])
        growing_cache: dict[int, bool] = {}
        items = []
        for req in rows:
            if req.case_id not in growing_cache:
                growing_cache[req.case_id] = services.in_growing_period(req.case)
            items.append(s.request_json(req, user, growing=growing_cache[req.case_id]))
        if params.get("mine") in ("1", "true"):
            items = [item for item in items if any(item["can"].values())]
        return Response(items)


class RequestDetailView(APIView):
    def get(self, request, pk):
        return _request_response(request, pk)


class CaseRequestCreateView(APIView):
    def post(self, request, pk):
        get_object_or_404(services.visible_cases(request.user), pk=pk)
        ser = s.RequestCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            req = growing.create_request(request.user, pk, kind=d["kind"], text=d["text"], qty_kg=d["qty_kg"],
                                         photo_ids=d["photo_ids"])
        except ChainError as exc:
            return chain_response(exc)
        return _request_response(request, req.pk, status=201)


class RequestActionView(APIView):
    action = ""

    def post(self, request, pk):
        get_object_or_404(growing.visible_requests(request.user), pk=pk)
        user = request.user
        try:
            if self.action == "approve":
                ser = s.NoteSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                growing.approve_request(user, pk, note=ser.validated_data["note"])
            elif self.action == "reject":
                ser = s.ReasonSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                growing.reject_request(user, pk, reason=ser.validated_data["reason"])
            elif self.action == "deliver":
                ser = s.DeliverSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                d = ser.validated_data
                growing.deliver_request(user, pk, product_id=d["product_id"], qty=d["qty"],
                                        photo_ids=d["photo_ids"], note=d["note"])
            else:
                growing.confirm_request(user, pk)
        except ChainError as exc:
            return chain_response(exc)
        return _request_response(request, pk)


class AssignVetView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        get_object_or_404(services.visible_cases(request.user), pk=pk)
        ser = s.AssignVetSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            growing.assign_vet(request.user, pk, ser.validated_data["vet_id"])
        except ChainError as exc:
            return chain_response(exc)
        return _detail(pk, request)


class VisitListView(APIView):
    def get(self, request):
        params, user = request.query_params, request.user
        qs = growing.visible_visits(user)
        if params.get("status"):
            qs = qs.filter(status__in=[v.strip() for v in params["status"].split(",") if v.strip()])
        if params.get("case", "").isdigit():
            qs = qs.filter(case_id=int(params["case"]))
        if params.get("mine") in ("1", "true"):
            qs = qs.filter(vet=user, status=VisitStatus.PLANNED)
        rows = list(s.visit_queryset(qs).order_by("-id")[:500])
        planned = sorted((v for v in rows if v.status == VisitStatus.PLANNED), key=lambda v: (v.planned_date, v.pk))
        rest = [v for v in rows if v.status != VisitStatus.PLANNED]
        return Response([s.visit_json(v, user) for v in planned + rest])


class VisitDetailView(APIView):
    def get(self, request, pk):
        return _visit_response(request, pk)


class VisitActionView(APIView):
    action = ""

    def post(self, request, pk):
        get_object_or_404(growing.visible_visits(request.user), pk=pk)
        try:
            if self.action == "complete":
                ser = s.VisitCompleteSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                d = ser.validated_data
                growing.complete_visit(request.user, pk, chicks_count=d["chicks_count"], health=d["health"],
                                       note=d["note"], photo_ids=d["photo_ids"], drugs=d["drugs"])
            else:
                ser = s.ReasonSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                growing.cancel_visit(request.user, pk, reason=ser.validated_data["reason"])
        except ChainError as exc:
            return chain_response(exc)
        return _visit_response(request, pk)
