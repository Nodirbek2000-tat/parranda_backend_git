import io
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Role, User
from audit.models import AuditLog
from cases.models import Case
from config.testutils import NNPTestCase

from .models import Package, PackageItem, Product, StockMove


class SeedTests(TestCase):
    def test_seed_is_idempotent_and_creates_no_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_accounts.txt"
            call_command("seed_demo", output=str(out), stdout=io.StringIO())
            counts = (Product.objects.count(), StockMove.objects.count(), Package.objects.count(),
                      PackageItem.objects.count(), User.objects.count())
            first = out.read_text(encoding="utf-8")
            call_command("seed_demo", output=str(out), stdout=io.StringIO())
            self.assertEqual(counts, (Product.objects.count(), StockMove.objects.count(), Package.objects.count(),
                                      PackageItem.objects.count(), User.objects.count()))
            self.assertEqual(first, out.read_text(encoding="utf-8"))  # passwords kept
            self.assertEqual(Case.objects.count(), 0)
            self.assertEqual(Package.objects.count(), 3)
            self.assertEqual(sorted(float(p.area_sotix) for p in Package.objects.all()), [1, 2, 3])
            self.assertEqual(set(User.objects.values_list("role", flat=True)), set(Role.values))
            self.assertTrue(all(p.is_demo for p in Product.objects.all()))
            self.assertTrue(all(p.balance > 0 for p in Product.objects.all()))
            line = next(row for row in first.splitlines() if row.startswith("inspector "))
            self.assertTrue(User.objects.get(username="inspector").check_password(line.split()[2]))
            self.assertTrue(User.objects.get(username="superadmin").is_superuser)


class StockApiTests(NNPTestCase):
    def test_permissions(self):
        self.assertEqual(self.client_for(Role.INSPECTOR).get("/api/products").status_code, 403)
        self.assertEqual(self.client_for(Role.WAREHOUSE).get("/api/products").status_code, 200)
        self.assertEqual(self.client_for(Role.OWNER).get("/api/packages").status_code, 200)
        self.assertEqual(self.client_for(Role.WAREHOUSE).patch(f"/api/packages/{self.package.pk}", {},
                                                               format="json").status_code, 403)

    def test_product_create_patch_and_stock_in(self):
        client = self.client_for(Role.WAREHOUSE)
        r = client.post("/api/products", {"name": "Mix", "unit": "kg", "category": "material", "price": 1000,
                                          "min_stock": 5}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        pid = r.json()["id"]
        self.assertEqual((r.json()["balance"], r.json()["is_low"]), (0, True))
        r = client.patch(f"/api/products/{pid}", {"price": 1200, "balance": 999}, format="json")
        self.assertEqual((r.json()["price"], r.json()["balance"]), (1200, 0))  # balance is not editable
        r = client.post("/api/stock/in", {"product_id": pid, "qty": "12.5", "note": "Kirim"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["product"]["balance"], 12.5)
        self.assertEqual(r.json()["move"]["balance_after"], 12.5)
        self.assertEqual(client.post("/api/stock/in", {"product_id": pid, "qty": 0}, format="json").status_code, 400)
        moves = client.get(f"/api/stock/moves?product={pid}").json()
        self.assertEqual(len(moves), 1)
        self.assertTrue(AuditLog.objects.filter(action="stock.in").exists())

    def test_package_patch_replaces_items(self):
        r = self.client_for(Role.ADMIN).patch(
            f"/api/packages/{self.package.pk}",
            {"price": 5000, "items": [{"product_id": self.product_a.pk, "qty": 3}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["price"], 5000)
        self.assertEqual([(i["product_id"], i["qty"]) for i in r.json()["items"]], [(self.product_a.pk, 3.0)])
        packages = self.client_for(Role.INSPECTOR).get("/api/packages").json()
        self.assertEqual(packages[0]["items"][0]["product_name"], "Sement")


class AuditTests(NNPTestCase):
    def test_audit_is_superadmin_only_and_immutable(self):
        self.create_case()
        self.assertEqual(self.client_for(Role.ADMIN).get("/api/audit").status_code, 403)
        rows = self.client_for(Role.SUPERADMIN).get("/api/audit").json()
        self.assertIn("case.create", [r["action"] for r in rows])
        row = AuditLog.objects.first()
        with self.assertRaises(ValueError):
            row.save()
        with self.assertRaises(ValueError):
            row.delete()
