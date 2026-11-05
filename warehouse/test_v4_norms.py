"""Products catalogue fields, feed norms and construction options (API_CONTRACT_V4 §5)."""

from __future__ import annotations

from audit.models import AuditLog, AuditModule
from builder.models import ConfigVersion
from common.roles import Role
from common.testing import BaseAPITestCase, make_product, make_user
from warehouse.models import Product, ProductCategory, Unit


class ProductCatalogueTests(BaseAPITestCase):
    def test_new_fields_read_write_audit_and_versions(self):
        self.login_as(self.admin)
        response = self.client.post(
            "/api/products",
            {"name": "Enrofloksatsin", "category": "VET_DRUG", "unit": "litr", "min_stock": "1",
             "price": "125000.50", "dosage": "1 ml / 1 l suv", "description": "Antibiotik"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["price"], 125000.5)
        self.assertIsNone(response.data["consumption_norm"])
        self.assertEqual(response.data["dosage"], "1 ml / 1 l suv")
        product_id = response.data["id"]
        response = self.client.patch(f"/api/products/{product_id}", {"consumption_norm": "0.125"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["consumption_norm"], 0.125)
        versions = ConfigVersion.objects.filter(object_type="product", object_id=product_id).order_by("version")
        self.assertEqual([v.version for v in versions], [1, 2])
        self.assertEqual(versions[1].data["consumption_norm"], 0.125)
        update = AuditLog.objects.filter(module=AuditModule.PRODUCTS, action="update", object_id=product_id).get()
        self.assertEqual(update.after, {"consumption_norm": 0.125})
        # A no-op PATCH writes nothing.
        self.client.patch(f"/api/products/{product_id}", {"consumption_norm": "0.125"}, format="json")
        self.assertEqual(versions.count(), 2)

    def test_validation(self):
        self.login_as(self.admin)
        product = make_product()
        for body in ({"price": "-1"}, {"consumption_norm": "-0.5"}, {"dosage": "x" * 201},
                     {"description": "x" * 1001}):
            response = self.client.patch(f"/api/products/{product.pk}", body, format="json")
            self.assertEqual(response.status_code, 400, body)
        response = self.client.patch(f"/api/products/{product.pk}", {"price": None}, format="json")
        self.assertEqual(response.status_code, 200)


class FeedNormTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vet = make_user("vet1", Role.VET)
        cls.logist = make_user("logist1", Role.LOGISTICS)
        cls.feed = make_product("Start yem")
        cls.drug = Product.objects.create(name="Vitamin", category=ProductCategory.VET_DRUG, unit=Unit.LITRE)

    def post(self, **body):
        data = {"poultry_type": "BROILER", "day_from": 1, "day_to": 10, "grams_per_bird_day": "25.5", **body}
        return self.client.post("/api/feed-norms", data, format="json")

    def test_crud_versions_and_audit(self):
        self.login_as(self.admin)
        response = self.post(product=self.feed.pk, note="Start")
        self.assertEqual(response.status_code, 201, response.data)
        norm_id = response.data["id"]
        self.assertEqual(response.data["product"], {"id": self.feed.pk, "name": "Start yem", "unit": "kg"})
        self.assertEqual(response.data["poultry_type_display"], "Go‘sht yo‘nalishi")
        self.assertEqual(response.data["grams_per_bird_day"], 25.5)
        response = self.client.patch(f"/api/feed-norms/{norm_id}", {"day_to": 14}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["day_to"], response.data["note"]), (14, "Start"))
        self.assertEqual(self.client.get("/api/feed-norms", {"poultry_type": "BROILER"}).data["count"], 1)
        self.assertEqual(self.client.get("/api/feed-norms", {"poultry_type": "BREEDER"}).data["count"], 0)
        self.assertEqual(self.client.delete(f"/api/feed-norms/{norm_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/feed-norms/{norm_id}").status_code, 404)
        versions = list(ConfigVersion.objects.filter(object_type="feed_norm", object_id=norm_id).order_by("version"))
        self.assertEqual([(v.version, v.status) for v in versions], [(1, ""), (2, ""), (3, "deleted")])
        self.assertEqual(versions[1].data["day_to"], 14)
        rows = AuditLog.objects.filter(module=AuditModule.PRODUCTS, op_id=f"YM-{norm_id:06d}").order_by("id")
        self.assertEqual([row.action for row in rows], ["create", "update", "delete"])
        self.assertEqual(rows[1].after, {"object": "feed_norm", "id": norm_id, "day_to": 14})

    def test_validation(self):
        self.login_as(self.admin)
        self.assertEqual(self.post().status_code, 201)
        response = self.post(day_from=10, day_to=20)
        self.assertEqual(response.status_code, 400)
        self.assertIn("1–10", response.data["day_from"][0])
        self.assertEqual(self.post(poultry_type="BREEDER", day_from=5, day_to=20).status_code, 201)  # other type
        self.assertEqual(self.post(day_from=11, day_to=20).status_code, 201)
        self.assertEqual(self.post(day_from=30, day_to=25).status_code, 400)
        self.assertEqual(self.post(day_from=0).status_code, 400)
        self.assertEqual(self.post(day_from=390, day_to=401).status_code, 400)
        self.assertEqual(self.post(day_from=50, day_to=60, grams_per_bird_day="0").status_code, 400)
        response = self.post(day_from=50, day_to=60, product=self.drug.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("product", response.data)
        self.assertEqual(self.post(poultry_type="LAYER", day_from=70, day_to=80).status_code, 400)
        # PATCH into an overlap / reversed range.
        norm_id = self.post(day_from=100, day_to=110).data["id"]
        self.assertEqual(self.client.patch(f"/api/feed-norms/{norm_id}", {"day_from": 15}, format="json").status_code,
                         400)
        self.assertEqual(self.client.patch(f"/api/feed-norms/{norm_id}", {"day_from": 120}, format="json").status_code,
                         400)

    def test_permissions(self):
        for user in (self.warehouse, self.vet):
            self.login_as(user)
            self.assertEqual(self.client.get("/api/feed-norms").status_code, 200)
            self.assertEqual(self.post().status_code, 403)
        for user in (self.logist, self.owner_a):
            self.login_as(user)
            self.assertEqual(self.client.get("/api/feed-norms").status_code, 403)
        self.login_as(make_user("root", Role.ADMIN, is_superuser=True))
        self.assertEqual(self.post().status_code, 201)


class ConstructionOptionTests(BaseAPITestCase):
    def test_crud_versions_and_permissions(self):
        self.login_as(self.admin)
        body = {"name": "Issiqxona 50 m²", "unit": "dona", "price": "15000000", "description": "Polikarbonat"}
        response = self.client.post("/api/construction-options", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        option_id = response.data["id"]
        self.assertTrue(response.data["is_active"])
        duplicate = self.client.post("/api/construction-options", {"name": "issiqxona 50 m²"}, format="json")
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(self.client.post("/api/construction-options", {"name": "X", "price": "-5"},
                                          format="json").status_code, 400)
        response = self.client.patch(f"/api/construction-options/{option_id}", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(self.client.get("/api/construction-options", {"is_active": "1"}).data["count"], 0)
        self.assertEqual(self.client.get("/api/construction-options", {"search": "issiq"}).data["count"], 1)
        self.login_as(self.warehouse)
        self.assertEqual(self.client.get("/api/construction-options").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/construction-options/{option_id}", {"price": "1"},
                                           format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/construction-options/{option_id}").status_code, 403)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/construction-options/{option_id}").status_code, 204)
        versions = ConfigVersion.objects.filter(object_type="construction_option", object_id=option_id)
        self.assertEqual(sorted(versions.values_list("version", flat=True)), [1, 2, 3])
        self.assertEqual(
            AuditLog.objects.filter(module=AuditModule.PRODUCTS, op_id=f"QV-{option_id:06d}").count(), 3
        )
