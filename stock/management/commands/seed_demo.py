"""Idempotent DEMO data: products with opening stock, 3 packages (1/2/3 sotix), one test account per role.

No households/cases are created. Logins and passwords are written to test_accounts.txt (gitignored).
Running again keeps existing data and the passwords already listed in the file.
"""

import secrets
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Role, User
from stock.models import Category, Package, PackageItem, Product, StockMove, Unit

# name, category, unit, DEMO price (so‘m), opening stock, min stock, qty in the 1-sotix package
PRODUCTS = [
    ("Sement M400 (50 kg)", Category.MATERIAL, Unit.QOP, 65_000, 300, 30, 10),
    ("G‘isht (pishiq)", Category.MATERIAL, Unit.DONA, 1_200, 20_000, 2_000, 1_000),
    ("Yog‘och brus 50×100", Category.MATERIAL, Unit.M, 18_000, 1_500, 150, 60),
    ("Profnastil tom (C8)", Category.MATERIAL, Unit.M2, 75_000, 600, 60, 25),
    ("Setka (to‘r) 1,5 m", Category.MATERIAL, Unit.M, 9_000, 1_000, 100, 40),
    ("Mix (aralash)", Category.MATERIAL, Unit.KG, 22_000, 200, 20, 5),
    ("Oziqlantirgich (bunker)", Category.JIHOZ, Unit.DONA, 85_000, 300, 30, 3),
    ("Suv ichgich (nipel tizimi, 1 m)", Category.JIHOZ, Unit.DONA, 60_000, 300, 30, 3),
    ("Isitish lampasi (IK, 250 W)", Category.JIHOZ, Unit.DONA, 45_000, 200, 20, 2),
    ("Termometr-gigrometr", Category.JIHOZ, Unit.DONA, 35_000, 100, 10, 1),
    ("Start yem (jo‘ja, 0–10 kun)", Category.YEM, Unit.QOP, 320_000, 200, 20, 4),
    ("O‘sish yemi (11–24 kun)", Category.YEM, Unit.QOP, 300_000, 300, 30, 8),
    ("Vitamin kompleksi", Category.DORI, Unit.LITR, 120_000, 50, 5, 1),
    ("Dezinfeksiya vositasi", Category.DORI, Unit.LITR, 90_000, 60, 6, 1),
    ("Vaksina (Nyukasl kasalligi)", Category.DORI, Unit.DONA, 25_000, 200, 20, 2),
]

# sotix, capacity (birds), DEMO price (so‘m)
PACKAGES = [(1, 100, 12_000_000), (2, 200, 22_000_000), (3, 300, 32_000_000)]

ACCOUNTS = [
    (Role.SUPERADMIN, "superadmin"), (Role.ADMIN, "admin"), (Role.INSPECTOR, "inspector"), (Role.HR, "hr"),
    (Role.DIRECTOR, "director"), (Role.BUILDER, "builder"), (Role.WAREHOUSE, "warehouse"),
    (Role.INCUBATOR, "incubator"), (Role.LOGISTICS, "logistics"), (Role.VET, "vet"), (Role.OWNER, "owner"),
    (Role.ACCOUNTANT, "accountant"), (Role.SLAUGHTERHOUSE, "slaughterhouse"),
]


def package_name(sotix: int) -> str:
    return f"{sotix} sotixlik jo‘jaxona (DEMO)"


class Command(BaseCommand):
    help = "DEMO mahsulotlar, paketlar va har rol uchun test hisobi (xonadonlarsiz). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(Path(settings.BASE_DIR) / "test_accounts.txt"))
        parser.add_argument("--no-accounts", action="store_true", help="Test hisoblarini yaratmaslik")

    def handle(self, *args, output, no_accounts, **options):
        with transaction.atomic():
            products = self._products()
            self._packages(products)
        if not no_accounts:
            self._accounts(Path(output))
        self.stdout.write(self.style.SUCCESS("DEMO ma’lumotlar tayyor (xonadonlar yaratilmadi)."))

    def _products(self) -> dict[str, Product]:
        result = {}
        for name, category, unit, price, opening, min_stock, _ in PRODUCTS:
            product, created = Product.objects.get_or_create(
                name=name, defaults={"category": category, "unit": unit, "price": price, "min_stock": min_stock,
                                     "is_demo": True})
            if created and opening:
                product.balance = Decimal(opening)
                product.save(update_fields=["balance"])
                StockMove.objects.create(product=product, type=StockMove.IN, qty=opening, balance_after=opening,
                                         note="DEMO boshlang‘ich qoldiq")
            result[name] = product
        return result

    def _packages(self, products):
        for sotix, capacity, price in PACKAGES:
            package, created = Package.objects.get_or_create(
                name=package_name(sotix),
                defaults={"area_sotix": sotix, "capacity_birds": capacity, "price": price, "is_demo": True,
                          "description": f"DEMO: {sotix} sotixlik jo‘jaxona, {capacity} ta parranda uchun "
                                         "qurilish materiallari, jihozlar, yem va dorilar."})
            if created:
                PackageItem.objects.bulk_create([
                    PackageItem(package=package, product=products[name], qty=per_sotix * sotix)
                    for name, *_rest, per_sotix in PRODUCTS
                ])

    def _accounts(self, path: Path):
        known = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] in {u for _, u in ACCOUNTS}:
                    known[parts[0]] = parts[2]
        lines = ["# NNP test hisoblari (DEMO). Faylni hech kimga yubormang, gitga qo‘shmang.",
                 "# login            rol           parol", ""]
        for role, username in ACCOUNTS:
            user = User.objects.filter(username=username).first()
            if user is None:
                password = secrets.token_urlsafe(9)
                user = User(username=username, role=role, full_name=f"Test {role.label} (DEMO)")
                user.set_password(password)
                user.save()
            else:  # an existing account is never touched (its password may be real)
                password = known.get(username)
                if password is None or not user.check_password(password):
                    password = "(mavjud hisob, parol o‘zgartirilmadi)"
            lines.append(f"{username:<18} {role.value:<13} {password}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.stdout.write(f"Test hisoblari: {path}")
