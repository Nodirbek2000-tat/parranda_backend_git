from django.db import transaction
from django.db.models import Count, F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import IsAdmin, role_required
from accounts.models import Role
from audit.models import log
from stock.models import Package, Product
from stock.serializers import product_json

from . import serializers as s
from . import services
from . import stages as chain
from .models import Case, CaseStage, CaseStatus, StageEvent, StageStatus
from .services import ChainError


def chain_response(exc: ChainError) -> Response:
    return Response({"detail": exc.detail, **exc.extra}, status=exc.status)


def _detail(case_id, request):
    case = get_object_or_404(services.visible_cases(request.user).select_related(
        "package", "owner_user", "created_by", "vet_user"), pk=case_id)
    return Response(s.case_detail(case, request.user))


class CaseListView(APIView):
    def get(self, request):
        user, params = request.user, request.query_params
        qs = services.my_cases(user) if params.get("mine") in ("1", "true") else services.visible_cases(user)
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("stage", "").isdigit():
            qs = qs.filter(current_stage=int(params["stage"]))
        if params.get("district"):
            # "Urgut tumani" also matches households saved as just "Urgut"
            name = params["district"].strip()
            base = name.removesuffix(" tumani").removesuffix(" shahri").strip()
            qs = qs.filter(Q(district__iexact=name) | Q(district__iexact=base))
        if params.get("search"):
            term = params["search"].strip()
            qs = qs.filter(Q(code__icontains=term) | Q(name__icontains=term) | Q(owner_full_name__icontains=term)
                           | Q(owner_phone__icontains=term) | Q(district__icontains=term) | Q(mfy__icontains=term))
        from .summary import summaries

        rows = list(qs.select_related("package").prefetch_related("stages")[:500])
        stats = summaries(rows)
        return Response([{**s.case_item(case), "summary": stats.get(case.pk)} for case in rows])

    def post(self, request):
        if request.user.role != Role.INSPECTOR:
            raise PermissionDenied("Yangi xonadonni faqat Inspektor kiritadi.")
        ser = s.CaseCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        try:
            case = services.create_case(
                request.user, data, package=data.pop("package"), conclusion=data.pop("conclusion"),
                photo_ids=data.pop("photo_ids"), document_ids=data.pop("document_ids"))
        except ChainError as exc:
            return chain_response(exc)
        response = _detail(case.pk, request)
        response.status_code = 201
        return response


class CaseDetailView(APIView):
    def get(self, request, pk):
        return _detail(pk, request)

    @transaction.atomic
    def delete(self, request, pk):
        """Soft delete (ADMIN / SUPERADMIN): hidden from every list; history rows keep pointing at it."""
        if request.user.role not in (Role.ADMIN, Role.SUPERADMIN):
            raise PermissionDenied("Xonadonni faqat Administrator o‘chira oladi.")
        case = get_object_or_404(Case.objects.select_for_update(of=("self",)), pk=pk)
        case.deleted_at = timezone.now()
        case.save(update_fields=["deleted_at"])
        log(request.user, "case.delete", case)
        return Response(status=204)


class StageActionView(APIView):
    action = ""

    def post(self, request, pk, n):
        get_object_or_404(services.visible_cases(request.user), pk=pk)
        try:
            if self.action == "submit":
                self._submit(request, pk, n)
            elif self.action == "approve":
                ser = s.NoteSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                services.approve(request.user, pk, n, note=ser.validated_data["note"])
            else:
                ser = s.ReasonSerializer(data=request.data)
                ser.is_valid(raise_exception=True)
                services.return_stage(request.user, pk, n, reason=ser.validated_data["reason"])
        except ChainError as exc:
            return chain_response(exc)
        return _detail(pk, request)

    @staticmethod
    def _submit(request, pk, n):
        ser = s.SubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        body = ser.validated_data
        household, package = None, None
        if n == 1:
            raw = body["data"]
            household_ser = s.HouseholdMixin(data=raw, partial=True)
            household_ser.is_valid(raise_exception=True)
            household = dict(household_ser.validated_data)
            if raw.get("package_id"):
                package = Package.objects.filter(pk=raw["package_id"]).first()
                if package is None:
                    raise ValidationError({"package_id": ["Paket topilmadi."]})
        services.submit(request.user, pk, n, data=body["data"], note=body["note"], photo_ids=body["photo_ids"],
                        document_ids=body["document_ids"], household=household, package=package)


class RejectView(APIView):
    def post(self, request, pk):
        get_object_or_404(services.visible_cases(request.user), pk=pk)
        ser = s.ReasonSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            services.reject(request.user, pk, reason=ser.validated_data["reason"])
        except ChainError as exc:
            return chain_response(exc)
        return _detail(pk, request)


class OwnerAccountView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, pk):
        user_id = request.data.get("user_id") if isinstance(request.data, dict) else None
        try:
            result = services.owner_account(request.user, pk, link_user_id=int(user_id) if user_id else None)
        except (TypeError, ValueError):
            raise ValidationError({"user_id": ["Butun son kiriting."]}) from None
        except ChainError as exc:
            return chain_response(exc)
        return Response(result)


class CasePdfView(APIView):
    def get(self, request, pk):
        from .pdf import case_pdf

        case = get_object_or_404(services.visible_cases(request.user).select_related("package", "created_by"), pk=pk)
        response = HttpResponse(case_pdf(case), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="dalolatnoma-{case.code}.pdf"'
        return response


class DashboardView(APIView):
    permission_classes = [role_required(Role.ADMIN, Role.DIRECTOR, Role.HR)]

    def get(self, request):
        now = timezone.now()
        open_stages = CaseStage.objects.filter(case__status=CaseStatus.IN_PROGRESS, number=F("case__current_stage"))
        counts = {
            row["number"]: row for row in open_stages.values("number").annotate(
                total=Count("id"),
                active=Count("id", filter=Q(status=StageStatus.ACTIVE)),
                submitted=Count("id", filter=Q(status=StageStatus.SUBMITTED)),
                returned=Count("id", filter=Q(status=StageStatus.RETURNED)),
                overdue=Count("id", filter=Q(status__in=services.OPEN, due_at__lt=now)),
            ).order_by()
        }
        per_stage = []
        for st in chain.STAGES:
            row = counts.get(st.number, {})
            per_stage.append({
                "number": st.number, "key": st.key, "title": st.title,
                **{k: row.get(k, 0) for k in ("total", "active", "submitted", "returned", "overdue")},
            })
        totals = Case.objects.aggregate(
            total=Count("id"),
            in_progress=Count("id", filter=Q(status=CaseStatus.IN_PROGRESS)),
            done=Count("id", filter=Q(status=CaseStatus.DONE)),
            rejected=Count("id", filter=Q(status=CaseStatus.REJECTED)),
        )
        low = [product_json(p) for p in Product.objects.all() if p.is_low]
        events = StageEvent.objects.select_related("actor", "case").order_by("-id")[:10]
        return Response({
            "per_stage": per_stage,
            "overdue": sum(row["overdue"] for row in per_stage),
            **totals,
            "low_stock": low,
            "events": [s.event_json(e, with_case=True) for e in events],
        })

