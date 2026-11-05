"""Notification constructor (API_CONTRACT_V4 §4): custom text, delay, extra users, versions, revert."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from builder.models import ConfigVersion
from notifications import services as notification_services
from notifications.models import Notification, NotificationSetting, NotificationStatus
from notifications.services import notify, pending

from .helpers import BuilderTestCase

URL = "/api/notification-settings"


class NotificationConstructorTests(BuilderTestCase):
    def patch(self, event, body, user=None):
        self.as_user(user or self.superuser)
        return self.client.patch(f"{URL}/{event}", body, format="json")

    def test_settings_fields_and_versions(self):
        response = self.patch("household_created", {
            "custom_text": "Yangi: {code} — {title} {unknown}", "delay_minutes": 30,
            "extra_user_ids": [self.vet.pk, self.vet.pk], "comment": "matn",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["delay_minutes"], response.data["extra_user_ids"]), (30, [self.vet.pk]))
        self.assertEqual(self.patch("household_created", {"delay_minutes": 10081}).status_code, 400)
        self.assertEqual(self.patch("household_created", {"extra_user_ids": [999999]}).status_code, 400)
        self.assertEqual(self.patch("household_created", {"delay_minutes": 5}, user=self.admin).status_code, 403)
        self.patch("household_created", {"delay_minutes": 0})
        versions = self.client.get(f"{URL}/household_created/versions").data
        self.assertEqual([v["version"] for v in versions], [2, 1])
        self.assertEqual(versions[1]["comment"], "matn")
        reverted = self.client.post(f"{URL}/household_created/revert", {"version": 1}, format="json")
        self.assertEqual(reverted.data["delay_minutes"], 30)
        self.assertEqual(ConfigVersion.objects.filter(object_type="notification_setting").count(), 3)
        self.assertEqual(self.client.post(f"{URL}/household_created/revert", {"version": 9},
                                          format="json").status_code, 400)
        self.assertEqual(self.client.get(f"{URL}/nope/versions").status_code, 404)
        listed = {row["event"]: row for row in self.client.get(URL).data}
        self.assertEqual(listed["household_created"]["delay_minutes"], 30)
        self.assertIn("task_assigned", listed)
        self.assertEqual(listed["task_cancelled"]["custom_text"], "")

    def test_notify_applies_text_delay_and_extra_users(self):
        NotificationSetting.objects.create(
            event="household_created", custom_text="{code}: {title} <b>{unknown}</b>\n{text}", delay_minutes=60,
            extra_user_ids=[self.vet.pk],
        )
        User = type(self.vet)
        User.objects.filter(pk__in=[self.admin.pk, self.vet.pk]).update(telegram_id=None)
        User.objects.filter(pk=self.admin.pk).update(telegram_id=5551)
        rows = notify("household_created", key="x1", text="🏠 <b>NNP — YANGI XONADON</b>\n\nXonadon: A",
                      related_op="XN-0001")
        recipients = {row.recipient_id for row in rows}
        self.assertIn(self.vet.pk, recipients)  # extra user
        self.assertIn(self.admin.pk, recipients)  # default role
        admin_row = Notification.objects.get(event="household_created", recipient=self.admin)
        self.assertTrue(admin_row.text.startswith("🏠 <b>NNP — YANGI XONADON</b>\n\nXN-0001: Yangi xonadon"))
        self.assertIn("&lt;b&gt;{unknown}&lt;/b&gt;", admin_row.text)  # literal + escaped
        self.assertIn("Xonadon: A", admin_row.text)  # {text}
        self.assertEqual(admin_row.status, NotificationStatus.PENDING)
        self.assertIsNotNone(admin_row.send_after)
        self.assertNotIn(admin_row.pk, [row.pk for row in pending()])  # delayed
        Notification.objects.filter(pk=admin_row.pk).update(send_after=timezone.now() - timedelta(minutes=1))
        self.assertIn(admin_row.pk, [row.pk for row in pending()])

    def test_immediate_rows_have_no_send_after(self):
        type(self.admin).objects.filter(pk=self.admin.pk).update(telegram_id=5552)
        notify("household_created", key="x2", text="t\n\nb")
        row = Notification.objects.get(event="household_created", recipient=self.admin)
        self.assertIsNone(row.send_after)
        self.assertIn(row.pk, [r.pk for r in pending()])

    def test_builder_events_reach_any_role_preferences(self):
        keys = [d.key for d in notification_services.events_for_role("VET")]
        self.assertIn("task_assigned", keys)
        self.assertIn("process_stage_started", keys)
