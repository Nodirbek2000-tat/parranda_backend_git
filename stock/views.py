from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.auth import IsAdmin, role_required
from accounts.models import Role
from audit.models import log
from config.softdelete import Conflict

from . import services
from .models import Package, PackageItem, Product, StockMove
from .serializers import PackageSerializer, ProductSerializer, StockInSerializer, move_json, package_json, product_json

IsStockRole = role_required(Role.WAREHOUSE, Role.ADMIN)
_ProductReaders = role_required(Role.WAREHOUSE, Role.ADMIN, Role.LOGISTICS, Role.VET)


class CanReadProducts(_ProductReaders):
    """GET: also LOGISTICS (feed picker) and VET (drug picker); writes stay WAREHOUSE/ADMIN."""

    def has_permission(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return super().has_permission(request, view)
        return IsStockRole().has_permission(request, view)


def _limit(request, default=200, maximum=1000):
    try:
        return max(1, min(int(request.query_params.get("limit", default)), maximum))
    except ValueError:
        return default


class ProductListView(APIView):
    permission_classes = [CanReadProducts]

    def get(self, request):
        qs = Product.objects.all()
        if request.query_params.get("category"):
            qs = qs.filter(category=request.query_params["category"])
        if request.query_params.get("search"):
            qs = qs.filter(name__icontains=request.query_params["search"])
        return Response([product_json(p) for p in qs])

    def post(self, request):
        ser = ProductSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        product = ser.save()
        log(request.user, "product.create", product, after=product_json(product))
        return Response(product_json(product), status=201)


class ProductDetailView(APIView):
    permission_classes = [IsStockRole]

    @transaction.atomic
    def delete(self, request, pk):
        product = get_object_or_404(Product.objects.select_for_update(of=("self",)), pk=pk)
        packages = list(Package.objects.filter(items__product=product).values_list("name", flat=True))
        if packages:
            raise Conflict(f"Bu mahsulot paket tarkibida bor ({', '.join(packages)}). Avval uni paketdan olib tashlang.")
        product.deleted_at = timezone.now()
        product.name = f"{product.name} (o‘chirilgan #{product.pk})"[:200]  # frees the unique name
        product.save(update_fields=["deleted_at", "name"])
        log(request.user, "product.delete", product)
        return Response(status=204)

    def patch(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        before = product_json(product)
        ser = ProductSerializer(product, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        product = ser.save()
        log(request.user, "product.update", product, before=before, after=product_json(product))
        return Response(product_json(product))


class StockInView(APIView):
    permission_classes = [IsStockRole]

    def post(self, request):
        ser = StockInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        move = services.stock_in(d["product"].pk, Decimal(d["qty"]), user=request.user, note=d["note"])
        move = StockMove.objects.select_related("product", "case", "created_by").get(pk=move.pk)
        return Response({"move": move_json(move), "product": product_json(move.product)}, status=201)


class StockMovesView(APIView):
    permission_classes = [IsStockRole]

    def get(self, request):
        qs = StockMove.objects.select_related("product", "case", "created_by")
        params = request.query_params
        if params.get("product", "").isdigit():
            qs = qs.filter(product_id=params["product"])
        if params.get("case", "").isdigit():
            qs = qs.filter(case_id=params["case"])
        if params.get("type") in (StockMove.IN, StockMove.OUT):
            qs = qs.filter(type=params["type"])
        return Response([move_json(m) for m in qs[: _limit(request)]])


class PackageListView(APIView):
    def get(self, request):
        return Response([package_json(p) for p in Package.objects.prefetch_related("items__product")])


class PackageDetailView(APIView):
    permission_classes = [IsAdmin]

    @transaction.atomic
    def delete(self, request, pk):
        from cases.models import Case, CaseStatus

        package = get_object_or_404(Package.objects.select_for_update(of=("self",)), pk=pk)
        busy = list(Case.objects.filter(package=package, status=CaseStatus.IN_PROGRESS).values_list("code", flat=True)[:5])
        if busy:
            raise Conflict(f"Bu paket jarayondagi xonadonlarda ishlatilmoqda ({', '.join(busy)}). Avval ularni yakunlang.")
        package.deleted_at = timezone.now()
        package.name = f"{package.name} (o‘chirilgan #{package.pk})"[:200]
        package.save(update_fields=["deleted_at", "name"])
        log(request.user, "package.delete", package)
        return Response(status=204)

    def get(self, request, pk):
        return Response(package_json(get_object_or_404(Package, pk=pk)))

    @transaction.atomic
    def patch(self, request, pk):
        package = get_object_or_404(Package, pk=pk)
        before = package_json(package)
        ser = PackageSerializer(package, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data.pop("items", None)
        for key, value in ser.validated_data.items():
            setattr(package, key, value)
        package.save()
        if items is not None:  # full replacement of the item list
            package.items.all().delete()
            merged = {}
            for item in items:
                merged[item["product"].pk] = merged.get(item["product"].pk, 0) + item["qty"]
            PackageItem.objects.bulk_create([PackageItem(package=package, product_id=pid, qty=q)
                                             for pid, q in merged.items()])
        after = package_json(package)
        log(request.user, "package.update", package, before=before, after=after)
        return Response(after)
