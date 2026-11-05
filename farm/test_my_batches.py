"""Owner batch card (TZ §2 ROLE 6, §6) and the household's first received date per batch (TZ §5)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from common.testing import BaseAPITestCase, make_batch
from vet.models import Vaccination


class MyBatchTests(BaseAPITestCase):
    def test_owner_sees_own_batch_with_vaccinations_only(self):
        Vaccination.objects.create(household=self.household_a, batch=self.batch, vaccine_name="Nyukasl",
                                   planned_date=timezone.localdate() + timedelta(days=3), dose="0.5 ml")
        Vaccination.objects.create(household=self.household_b, batch=self.batch, vaccine_name="Boshqa uy",
                                   planned_date=timezone.localdate())
        self.login_as(self.owner_a)
        response = self.client.get(f"/api/my/batches/{self.batch.pk}")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual((data["code"], data["count"], data["supplier"]), (self.batch.code, 300, "Toshkent inkubatori"))
        self.assertEqual(data["age_days"], 1 + 30)  # 1 day old on arrival, arrived 30 days ago
        self.assertEqual(data["first_received_date"], (timezone.localdate() - timedelta(days=20)).isoformat())
        self.assertEqual([row["vaccine_name"] for row in data["vaccinations"]], ["Nyukasl"])

        other = make_batch(self.admin, 50)
        self.assertEqual(self.client.get(f"/api/my/batches/{other.pk}").status_code, 404)
        self.login_as(self.admin)
        self.assertEqual(self.client.get(f"/api/my/batches/{self.batch.pk}").status_code, 403)

    def test_household_batches_have_first_received_date(self):
        self.login_as(self.admin)
        rows = self.client.get(f"/api/households/{self.household_a.pk}/batches").data
        self.assertEqual(rows[0]["first_received_date"], (timezone.localdate() - timedelta(days=20)).isoformat())
