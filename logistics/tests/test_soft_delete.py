"""Soft delete of households, products, vehicles and drivers (DELETE on the detail routes).

The row stays for history, disappears from lists / details / search / pickers / statistics, frees its unique
value and is refused (409) while running work still needs it.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.utils import timezone

from accounts.models import User
from audit.models import AuditAction, AuditLog
from farm.models import Household, PoultryMovement
from logistics.models import Driver, LogisticsTask, Vehicle
from orders.models import OrderKind
from reports.registry import REPORTS
from warehouse.models import Product, StockTransaction

from .helpers import LogisticsTestCase


def _ids(response) -> list[int]:
    data = response.data
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    return [row["id"] for row in rows]


def _search_ids(response, group: str) -> list[int]:
    for item in response.data["groups"]:
        if item["type"] == group:
            return [row["id"] for row in item["items"]]
    return []


class HouseholdDeleteTests(LogisticsTestCase):
    def test_admin_deletes_household_and_owner_account(self):
        household, owner = self.household_b, self.owner_b
        self.login_as(self.admin)
        response = self.client.delete(f"/api/households/{household.pk}")
        self.assertEqual(response.status_code, 204)

        household = Household.all_objects.get(pk=household.pk)
        self.assertIsNotNone(household.deleted_at)
        self.assertFalse(household.is_active)
        owner = User.objects.get(pk=owner.pk)
        self.assertIsNotNone(owner.deleted_at)
        self.assertFalse(owner.is_active)
        self.assertTrue(owner.username.startswith(f"deleted-{owner.pk}-"))

        # Hidden: detail 404, lists, search, user list
        self.assertEqual(self.client.get(f"/api/households/{household.pk}").status_code, 404)
        self.assertNotIn(household.pk, _ids(self.client.get("/api/households?page_size=200")))
        self.assertNotIn(household.pk, _search_ids(self.client.get("/api/search?q=Bog"), "household"))
        self.assertNotIn(owner.pk, _ids(self.client.get("/api/users?page_size=200")))
        self.assertFalse(Household.objects.filter(pk=household.pk).exists())

        # History rows still load their household through the FK
        movement = PoultryMovement.objects.filter(household_id=household.pk).first()
        self.assertEqual(movement.household.pk, household.pk)
        self.assertEqual(self.client.get(f"/api/batches/{self.batch.pk}").status_code, 200)

        # Audited (household + owner account)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=household.pk,
                                                module="households").exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=owner.pk,
                                                module="users").exists())

    def test_owner_cannot_log_in_after_delete_and_login_is_free(self):
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/households/{self.household_b.pk}").status_code, 204)
        self.client.force_authenticate(user=None)
        response = self.client.post("/api/auth/login", {"username": "owner_b", "password": "Parol12345"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.login_as(self.admin)
        response = self.client.post(
            "/api/households",
            {
                "name": "Yangi xonadon", "owner_first_name": "Ali", "owner_last_name": "Valiyev",
                "owner_username": "owner_b", "phone": "+998901234567", "region": "Samarqand viloyati",
                "district": "Navobod tumani",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_dashboards_stats_and_reports_still_work(self):
        self.login_as(self.admin)
        before = self.client.get("/api/dashboard/admin").data
        self.assertEqual(self.client.delete(f"/api/households/{self.household_b.pk}").status_code, 204)
        self.assertEqual(self.client.get("/api/dashboard/admin").status_code, 200)
        after = self.client.get("/api/dashboard/admin").data
        self.assertEqual(after["households_total"], before["households_total"] - 1)
        self.assertEqual(self.client.get("/api/stats/overview").status_code, 200)
        self.assertEqual(self.client.get("/api/households/map").status_code, 200)
        today = timezone.localdate()
        period = f"date_from={today - timedelta(days=30)}&date_to={today}"
        for report in REPORTS:
            response = self.client.get(f"/api/reports/{report.key}?format=json&{period}")
            self.assertIn(response.status_code, (200, 400), report.key)

    def test_refused_with_not_finished_task(self):
        task = self.create_task(quantity=10)
        self.login_as(self.admin)
        response = self.client.delete(f"/api/households/{self.household_a.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(task["code"], response.data["detail"])
        self.assertIsNone(Household.all_objects.get(pk=self.household_a.pk).deleted_at)
        # Cancelled task → allowed
        self.act(self.admin, task["id"], "cancel", {"reason": "Kerak emas"})
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/households/{self.household_a.pk}").status_code, 204)
        # The task history still shows the household
        self.assertEqual(LogisticsTask.objects.get(pk=task["id"]).household.pk, self.household_a.pk)

    def test_refused_with_open_order(self):
        self.login_as(self.owner_a)
        response = self.client.post(
            "/api/orders",
            {"kind": OrderKind.SUPPLY_REQUEST, "items": [{"product": self.feed.pk, "quantity": 20}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.login_as(self.admin)
        response = self.client.delete(f"/api/households/{self.household_a.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("buyurtma", response.data["detail"])

    def test_permissions(self):
        for user in (self.warehouse, self.vet, self.owner_a, self.logist):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/households/{self.household_a.pk}").status_code, 403)
        self.assertIsNone(Household.all_objects.get(pk=self.household_a.pk).deleted_at)


class ProductDeleteTests(LogisticsTestCase):
    def test_delete_hides_product_and_keeps_history(self):
        self.login_as(self.warehouse)
        response = self.client.delete(f"/api/products/{self.vitamin.pk}")
        self.assertEqual(response.status_code, 204)
        product = Product.all_objects.get(pk=self.vitamin.pk)
        self.assertIsNotNone(product.deleted_at)
        self.assertFalse(product.is_active)
        self.assertEqual(product.name, f"Vitamin (o‘chirilgan #{product.pk})")
        self.assertEqual(product.balance, Decimal("50"))  # remaining stock is kept (just hidden)

        self.assertEqual(self.client.get(f"/api/products/{product.pk}").status_code, 404)
        self.assertNotIn(product.pk, _ids(self.client.get("/api/products?page_size=200")))
        self.assertNotIn(product.pk, _search_ids(self.client.get("/api/search?q=Vitamin"), "product"))
        # History rows still load the product
        tx = StockTransaction.objects.filter(product_id=product.pk).first()
        self.assertEqual(tx.product.name, product.name)
        response = self.client.get("/api/stock-transactions?page_size=200")
        self.assertEqual(response.status_code, 200)
        self.assertIn(tx.pk, _ids(response))
        self.assertEqual(self.client.get("/api/warehouse/stats").status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=product.pk,
                                                module="products").exists())

    def test_deleted_product_cannot_be_used_and_name_is_free(self):
        self.login_as(self.warehouse)
        self.assertEqual(self.client.delete(f"/api/products/{self.vitamin.pk}").status_code, 204)
        response = self.client.post(
            "/api/stock-transactions",
            {"product": self.vitamin.pk, "type": "IN", "quantity": 1, "document_no": "K-2"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/products", {"name": "Vitamin", "category": "OTHER", "unit": "dona", "min_stock": 0},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_refused_while_reserved_by_task(self):
        task = self.create_task(quantity=10)
        self.login_as(self.admin)
        response = self.client.delete(f"/api/products/{self.feed.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("zaxiralangan", response.data["detail"])
        self.act(self.admin, task["id"], "cancel", {"reason": "Kerak emas"})
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/products/{self.feed.pk}").status_code, 204)

    def test_refused_while_in_open_order(self):
        self.login_as(self.owner_a)
        response = self.client.post(
            "/api/orders",
            {"kind": OrderKind.SUPPLY_REQUEST, "items": [{"product": self.vitamin.pk, "quantity": 2}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.login_as(self.admin)
        response = self.client.delete(f"/api/products/{self.vitamin.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("buyurtma", response.data["detail"])

    def test_permissions(self):
        for user in (self.vet, self.owner_a, self.logist):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/products/{self.vitamin.pk}").status_code, 403)
        self.assertIsNone(Product.all_objects.get(pk=self.vitamin.pk).deleted_at)

    def test_integrity_check_still_counts_deleted_products(self):
        self.login_as(self.warehouse)
        self.assertEqual(self.client.delete(f"/api/products/{self.vitamin.pk}").status_code, 204)
        out = io.StringIO()
        call_command("check_integrity", stdout=out)  # raises CommandError on any mismatch
        self.assertIn("Nomuvofiqlik topilmadi", out.getvalue())


class VehicleDeleteTests(LogisticsTestCase):
    def test_delete_hides_vehicle_and_frees_plate(self):
        self.login_as(self.logist)
        response = self.client.delete(f"/api/logistics/vehicles/{self.small_vehicle.pk}")
        self.assertEqual(response.status_code, 204)
        vehicle = Vehicle.all_objects.get(pk=self.small_vehicle.pk)
        self.assertIsNotNone(vehicle.deleted_at)
        self.assertFalse(vehicle.is_active)
        self.assertTrue(vehicle.plate_number.startswith(f"DEL-{vehicle.pk}-"))
        self.assertLessEqual(len(vehicle.plate_number), 15)
        self.assertEqual(self.client.get(f"/api/logistics/vehicles/{vehicle.pk}").status_code, 404)
        self.assertNotIn(vehicle.pk, _ids(self.client.get("/api/logistics/vehicles?page_size=200")))
        response = self.client.post("/api/logistics/vehicles", {"plate_number": "01 B 777 CC", "model": "Damas"},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=vehicle.pk,
                                                module="fleet").exists())

    def test_refused_on_not_finished_task_and_history_kept(self):
        task = self.create_task(quantity=10)
        self.to_status(task["id"], "DELIVERED")
        self.login_as(self.admin)
        response = self.client.delete(f"/api/logistics/vehicles/{self.vehicle.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(task["code"], response.data["detail"])
        self.to_status_confirm(task["id"])
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/logistics/vehicles/{self.vehicle.pk}").status_code, 204)
        response = self.client.get(f"/api/logistics/tasks/{task['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["vehicle"]["id"], self.vehicle.pk)
        # Assigning a deleted vehicle to a new task is impossible
        new_task = self.create_task(quantity=5)
        response = self.act(self.admin, new_task["id"], "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk})
        self.assertEqual(response.status_code, 400)

    def to_status_confirm(self, task_id: int) -> None:
        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": True})
        self.assertEqual(response.status_code, 200, response.data)

    def test_permissions(self):
        for user in (self.warehouse, self.vet, self.owner_a):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/logistics/vehicles/{self.vehicle.pk}").status_code, 403)
        self.assertIsNone(Vehicle.all_objects.get(pk=self.vehicle.pk).deleted_at)


class DriverDeleteTests(LogisticsTestCase):
    def test_delete_hides_driver_and_releases_user(self):
        self.login_as(self.admin)
        response = self.client.delete(f"/api/logistics/drivers/{self.driver.pk}")
        self.assertEqual(response.status_code, 204)
        driver = Driver.all_objects.get(pk=self.driver.pk)
        self.assertIsNotNone(driver.deleted_at)
        self.assertFalse(driver.is_active)
        self.assertIsNone(driver.user_id)
        self.assertEqual(self.client.get(f"/api/logistics/drivers/{driver.pk}").status_code, 404)
        self.assertNotIn(driver.pk, _ids(self.client.get("/api/logistics/drivers?page_size=200")))
        # The logistics user can be linked to a new driver
        response = self.client.post(
            "/api/logistics/drivers",
            {"full_name": "Yangi haydovchi", "phone": "+998901112233", "user": self.logist.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_refused_on_not_finished_task_and_history_kept(self):
        task = self.create_task(quantity=10)
        self.to_status(task["id"], "ASSIGNED")
        self.login_as(self.logist)
        response = self.client.delete(f"/api/logistics/drivers/{self.driver.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(task["code"], response.data["detail"])
        self.act(self.admin, task["id"], "cancel", {"reason": "Kerak emas"})
        self.login_as(self.logist)
        self.assertEqual(self.client.delete(f"/api/logistics/drivers/{self.driver.pk}").status_code, 204)
        self.login_as(self.admin)
        response = self.client.get(f"/api/logistics/tasks/{task['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["driver"]["id"], self.driver.pk)

    def test_permissions(self):
        for user in (self.warehouse, self.vet, self.owner_a):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/logistics/drivers/{self.driver.pk}").status_code, 403)
        self.assertIsNone(Driver.all_objects.get(pk=self.driver.pk).deleted_at)
