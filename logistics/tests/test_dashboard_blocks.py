"""Logistics blocks of the dashboards (API_CONTRACT_V3 §10.1 ``logistics``, §10.2 ``deliveries_to_confirm``)."""

from __future__ import annotations

from .helpers import LogisticsTestCase


class DashboardBlockTests(LogisticsTestCase):
    def test_admin_dashboard_logistics_counters(self):
        self.login_as(self.admin)
        zeros = self.client.get("/api/dashboard/admin").data["logistics"]
        self.assertEqual(zeros, {"new": 0, "assigned": 0, "accepted": 0, "in_transit": 0, "delivered": 0,
                                 "overdue": 0, "delivered_today": 0})
        self.create_task(10)
        moving = self.create_task(20)["id"]
        self.to_status(moving, "IN_TRANSIT")
        done = self.create_task(30)["id"]
        self.to_status(done, "DELIVERED")
        self.login_as(self.warehouse)
        block = self.client.get("/api/dashboard/admin").data["logistics"]
        self.assertEqual(block, {"new": 1, "assigned": 0, "accepted": 0, "in_transit": 1, "delivered": 1,
                                 "overdue": 0, "delivered_today": 1})

    def test_owner_dashboard_deliveries_to_confirm(self):
        mine = self.create_task(10)["id"]
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/dashboard/owner").data["deliveries_to_confirm"], [])
        self.to_status(mine, "DELIVERED")
        self.login_as(self.owner_a)
        rows = self.client.get("/api/dashboard/owner").data["deliveries_to_confirm"]
        self.assertEqual([(row["id"], row["status"]) for row in rows], [(mine, "DELIVERED")])
        self.assertIn("confirm", rows[0]["allowed_actions"])
        self.assertEqual(rows[0]["household"]["id"], self.household_a.pk)
        # Another household's owner never sees it.
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get("/api/dashboard/owner").data["deliveries_to_confirm"], [])
        # Confirmed tasks leave the list.
        self.assertEqual(self.act(self.owner_a, mine, "confirm", {"received_ok": True}).status_code, 200)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/dashboard/owner").data["deliveries_to_confirm"], [])
