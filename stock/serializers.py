from decimal import Decimal

from rest_framework import serializers

from .models import Category, Package, Product, Unit


def product_json(p: Product) -> dict:
    return {
        "id": p.pk, "name": p.name, "unit": p.unit, "category": p.category,
        "category_display": p.get_category_display(), "price": float(p.price), "balance": float(p.balance),
        "min_stock": float(p.min_stock), "is_low": p.is_low, "is_demo": p.is_demo,
    }


def move_json(m) -> dict:
    return {
        "id": m.pk, "product": {"id": m.product_id, "name": m.product.name, "unit": m.product.unit},
        "type": m.type, "type_display": m.get_type_display(), "qty": float(m.qty),
        "balance_after": float(m.balance_after),
        "case": {"id": m.case_id, "code": m.case.code} if m.case_id else None,
        "note": m.note,
        "created_by": {"id": m.created_by_id, "full_name": m.created_by.full_name} if m.created_by_id else None,
        "created_at": m.created_at,
    }


def package_json(pkg: Package) -> dict:
    return {
        "id": pkg.pk, "name": pkg.name, "area_sotix": float(pkg.area_sotix), "capacity_birds": pkg.capacity_birds,
        "price": float(pkg.price), "description": pkg.description, "is_demo": pkg.is_demo,
        "items": [
            {"product_id": i.product_id, "product_name": i.product.name, "unit": i.product.unit, "qty": float(i.qty)}
            for i in pkg.items.all()
        ],
    }


class ProductSerializer(serializers.ModelSerializer):
    unit = serializers.ChoiceField(choices=Unit.choices)
    category = serializers.ChoiceField(choices=Category.choices)

    class Meta:
        model = Product
        fields = ("name", "unit", "category", "price", "min_stock")
        extra_kwargs = {"price": {"min_value": 0}, "min_stock": {"min_value": 0}}


class StockInSerializer(serializers.Serializer):
    product_id = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), source="product")
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class PackageItemInput(serializers.Serializer):
    product_id = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all(), source="product")
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))


class PackageSerializer(serializers.ModelSerializer):
    items = PackageItemInput(many=True, required=False)

    class Meta:
        model = Package
        fields = ("name", "area_sotix", "capacity_birds", "price", "description", "items")
        extra_kwargs = {"price": {"min_value": 0}, "area_sotix": {"min_value": 0}}
