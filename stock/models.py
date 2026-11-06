from django.conf import settings
from django.db import models

from config.softdelete import AliveManager


class Unit(models.TextChoices):
    KG = "kg", "kg"
    DONA = "dona", "dona"
    QOP = "qop", "qop"
    M = "m", "m"
    M2 = "m²", "m²"
    LITR = "litr", "litr"


class Category(models.TextChoices):
    MATERIAL = "material", "Qurilish materiali"
    JIHOZ = "jihoz", "Jihoz"
    YEM = "yem", "Yem"
    DORI = "dori", "Veterinariya dori"
    BOSHQA = "boshqa", "Boshqa"


class Product(models.Model):
    name = models.CharField("Nomi", max_length=200, unique=True)
    unit = models.CharField("O‘lchov birligi", max_length=10, choices=Unit.choices, default=Unit.DONA)
    category = models.CharField("Turkum", max_length=20, choices=Category.choices, default=Category.BOSHQA)
    price = models.DecimalField("Narxi (so‘m)", max_digits=14, decimal_places=2, default=0)
    # Changed only by StockMove (stock.services), never edited directly.
    balance = models.DecimalField("Qoldiq", max_digits=14, decimal_places=3, default=0)
    min_stock = models.DecimalField("Minimal qoldiq", max_digits=14, decimal_places=3, default=0)
    is_demo = models.BooleanField("DEMO", default=False)
    deleted_at = models.DateTimeField("O‘chirilgan", null=True, blank=True)

    objects = AliveManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["category", "name"]
        verbose_name = "Mahsulot"
        verbose_name_plural = "Mahsulotlar"

    def __str__(self) -> str:
        return self.name

    @property
    def is_low(self) -> bool:
        return self.balance <= self.min_stock


class StockMove(models.Model):
    IN = "IN"
    OUT = "OUT"
    TYPES = [(IN, "Kirim"), (OUT, "Chiqim")]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="moves")
    type = models.CharField(max_length=3, choices=TYPES)
    qty = models.DecimalField(max_digits=14, decimal_places=3)
    balance_after = models.DecimalField(max_digits=14, decimal_places=3)
    case = models.ForeignKey("cases.Case", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_moves")
    note = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "Ombor harakati"
        verbose_name_plural = "Ombor harakatlari"

    def __str__(self) -> str:
        return f"{self.get_type_display()} {self.product} {self.qty}"


class Package(models.Model):
    name = models.CharField("Nomi", max_length=200, unique=True)
    area_sotix = models.DecimalField("Maydon (sotix)", max_digits=6, decimal_places=2, default=1)
    capacity_birds = models.PositiveIntegerField("Sig‘im (parranda)", default=0)
    price = models.DecimalField("Narxi (so‘m)", max_digits=14, decimal_places=2, default=0)
    description = models.TextField("Tavsif", blank=True)
    is_demo = models.BooleanField("DEMO", default=False)
    deleted_at = models.DateTimeField("O‘chirilgan", null=True, blank=True)

    objects = AliveManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["area_sotix", "id"]
        verbose_name = "Paket"
        verbose_name_plural = "Paketlar"

    def __str__(self) -> str:
        return self.name


class PackageItem(models.Model):
    package = models.ForeignKey(Package, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="package_items")
    qty = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        unique_together = [("package", "product")]
        verbose_name = "Paket tarkibi"
        verbose_name_plural = "Paket tarkibi"

    def __str__(self) -> str:
        return f"{self.product} × {self.qty}"
