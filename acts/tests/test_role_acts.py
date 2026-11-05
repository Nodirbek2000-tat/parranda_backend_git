"""Role act content (API_CONTRACT_V4 §6): only finished work, filtered by period, household and user."""

from __future__ import annotations

from datetime import timedelta

from builder.models import TaskStatus
from common.roles import Role

from .helpers import ActsTestCase, today


class RoleActContentTests(ActsTestCase):
    def test_vet_act_counts_done_work_of_the_vet(self):
        response = self.preview(self.admin, kind="role", role="VET", user_id=self.vet.pk)
        self.assertEqual(response.status_code, 200, response.data)
        snap = response.data
        self.assertEqual(snap["code"], "")
        self.assertEqual(snap["title"], "Veterinar bajargan ishlar dalolatnomasi")
        self.assertEqual(snap["role"], "VET")
        self.assertEqual(snap["role_display"], "Veterinar")
        self.assertEqual(snap["responsible"], self.vet.full_name)
        summary = self.summary(snap)
        self.assertEqual(summary["O‘tkazilgan tekshiruvlar"], "1")  # cancelled + other vet's visit excluded
        self.assertEqual(summary["Tekshirilgan parrandalar"], "250")
        self.assertEqual(summary["O‘tkazilgan emlashlar"], "1")
        self.assertEqual(summary["Emlangan parrandalar"], "240")
        self.assertEqual(summary["Kasallik qaydlari"], "1")
        self.assertEqual(summary["Yopilgan murojaatlar"], "1")
        self.assertEqual(summary["Tasdiqlangan vazifalar"], "1")
        visits = self.section(snap, "Tekshiruvlar")
        self.assertEqual(visits["columns"], ["Sana", "Kod", "Xonadon", "Tekshirilgan", "Holat", "Xulosa"])
        self.assertEqual(visits["rows"][0][2], f"{self.household_a.code} {self.household_a.name}")
        self.assertEqual(visits["rows"][0][3:], ["250", "Yaxshi", "Sog‘lom"])
        # Approved builder task with its latest submission values; the SUBMITTED one is not counted.
        self.assertEqual([t["code"] for t in snap["tasks"]], [self.task.code])
        self.assertEqual(snap["tasks"][0]["values"], [{"label": "Maydon (m²)", "value": "120"},
                                                      {"label": "Rasm", "value": "rasm.png"}])
        self.assertEqual(snap["photos"], [{"file_id": self.photo.pk, "label": f"{self.task.code}: Rasm"}])
        self.assertTrue(snap["signatures"][0].startswith("Veterinar: ____________ Dilnoza Karimova"))

    def test_whole_role_includes_every_vet(self):
        snap = self.preview(self.admin, kind="role", role="VET").data
        self.assertEqual(self.summary(snap)["O‘tkazilgan tekshiruvlar"], "2")
        self.assertEqual(snap["responsible"], "Barcha veterinarlar")

    def test_household_filter(self):
        snap = self.preview(self.admin, kind="role", role="VET", household_id=self.household_b.pk).data
        self.assertEqual(self.summary(snap)["O‘tkazilgan tekshiruvlar"], "1")
        self.assertEqual(self.summary(snap)["Tasdiqlangan vazifalar"], "0")
        self.assertEqual(snap["household"]["code"], self.household_b.code)
        self.assertEqual(snap["household"]["owner"], self.owner_b.full_name)
        self.assertIn("Xonadon egasi: ____________", snap["signatures"][-1])

    def test_period_filter(self):
        old = today() - timedelta(days=60)
        response = self.preview(self.admin, kind="role", role="VET", date_from=(old - timedelta(days=5)).isoformat(),
                                date_to=old.isoformat())
        summary = self.summary(response.data)
        self.assertEqual(summary["O‘tkazilgan tekshiruvlar"], "0")
        self.assertEqual(summary["Tasdiqlangan vazifalar"], "0")
        self.assertEqual(response.data["period"], f"{old - timedelta(days=5):%d.%m.%Y} — {old:%d.%m.%Y}")

    def test_default_period_and_limits(self):
        self.login_as(self.admin)
        response = self.client.get("/api/acts/preview", {"kind": "role", "role": "VET"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["period"], f"{today().replace(day=1):%d.%m.%Y} — {today():%d.%m.%Y}")
        response = self.client.get(
            "/api/acts/preview",
            {"kind": "role", "role": "VET", "date_from": (today() - timedelta(days=366)).isoformat(),
             "date_to": today().isoformat()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("date_to", response.data)
        response = self.client.get("/api/acts/preview", {"kind": "role", "role": "VET",
                                                         "date_from": today().isoformat(),
                                                         "date_to": (today() - timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/acts/preview", {"kind": "role"}).status_code, 400)

    def test_warehouse_act(self):
        snap = self.preview(self.warehouse, kind="role", role="WAREHOUSE").data
        summary = self.summary(snap)
        self.assertEqual(summary["Kirim operatsiyalari"], "1")
        self.assertEqual(summary["Chiqim operatsiyalari"], "1")
        self.assertEqual(summary["Xonadonlarga tarqatilgan parrandalar"], "50")
        receipts = self.section(snap, "Kirim")
        self.assertEqual(receipts["rows"][0][2:5], ["KR-1", "Yem PK-1", "1 000 kg"])
        issues = self.section(snap, "Chiqim (tarqatish)")
        self.assertEqual(issues["rows"][0][4:], ["40 kg", f"{self.household_a.code} {self.household_a.name}"])
        # The other warehouse user did nothing.
        snap = self.preview(self.admin, kind="role", role="WAREHOUSE", user_id=self.warehouse2.pk).data
        self.assertEqual(self.summary(snap)["Kirim operatsiyalari"], "0")

    def test_logistics_act_excludes_cancelled_tasks(self):
        snap = self.preview(self.logist, kind="role", role="LOGISTICS").data
        summary = self.summary(snap)
        self.assertEqual(summary["Yetkazilgan topshiriqlar"], "1")
        self.assertEqual(summary["Yetkazilgan yuk, kg"], "150")
        row = self.section(snap, "Yetkazilgan topshiriqlar")["rows"][0]
        self.assertEqual(row[1], self.delivery.code)
        self.assertEqual(row[3], "Yem PK-1 150 kg")
        self.assertEqual(row[5], "Sardor Rahimov / 01 A 123 BC")

    def test_owner_act(self):
        snap = self.preview(self.owner_a, kind="role", role="OWNER").data
        summary = self.summary(snap)
        self.assertEqual(summary["Kunlik hisobotlar"], "2")
        self.assertEqual(summary["Go‘sht, kg"], "50")
        self.assertEqual(summary["Yem, kg"], "30")
        self.assertEqual(summary["Nobud bo‘lgan"], "2")
        self.assertEqual(summary["O‘rtacha vazn, g"], "1 800")
        self.assertEqual(summary["Qabul qilingan yetkazmalar"], "1")
        self.assertEqual(summary["Veterinarga murojaatlar"], "1")
        self.assertEqual(snap["household"]["code"], self.household_a.code)
        self.assertEqual(snap["responsible"], self.owner_a.full_name)
        # Owner B sees only household B (nothing there).
        snap = self.preview(self.owner_b, kind="role", role="OWNER").data
        self.assertEqual(self.summary(snap)["Kunlik hisobotlar"], "0")

    def test_admin_act(self):
        snap = self.preview(self.admin, kind="role", role="ADMIN").data
        summary = self.summary(snap)
        self.assertEqual(summary["Ro‘yxatga olingan xonadonlar"], "3")
        self.assertEqual(summary["Tasdiqlangan vazifalar"], "1")  # approved by the admin
        self.assertEqual(snap["responsible"], "Barcha administratorlar")

    def test_test_tasks_are_excluded(self):
        self.make_task(self.household_a, status=TaskStatus.APPROVED, values={"area_m2": 1}, is_test=True)
        snap = self.preview(self.admin, kind="role", role="VET").data
        self.assertEqual(self.summary(snap)["Tasdiqlangan vazifalar"], "1")

    def test_profile_executor_matches_its_base_role(self):
        from builder.models import Blueprint, BlueprintKind, BlueprintStatus

        profile = Blueprint.objects.create(
            kind=BlueprintKind.ROLE, name="Katta vet", status=BlueprintStatus.ACTIVE,
            data={"base_role": "VET", "permissions": {}},
        )
        self.make_task(self.household_b, status=TaskStatus.APPROVED, values={}, executor=f"profile:{profile.pk}")
        snap = self.preview(self.admin, kind="role", role="VET").data
        self.assertEqual(self.summary(snap)["Tasdiqlangan vazifalar"], "2")
        snap = self.preview(self.admin, kind="role", role="LOGISTICS").data
        self.assertEqual(self.summary(snap)["Tasdiqlangan vazifalar"], "0")

    def test_role_template_body(self):
        from builder.models import Blueprint, BlueprintKind, BlueprintStatus

        Blueprint.objects.create(
            kind=BlueprintKind.DOCUMENT, name="Vet akti", status=BlueprintStatus.ACTIVE,
            data={"scope": "role", "role": "VET", "title": "Veterinariya ishlari",
                  "body": "{{company}}: {{responsible}} ({{role}}) {{period}} {{household.code}} {{unknown.key}}\n"
                          "{{quantities}}",
                  "signatures": ["Veterinar", "Xonadon egasi"], "include_photos": False,
                  "include_approvals": True, "include_quantities": True},
        )
        snap = self.preview(self.admin, kind="role", role=Role.VET, user_id=self.vet.pk,
                            household_id=self.household_a.pk).data
        self.assertEqual(snap["title"], "Veterinariya ishlari")
        self.assertTrue(snap["body"].startswith(f"Navobod naslli parranda qisqarmasi: {self.vet.full_name} (Veterinar)"))
        self.assertIn(f" {self.household_a.code} {{{{unknown.key}}}}\n", snap["body"])
        self.assertIn("O‘tkazilgan tekshiruvlar: 1", snap["body"])
        self.assertEqual(snap["photos"], [])
        self.assertEqual(snap["signatures"], [f"Veterinar: ____________ {self.vet.full_name}",
                                              f"Xonadon egasi: ____________ {self.owner_a.full_name}"])
