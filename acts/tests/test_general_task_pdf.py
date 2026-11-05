"""General act, task act, stored snapshots, PDF and audit (API_CONTRACT_V4 §6)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from acts.models import Act
from audit.models import AuditLog, AuditModule
from builder.models import Blueprint, BlueprintKind, BlueprintStatus
from vet.models import HealthStatus, VetStatus, VetVisit

from .helpers import ActsTestCase, today


class GeneralActTests(ActsTestCase):
    def test_general_act_combines_roles(self):
        response = self.preview(self.admin, kind="general")
        self.assertEqual(response.status_code, 200, response.data)
        snap = response.data
        self.assertEqual(snap["title"], "Umumiy dalolatnoma")
        self.assertIsNone(snap["role"])
        titles = [section["title"] for section in snap["sections"]]
        for role in ("Administrator", "Omborchi", "Logistika xodimi", "Veterinar", "Xonadon egasi"):
            self.assertIn(f"{role}: yakuniy ko‘rsatkichlar", titles)
        self.assertIn("Veterinar: Tekshiruvlar", titles)
        totals = self.summary(snap)
        self.assertEqual(totals["Logistika xodimi"], "1 ta ish")
        self.assertIn("Jami bajarilgan ishlar", totals)
        self.assertEqual(snap["signatures"][-1], "Direktor: ____________")
        self.assertEqual(len(snap["signatures"]), 6)
        self.assertEqual([t["code"] for t in snap["tasks"]], [self.task.code])  # deduplicated

    def test_general_template_body(self):
        Blueprint.objects.create(
            kind=BlueprintKind.DOCUMENT, name="Umumiy", status=BlueprintStatus.ACTIVE,
            data={"scope": "general", "title": "", "body": "Davr: {{period}}. Kod: {{act.code}}",
                  "signatures": ["Bosh hisobchi"]},
        )
        response = self.create(self.admin, kind="general")
        self.assertEqual(response.status_code, 201, response.data)
        act = Act.objects.get(pk=response.data["id"])
        self.assertTrue(act.snapshot["body"].endswith(f"Kod: {act.code}"))
        self.assertEqual(act.snapshot["signatures"], ["Bosh hisobchi: ____________", "Direktor: ____________"])
        self.assertIsNotNone(act.template_id)
        self.assertEqual(act.template_version, 1)


class TaskActTests(ActsTestCase):
    def test_default_task_act(self):
        snap = self.preview(self.admin, kind="task", task_id=self.task.pk).data
        self.assertEqual(snap["kind"], "task")
        self.assertEqual(snap["household"]["code"], self.household_a.code)
        self.assertEqual(snap["sections"][0]["rows"], [["Maydon (m²)", "120"], ["Rasm", "rasm.png"]])
        self.assertEqual(snap["photos"], [{"file_id": self.photo.pk, "label": "Rasm"}])
        self.assertEqual([a["label"] for a in snap["approvals"]], ["Bajardi", "Tasdiqladi"])
        self.assertEqual(snap["approvals"][1]["name"], self.admin.full_name)
        self.assertIn(self.task.code, snap["body"])

    def test_task_document_template(self):
        document = Blueprint.objects.create(
            kind=BlueprintKind.DOCUMENT, name="Ko‘rik akti", status=BlueprintStatus.ACTIVE,
            data={"scope": "task", "title": "Ko‘rik dalolatnomasi",
                  "body": "{{task.code}} {{task.title}}: maydon {{field.area_m2}} m², {{field.nope}}\n{{approvals}}",
                  "signatures": ["Veterinar"], "include_photos": False, "include_approvals": True,
                  "include_quantities": False},
        )
        task = self.make_task(self.household_a, status="APPROVED", values={"area_m2": 80, "photo": self.photo.pk},
                              definition={**self.template.data, "document_id": document.pk})
        response = self.create(self.vet, kind="task", task_id=task.pk)
        self.assertEqual(response.status_code, 201, response.data)
        snap = response.data["snapshot"]
        self.assertEqual(snap["title"], "Ko‘rik dalolatnomasi")
        self.assertTrue(snap["body"].startswith(f"{task.code} Xonadon ko‘rigi: maydon 80 m², {{{{field.nope}}}}\n"))
        self.assertIn(f"Tasdiqladi: {self.admin.full_name}", snap["body"])
        self.assertEqual(snap["photos"], [])
        self.assertEqual(snap["summary"], [])
        self.assertEqual(snap["signatures"], [f"Veterinar: ____________ {self.vet.full_name}"])
        self.assertEqual(response.data["task_id"], task.pk)
        self.assertIsNone(response.data["date_from"])


class StoredActTests(ActsTestCase):
    def test_create_detail_and_frozen_snapshot(self):
        response = self.create(self.admin, kind="role", role="VET", user_id=self.vet.pk)
        self.assertEqual(response.status_code, 201, response.data)
        act_id = response.data["id"]
        code = f"DL-{act_id:06d}"
        self.assertEqual(response.data["code"], code)
        self.assertEqual(response.data["snapshot"]["code"], code)
        self.assertEqual(response.data["kind_display"], "Rol dalolatnomasi")
        self.assertEqual(response.data["role_display"], "Veterinar")
        self.assertEqual(response.data["created_by_name"], self.admin.full_name)
        self.assertEqual(response.data["date_to"], today().isoformat())
        self.assertTrue(AuditLog.objects.filter(module=AuditModule.ACTS, action="create", object_id=act_id).exists())
        # Later work does not change the stored act.
        VetVisit.objects.create(
            household=self.household_a, vet=self.vet, planned_date=today(), purpose="Yangi",
            status=VetStatus.DONE, done_at=timezone.now(), birds_checked=10, health_status=HealthStatus.GOOD,
            conclusion="OK", created_by=self.admin,
        )
        detail = self.client.get(f"/api/acts/{act_id}").data
        self.assertEqual(self.summary(detail["snapshot"])["O‘tkazilgan tekshiruvlar"], "1")
        fresh = self.preview(self.admin, kind="role", role="VET", user_id=self.vet.pk).data
        self.assertEqual(self.summary(fresh)["O‘tkazilgan tekshiruvlar"], "2")
        self.login_as(self.admin)
        rows = self.client.get("/api/acts").data["results"]
        self.assertNotIn("snapshot", rows[0])

    def test_pdf(self):
        acts = [
            self.create(self.admin, kind="role", role="VET", household_id=self.household_a.pk).data["id"],
            self.create(self.admin, kind="general").data["id"],
            self.create(self.admin, kind="task", task_id=self.task.pk).data["id"],
            self.create(self.owner_a, kind="role", role="OWNER").data["id"],
        ]
        self.login_as(self.admin)
        for act_id in acts:
            response = self.client.get(f"/api/acts/{act_id}/pdf")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertEqual(response["Content-Disposition"], f'attachment; filename="DL-{act_id:06d}.pdf"')
            self.assertTrue(response.content.startswith(b"%PDF"))
            if act_id == acts[2]:  # task act: the photo thumbnail is embedded
                self.assertIn(b"/Subtype /Image", response.content)
        self.assertEqual(AuditLog.objects.filter(module=AuditModule.ACTS, action="export").count(), 4)

    def test_pdf_skips_missing_photo_and_truncates_long_sections(self):
        act = Act.objects.get(pk=self.create(self.admin, kind="general").data["id"])
        snapshot = dict(act.snapshot)
        snapshot["photos"] = [{"file_id": 999999, "label": "Yo‘q"}, {"file_id": self.photo.pk, "label": "Bor"}]
        snapshot["sections"] = [{"title": "Veterinar: Tekshiruvlar", "columns": ["A", "B", "C"],
                                 "rows": [[str(i), "x", "y"] for i in range(120)]}]
        self.photo.file.storage.delete(self.photo.file.name)  # missing on disk too
        from acts import pdf

        with self.assertLogs("nnp.acts", level="WARNING"):
            content = pdf.build(snapshot)
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertNotIn(b"/Subtype /Image", content)

    def test_owner_period_uses_dates(self):
        response = self.create(self.owner_a, kind="role", role="OWNER",
                               date_from=(today() - timedelta(days=3)).isoformat())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["user"]["id"], self.owner_a.pk)
