"""Verifier: the OpenAPI schema describes the REAL response shape of every Phase 2 endpoint (keys and nullability)."""

from __future__ import annotations

from datetime import date

from django.test import override_settings

from accounts.models import User
from common.testing import BOT_API_TOKEN, make_product
from farm.models import MovementType
from warehouse import services as warehouse_services
from decimal import Decimal

from .helpers import FrozenClockTestCase, move, report, staff, worked_example


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN, DEBUG=True)
class SchemaMatchesResponsesTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        data = worked_example(cls.admin)
        cls.household = data["household"]
        cls.breeder = data["breeder"]
        report(cls.household, date(2026, 11, 4), "30.25", "60.50", deaths=2, weight="2100")
        move(cls.admin, cls.breeder, None, MovementType.DEATH, 1, date(2026, 11, 4))
        cls.product = make_product("Yem PK-1", min_stock="100")
        warehouse_services.record_stock_transaction(actor=cls.warehouse, product_id=cls.product.pk,
                                                    transaction_type="IN", quantity=Decimal("500"), document_no="K")
        warehouse_services.record_stock_transaction(actor=cls.warehouse, product_id=cls.product.pk,
                                                    transaction_type="OUT", quantity=Decimal("50"), document_no="C",
                                                    household=cls.household)
        User.objects.filter(pk=cls.household.owner_id).update(telegram_id=6001, must_change_password=False)
        User.objects.filter(pk=cls.admin.pk).update(telegram_id=6002)

    def resolve(self, schema, node):
        while "$ref" in node:
            name = node["$ref"].split("/")[-1]
            node = schema["components"]["schemas"][name]
        if "allOf" in node and len(node["allOf"]) == 1:
            merged = dict(self.resolve(schema, node["allOf"][0]))
            merged.update({k: v for k, v in node.items() if k != "allOf"})
            return merged
        return node

    def check(self, schema, node, value, path):
        node = self.resolve(schema, node)
        if value is None:
            self.assertTrue(node.get("nullable") or "oneOf" in node or "anyOf" in node, f"{path} is null in response "
                            f"but not nullable in schema: {node}")
            return
        if "oneOf" in node or "anyOf" in node:
            return
        kind = node.get("type")
        if isinstance(value, dict):
            if kind != "object" or "properties" not in node:
                return  # free-form object (e.g. audit before/after)
            props = node["properties"]
            self.assertLessEqual(set(value), set(props), f"{path}: response keys missing from the schema")
            self.assertLessEqual(set(node.get("required", ())), set(value), f"{path}: required keys not sent")
            for key, item in value.items():
                self.check(schema, props[key], item, f"{path}.{key}")
        elif isinstance(value, list):
            self.assertEqual(kind, "array", path)
            for index, item in enumerate(value[:3]):
                self.check(schema, node["items"], item, f"{path}[{index}]")
        elif isinstance(value, bool):
            self.assertEqual(kind, "boolean", path)
        elif isinstance(value, int):
            self.assertIn(kind, ("integer", "number"), path)
        elif isinstance(value, float):
            self.assertEqual(kind, "number", path)
        elif isinstance(value, str):
            self.assertEqual(kind, "string", path)

    def test_shapes(self):
        self.login_as(self.admin)
        schema = self.client.get("/api/schema?format=json").json()
        owner = User.objects.get(pk=self.household.owner_id)
        plans = [
            (self.admin, "/api/stats/overview", "/api/stats/overview", {}),
            (self.admin, f"/api/households/{self.household.pk}/stats?date_from=2026-11-01&date_to=2026-11-04",
             "/api/households/{id}/stats", {}),
            (owner, "/api/my/stats", "/api/my/stats", {}),
            (self.admin, f"/api/batches/{self.breeder.pk}/stats", "/api/batches/{id}/stats", {}),
            (self.admin, f"/api/products/{self.product.pk}/stats", "/api/products/{id}/stats", {}),
            (self.admin, "/api/warehouse/stats", "/api/warehouse/stats", {}),
            (None, "/api/bot/users/6001/summary", "/api/bot/users/{telegram_id}/summary",
             {"HTTP_X_BOT_TOKEN": BOT_API_TOKEN}),
            (None, "/api/bot/users/6002/summary", "/api/bot/users/{telegram_id}/summary",
             {"HTTP_X_BOT_TOKEN": BOT_API_TOKEN}),
            (self.admin, "/api/audit-logs", "/api/audit-logs", {}),
            (None, "/api/health", "/api/health", {}),
        ]
        for user, url, template, headers in plans:
            with self.subTest(url=url):
                self.client.force_authenticate(user=user)
                response = self.client.get(url, **headers)
                self.assertEqual(response.status_code, 200, response.content[:300])
                operation = schema["paths"][template]["get"]
                content = operation["responses"]["200"]["content"]["application/json"]["schema"]
                self.check(schema, content, response.json(), template)
        # PATCH /api/batches/{id}
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/batches/{self.breeder.pk}", {"supplier": "Yangi"}, format="json")
        self.assertEqual(response.status_code, 200)
        content = schema["paths"]["/api/batches/{id}"]["patch"]["responses"]["200"]["content"]["application/json"]
        self.check(schema, content["schema"], response.json(), "PATCH batch")
