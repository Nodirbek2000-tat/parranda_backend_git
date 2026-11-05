"""Personal notification preferences (TZ §11.3 "Bildirishnoma sozlamalarini boshqarish")."""

from __future__ import annotations

from audit.models import AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_user

from . import services
from .models import Notification, NotificationStatus

URL = "/api/my/notification-preferences"


class PreferenceTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin2 = make_user("admin_pref", Role.ADMIN, telegram_id=880001)

    def test_list_contains_only_events_of_the_role(self):
        self.login_as(self.owner_a)
        events = {row["event"] for row in self.client.get(URL).data}
        self.assertIn("poultry_distributed", events)  # household_owner
        self.assertNotIn("household_created", events)  # admins only
        self.login_as(self.admin2)
        self.assertIn("daily_report", {row["event"] for row in self.client.get(URL).data})

    def test_mute_and_inbox_only(self):
        self.login_as(self.admin2)
        response = self.client.patch(f"{URL}/daily_report", {"telegram_enabled": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["enabled"], response.data["telegram_enabled"]), (True, False))
        self.assertTrue(AuditLog.objects.filter(module="notifications", object_type="NotificationPreference").exists())
        rows = services.notify("daily_report", key="t1", text="<b>x</b>\nmatn", staff_path="/x")
        mine = [row for row in rows if row.recipient_id == self.admin2.pk]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].status, NotificationStatus.SKIPPED)  # inbox only
        self.assertIsNone(mine[0].chat_id)

        self.client.patch(f"{URL}/daily_report", {"enabled": False}, format="json")
        rows = services.notify("daily_report", key="t2", text="<b>x</b>\nmatn", staff_path="/x")
        self.assertFalse([row for row in rows if row.recipient_id == self.admin2.pk])
        self.assertTrue([row for row in rows if row.recipient_id == self.admin.pk])  # others unaffected
        self.assertEqual(Notification.objects.filter(recipient=self.admin2).count(), 1)

    def test_unknown_or_foreign_event_is_404(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.client.patch(f"{URL}/household_created", {"enabled": False}, format="json").status_code,
                         404)
        self.assertEqual(self.client.patch(f"{URL}/nope", {"enabled": False}, format="json").status_code, 404)
