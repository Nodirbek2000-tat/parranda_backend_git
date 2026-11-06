import io
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User
from config.testutils import NNPTestCase
from notify.models import Notification
from notify.services import queue
from stock.models import Product, StockMove

from .models import Case, CaseStage, StageEvent
from .services import send_overdue_reminders

R = Role


class ChainHelpers(NNPTestCase):
    def advance_to(self, case_id, target):
        """Run the chain with the right roles until stage `target` is ACTIVE."""
        steps = {
            2: lambda: self.submit(R.HR, case_id, 2),
            3: lambda: self.submit(R.DIRECTOR, case_id, 3),
            4: lambda: (self.submit(R.BUILDER, case_id, 4, photo=True), self.act(R.ADMIN, case_id, 4, "approve")),
            5: lambda: (self.submit(R.WAREHOUSE, case_id, 5), self.act(R.ADMIN, case_id, 5, "approve")),
            6: lambda: (self.submit(R.INCUBATOR, case_id, 6, {"chicks_count": 100}, photo=True),
                        self.act(R.ADMIN, case_id, 6, "approve")),
            7: lambda: (self.submit(R.LOGISTICS, case_id, 7, photo=True), self.act(R.ADMIN, case_id, 7, "approve")),
            8: lambda: (self.submit(R.VET, case_id, 8, {"health": "good"}, photo=True),
                        self.act(R.ADMIN, case_id, 8, "approve")),
            9: lambda: (self.submit(R.ADMIN, case_id, 9, {"received_count": 98}, photo=True),
                        self.act(R.ADMIN, case_id, 9, "approve")),
        }
        for n in range(2, target):
            steps[n]()
        self.assertEqual(Case.objects.get(pk=case_id).current_stage, target)


class CreateCaseTests(ChainHelpers):
    def test_create_submits_stage_one_and_opens_stage_two(self):
        detail = self.create_case()
        self.assertEqual(detail["code"], f"XN-{detail['id']:04d}")
        self.assertEqual(detail["status"], "IN_PROGRESS")
        self.assertEqual(detail["current_stage"], 2)
        self.assertEqual(self.stage(detail, 1)["status"], "DONE")
        self.assertEqual(self.stage(detail, 1)["data"]["conclusion"], "Xonadon talabga javob beradi")
        self.assertEqual(self.stage(detail, 2)["status"], "ACTIVE")
        self.assertIsNotNone(self.stage(detail, 2)["due_at"])
        self.assertTrue(all(s["status"] == "WAITING" for s in detail["stages"][2:]))
        self.assertEqual(len(detail["stages"]), 10)
        self.assertEqual([e["action"] for e in detail["events"]], ["create", "submit"])
        self.assertEqual(len(self.stage(detail, 1)["photos"]), 1)
        self.assertAlmostEqual(detail["latitude"], 40.998878, places=6)

    def test_admin_may_create(self):
        self.create_case(role=R.ADMIN)

    def test_create_requires_photo_and_role(self):
        body = {"name": "X", "owner_full_name": "Y", "latitude": 41, "longitude": 69, "package_id": self.package.pk,
                "conclusion": "ok", "photo_ids": []}
        self.assertEqual(self.client_for(R.INSPECTOR).post("/api/cases", body, format="json").status_code, 400)
        body["photo_ids"] = [self.upload_photo(R.HR)]
        self.assertEqual(self.client_for(R.HR).post("/api/cases", body, format="json").status_code, 403)

    def test_cannot_attach_someone_elses_photo(self):
        foreign = self.upload_photo(R.HR)
        body = {"name": "X", "owner_full_name": "Y", "latitude": 41, "longitude": 69, "package_id": self.package.pk,
                "conclusion": "ok", "photo_ids": [foreign]}
        response = self.client_for(R.INSPECTOR).post("/api/cases", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Case.objects.count(), 0)


class FullChainTests(ChainHelpers):
    def test_happy_path_to_done(self):
        case_id = self.create_case()["id"]
        owner = self.client_for(R.ADMIN).post(f"/api/cases/{case_id}/owner-account").json()
        self.assertTrue(owner["temp_password"])
        owner_user = User.objects.get(username=owner["username"])
        self.assertEqual(owner_user.role, R.OWNER)
        owner_user.must_change_password = False
        owner_user.save()

        self.advance_to(case_id, 5)
        d = self.submit(R.WAREHOUSE, case_id, 5)
        self.assertEqual(self.stage(d, 5)["status"], "SUBMITTED")
        self.assertEqual(Product.objects.get(pk=self.product_a.pk).balance, Decimal("90"))
        self.assertEqual(Product.objects.get(pk=self.product_b.pk).balance, Decimal("6"))
        self.assertEqual(StockMove.objects.filter(case_id=case_id, type="OUT").count(), 2)
        self.act(R.ADMIN, case_id, 5, "approve")
        self.assertEqual(StockMove.objects.filter(case_id=case_id, type="OUT").count(), 2)  # approve: no 2nd OUT

        self.submit(R.INCUBATOR, case_id, 6, {"chicks_count": "100"}, photo=True)
        self.act(R.ADMIN, case_id, 6, "approve")
        self.submit(R.LOGISTICS, case_id, 7, photo=True)
        self.act(R.ADMIN, case_id, 7, "approve")
        self.submit(R.VET, case_id, 8, {"health": "normal"}, photo=True)
        self.act(R.ADMIN, case_id, 8, "approve")
        d = self.submit(owner_user, case_id, 9, {"received_count": 97}, photo=True)
        self.assertEqual(self.stage(d, 9)["data"], {"received_count": 97})
        self.act(R.ADMIN, case_id, 9, "approve")
        self.submit(R.ACCOUNTANT, case_id, 10, {"financing_type": "Grant", "amount": 12000000, "contract_no": "N-1"})
        self.act(R.ADMIN, case_id, 10, "approve", expect=403)  # stage 10 approver is SUPERADMIN
        d = self.act(R.SUPERADMIN, case_id, 10, "approve")
        self.assertEqual(d["status"], "DONE")
        self.assertIsNotNone(d["done_at"])
        self.assertTrue(all(s["status"] == "DONE" for s in d["stages"]))
        self.assertEqual(StageEvent.objects.filter(case_id=case_id).count(), 2 + 2 + 7 * 2)
        self.assertFalse(any(d["can"][k] for k in ("submit", "approve", "return", "reject")))
        # finished case refuses actions
        self.act(R.SUPERADMIN, case_id, 10, "approve", expect=409)

    def test_wrong_role_is_403(self):
        case_id = self.create_case()["id"]
        self.submit(R.DIRECTOR, case_id, 2, expect=403)
        self.submit(R.INSPECTOR, case_id, 2, expect=403)
        self.submit(R.OWNER, case_id, 2, expect=404)  # an owner does not even see a foreign case
        self.advance_to(case_id, 4)
        self.submit(R.WAREHOUSE, case_id, 4, photo=True, expect=403)
        self.submit(R.BUILDER, case_id, 4, photo=True)
        self.act(R.BUILDER, case_id, 4, "approve", expect=403)
        self.act(R.DIRECTOR, case_id, 4, "approve", expect=403)
        self.act(R.SUPERADMIN, case_id, 4, "approve")  # SUPERADMIN can always approve

    def test_no_skipping(self):
        case_id = self.create_case()["id"]
        self.submit(R.BUILDER, case_id, 4, photo=True, expect=409)
        self.submit(R.DIRECTOR, case_id, 3, expect=409)
        self.act(R.ADMIN, case_id, 4, "approve", expect=409)
        self.submit(R.SUPERADMIN, case_id, 5, expect=409)
        self.submit(R.INSPECTOR, case_id, 1, {"conclusion": "x"}, expect=409)  # stage 1 is DONE
        self.submit(R.HR, case_id, 2)
        self.submit(R.HR, case_id, 2, expect=409)  # already done
        self.submit(R.HR, case_id, 99, expect=404)

    def test_approve_requires_submitted(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 4)
        self.act(R.ADMIN, case_id, 4, "approve", expect=409)

    def test_validation_errors(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 4)
        d = self.submit(R.BUILDER, case_id, 4, expect=400)  # no photo
        self.assertIn("photo_ids", d["errors"])
        self.submit(R.BUILDER, case_id, 4, photo=True)
        self.act(R.ADMIN, case_id, 4, "approve")
        self.submit(R.WAREHOUSE, case_id, 5)
        self.act(R.ADMIN, case_id, 5, "approve")
        d = self.submit(R.INCUBATOR, case_id, 6, {}, photo=True, expect=400)
        self.assertIn("chicks_count", d["errors"])
        self.submit(R.INCUBATOR, case_id, 6, {"chicks_count": "abc"}, photo=True, expect=400)
        self.submit(R.INCUBATOR, case_id, 6, {"chicks_count": 0}, photo=True, expect=400)
        self.submit(R.INCUBATOR, case_id, 6, {"chicks_count": 50}, photo=True)
        self.act(R.ADMIN, case_id, 6, "approve")
        self.submit(R.LOGISTICS, case_id, 7, photo=True)
        self.act(R.ADMIN, case_id, 7, "approve")
        d = self.submit(R.VET, case_id, 8, {"health": "excellent"}, photo=True, expect=400)
        self.assertIn("health", d["errors"])


class ReturnRejectTests(ChainHelpers):
    def test_approver_returns_submitted_stage(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 4)
        self.submit(R.BUILDER, case_id, 4, photo=True)
        self.act(R.ADMIN, case_id, 4, "return", {"reason": ""}, expect=400)
        self.act(R.BUILDER, case_id, 4, "return", {"reason": "x"}, expect=403)
        d = self.act(R.ADMIN, case_id, 4, "return", {"reason": "Tom chala"})
        st = self.stage(d, 4)
        self.assertEqual(st["status"], "RETURNED")
        self.assertEqual(st["return_reason"], "Tom chala")
        self.assertEqual(d["events"][-1]["action"], "return")
        d = self.submit(R.BUILDER, case_id, 4, note="Tuzatildi")  # earlier photo still counts
        self.assertEqual(self.stage(d, 4)["status"], "SUBMITTED")
        self.assertEqual(self.stage(d, 4)["return_reason"], "")

    def test_hr_sends_inspection_back(self):
        case_id = self.create_case()["id"]
        d = self.act(R.HR, case_id, 2, "return", {"reason": "Pasport nusxasi yo‘q"})
        self.assertEqual(d["current_stage"], 1)
        self.assertEqual(self.stage(d, 1)["status"], "RETURNED")
        self.assertEqual(self.stage(d, 2)["status"], "WAITING")
        self.assertTrue(d["can"]["return"] is False)
        self.submit(R.HR, case_id, 2, expect=409)
        d = self.submit(R.INSPECTOR, case_id, 1, {"conclusion": "Hujjat qo‘shildi", "owner_phone": "+998900000000",
                                                   "latitude": 41.1234567})
        self.assertEqual(self.stage(d, 2)["status"], "ACTIVE")
        self.assertEqual(d["owner_phone"], "+998900000000")
        self.assertAlmostEqual(d["latitude"], 41.123457, places=6)
        self.submit(R.INSPECTOR, case_id, 1, {}, expect=409)

    def test_stage_with_approver_cannot_return_previous(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 4)
        self.act(R.BUILDER, case_id, 4, "return", {"reason": "x"}, expect=409)

    def test_director_rejects_on_stage_three(self):
        case_id = self.create_case()["id"]
        r = self.client_for(R.DIRECTOR).post(f"/api/cases/{case_id}/reject", {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 403)  # stage 2 is current
        self.submit(R.HR, case_id, 2)
        r = self.client_for(R.DIRECTOR).post(f"/api/cases/{case_id}/reject", {"reason": ""}, format="json")
        self.assertEqual(r.status_code, 400)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client_for(R.DIRECTOR).post(f"/api/cases/{case_id}/reject", {"reason": "Mos emas"},
                                                 format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "REJECTED")
        self.assertEqual(r.json()["reject_reason"], "Mos emas")
        self.submit(R.DIRECTOR, case_id, 3, expect=409)
        r = self.client_for(R.ADMIN).post(f"/api/cases/{case_id}/reject", {"reason": "y"}, format="json")
        self.assertEqual(r.status_code, 409)
        notified = set(Notification.objects.filter(text__contains="RAD ETILDI").values_list("user__role", flat=True))
        self.assertEqual(notified, {R.ADMIN, R.INSPECTOR})

    def test_admin_rejects_any_time_others_cannot(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 6)
        for role in (R.HR, R.INCUBATOR, R.INSPECTOR):
            r = self.client_for(role).post(f"/api/cases/{case_id}/reject", {"reason": "x"}, format="json")
            self.assertEqual(r.status_code, 403)
        r = self.client_for(R.ADMIN).post(f"/api/cases/{case_id}/reject", {"reason": "x"}, format="json")
        self.assertEqual(r.json()["status"], "REJECTED")


class StockStageTests(ChainHelpers):
    def test_shortage_is_409_and_writes_nothing(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 5)
        Product.objects.filter(pk=self.product_b.pk).update(balance=3)
        d = self.submit(R.WAREHOUSE, case_id, 5, expect=409)
        self.assertEqual(d["missing"], [{"product_id": self.product_b.pk, "name": "Yem", "unit": "qop",
                                          "need": 4.0, "have": 3.0}])
        self.assertEqual(Product.objects.get(pk=self.product_a.pk).balance, Decimal("100"))
        self.assertFalse(StockMove.objects.filter(case_id=case_id).exists())
        self.assertEqual(CaseStage.objects.get(case_id=case_id, number=5).status, "ACTIVE")
        self.assertFalse(StageEvent.objects.filter(case_id=case_id, stage_number=5).exists())

    def test_return_of_warehouse_stage_puts_stock_back(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 5)
        self.submit(R.WAREHOUSE, case_id, 5)
        self.act(R.ADMIN, case_id, 5, "return", {"reason": "Noto‘g‘ri miqdor"})
        self.assertEqual(Product.objects.get(pk=self.product_a.pk).balance, Decimal("100"))
        self.submit(R.WAREHOUSE, case_id, 5)
        self.assertEqual(Product.objects.get(pk=self.product_a.pk).balance, Decimal("90"))
        self.assertEqual(StockMove.objects.filter(case_id=case_id, type="OUT").count(), 4)
        self.assertEqual(StockMove.objects.filter(case_id=case_id, type="IN").count(), 2)


class VisibilityTests(ChainHelpers):
    def test_owner_sees_only_own_case_and_acts_on_stage_nine(self):
        own = self.create_case()["id"]
        other = self.create_case()["id"]
        owner = self.users[R.OWNER]
        Case.objects.filter(pk=own).update(owner_user=owner)
        client = self.client_for(owner)
        self.assertEqual([c["id"] for c in client.get("/api/cases").json()], [own])
        self.assertEqual(client.get(f"/api/cases/{other}").status_code, 404)
        self.assertEqual(client.get(f"/api/cases/{other}/pdf").status_code, 404)
        self.assertEqual(client.get(f"/api/cases/{own}").status_code, 200)
        self.advance_to(own, 9)
        self.advance_to(other, 9)
        self.assertEqual([c["id"] for c in client.get("/api/cases?mine=1").json()], [own])
        self.submit(owner, other, 9, {"received_count": 1}, photo=True, expect=404)
        second_owner = User.objects.create_user(username="owner2", password="x", role=R.OWNER)
        Case.objects.filter(pk=other).update(owner_user=second_owner)
        self.submit(owner, other, 9, {"received_count": 1}, photo=True, expect=404)
        d = self.submit(owner, own, 9, {"received_count": 99}, photo=True)
        self.assertEqual(self.stage(d, 9)["status"], "SUBMITTED")

    def test_mine_and_can_flags(self):
        case_id = self.create_case()["id"]
        hr = self.client_for(R.HR)
        self.assertEqual([c["id"] for c in hr.get("/api/cases?mine=1").json()], [case_id])
        self.assertEqual(self.client_for(R.BUILDER).get("/api/cases?mine=1").json(), [])
        self.assertEqual(len(self.client_for(R.BUILDER).get("/api/cases").json()), 1)  # staff see all
        can = hr.get(f"/api/cases/{case_id}").json()["can"]
        self.assertEqual({k: can[k] for k in ("submit", "approve", "return", "reject")},
                         {"submit": True, "approve": False, "return": True, "reject": False})
        self.advance_to(case_id, 4)
        self.submit(R.BUILDER, case_id, 4, photo=True)
        self.assertEqual([c["id"] for c in self.client_for(R.ADMIN).get("/api/cases?mine=1").json()], [case_id])
        can = self.client_for(R.ADMIN).get(f"/api/cases/{case_id}").json()["can"]
        self.assertTrue(can["approve"] and can["return"] and can["reject"] and not can["submit"])
        item = self.client_for(R.ADMIN).get("/api/cases?stage=4&status=IN_PROGRESS&search=karim").json()[0]
        self.assertEqual(item["current_stage_status"], "SUBMITTED")
        self.assertEqual(self.client_for(R.ADMIN).get("/api/cases?stage=5").json(), [])


class NotificationTests(ChainHelpers):
    def test_queued_once_per_event_after_commit(self):
        hr2 = User.objects.create_user(username="hr2", password="x", role=R.HR)
        User.objects.create_user(username="hr3", password="x", role=R.HR, is_active=False)
        case_id = self.create_case()["id"]
        rows = Notification.objects.filter(text__contains="YANGI VAZIFA")
        self.assertEqual(set(rows.values_list("user_id", flat=True)), {self.users[R.HR].pk, hr2.pk})
        n = rows.first()
        self.assertEqual(n.button_path, f"/cases/{case_id}")
        self.assertIn(f"Operatsiya ID: #XN-{case_id:04d}-2", n.text)
        self.assertIn("<b>Bosqich:</b> 2. HR tekshiruvi", n.text)
        # the same event key never creates a second row
        key = n.event_key.rsplit(":u", 1)[0]
        with self.captureOnCommitCallbacks(execute=True):
            queue([self.users[R.HR]], text="x", button_path="/", event_key=key)
        self.assertEqual(Notification.objects.filter(user=self.users[R.HR]).count(), 1)

    def test_not_queued_when_action_fails(self):
        case_id = self.create_case()["id"]
        before = Notification.objects.count()
        self.submit(R.BUILDER, case_id, 4, photo=True, expect=409)
        self.assertEqual(Notification.objects.count(), before)

    def test_recipients_per_event(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 4)
        self.submit(R.BUILDER, case_id, 4, photo=True)
        sub = Notification.objects.filter(text__contains="TASDIQLASH KUTILMOQDA")
        self.assertEqual(list(sub.values_list("user__role", flat=True)), [R.ADMIN])
        self.act(R.ADMIN, case_id, 4, "return", {"reason": "Qayta"})
        ret = Notification.objects.filter(text__contains="QAYTARILDI")
        self.assertEqual(list(ret.values_list("user__role", flat=True)), [R.BUILDER])
        self.assertIn("<b>Izoh:</b> Qayta", ret.first().text)

    def test_stage_nine_goes_to_owner_or_admin(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 9)
        users = set(Notification.objects.filter(text__contains="9. Parrandani").values_list("user__role", flat=True))
        self.assertEqual(users, {R.ADMIN})
        other = self.create_case()["id"]
        Case.objects.filter(pk=other).update(owner_user=self.users[R.OWNER])
        self.advance_to(other, 9)
        owner_rows = Notification.objects.filter(user=self.users[R.OWNER], text__contains="YANGI VAZIFA")
        self.assertEqual(owner_rows.count(), 1)


class ReminderTests(ChainHelpers):
    def test_overdue_reminder_once_per_day(self):
        case_id = self.create_case()["id"]
        CaseStage.objects.filter(case_id=case_id, number=2).update(due_at=timezone.now() - timedelta(hours=1))
        before = Notification.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_overdue_reminders(), 1)
        rows = Notification.objects.filter(text__contains="MUDDAT O‘TDI")
        self.assertEqual(set(rows.values_list("user__role", flat=True)), {R.HR, R.ADMIN})
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_overdue_reminders(), 0)
            call_command("send_reminders", stdout=io.StringIO())
        self.assertEqual(Notification.objects.count(), before + 2)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_overdue_reminders(timezone.now() + timedelta(days=1)), 1)
        self.assertEqual(Notification.objects.count(), before + 4)
        self.assertTrue(self.client_for(R.HR).get(f"/api/cases/{case_id}").json()["is_overdue"])

    def test_not_overdue_is_ignored(self):
        self.create_case()
        self.assertEqual(send_overdue_reminders(), 0)


class FileTests(ChainHelpers):
    def test_file_access_rules(self):
        detail = self.create_case()
        photo = self.stage(detail, 1)["photos"][0]
        path = f"/api/files/photo/{photo['id']}"
        self.assertEqual(self.client_for(R.VET).get(path).status_code, 200)
        self.assertEqual(self.client_for(R.OWNER).get(path).status_code, 403)
        self.assertEqual(APIClient().get(path).status_code, 401)
        self.assertEqual(APIClient().get(photo["url"]).status_code, 200)  # signed URL works in <img src>
        self.assertEqual(APIClient().get(path + "?sig=photo-1:bad:sig").status_code, 401)
        other_kind = photo["url"].replace("/photo/", "/document/")
        self.assertIn(APIClient().get(other_kind).status_code, (401, 404))
        Case.objects.filter(pk=detail["id"]).update(owner_user=self.users[R.OWNER])
        self.assertEqual(self.client_for(R.OWNER).get(path).status_code, 200)

    def test_unattached_file_only_for_uploader(self):
        pid = self.upload_photo(R.VET)
        self.assertEqual(self.client_for(R.VET).get(f"/api/files/photo/{pid}").status_code, 200)
        self.assertEqual(self.client_for(R.HR).get(f"/api/files/photo/{pid}").status_code, 403)

    def test_document_upload_and_type_check(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        client = self.client_for(R.HR)
        bad = client.post("/api/files/document", {"file": SimpleUploadedFile("x.exe", b"MZ")}, format="multipart")
        self.assertEqual(bad.status_code, 400)
        ok = client.post("/api/files/document", {"file": SimpleUploadedFile("pasport.pdf", b"%PDF-1.4")},
                         format="multipart")
        self.assertEqual(ok.status_code, 201)
        case_id = self.create_case()["id"]
        d = self.act(R.HR, case_id, 2, "submit", {"note": "ok", "document_ids": [ok.json()["id"]]})
        self.assertEqual(self.stage(d, 2)["documents"][0]["original_name"], "pasport.pdf")
        response = self.client_for(R.DIRECTOR).get(f"/api/files/document/{ok.json()['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])

    def test_invalid_image_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        r = self.client_for(R.HR).post("/api/files/photo", {"image": SimpleUploadedFile("a.png", b"nope")},
                                       format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_cleanup_removes_old_unattached(self):
        from .models import Photo

        pid = self.upload_photo(R.VET)
        self.create_case()
        Photo.objects.update(created_at=timezone.now() - timedelta(days=2))
        call_command("cleanup_files", stdout=io.StringIO())
        self.assertFalse(Photo.objects.filter(pk=pid).exists())
        self.assertEqual(Photo.objects.count(), 1)  # the attached one stays


class PdfAndDashboardTests(ChainHelpers):
    def test_pdf(self):
        case_id = self.create_case()["id"]
        self.act(R.HR, case_id, 2, "submit", {"note": "Hujjatlar to‘liq — ʻOʻzbekʼ №1"})
        r = self.client_for(R.DIRECTOR).get(f"/api/cases/{case_id}/pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_dashboard(self):
        case_id = self.create_case()["id"]
        self.create_case()
        self.client_for(R.ADMIN).post(f"/api/cases/{case_id}/reject", {"reason": "x"}, format="json")
        r = self.client_for(R.DIRECTOR).get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["per_stage"][1]["active"], 1)
        self.assertEqual((d["in_progress"], d["rejected"], d["done"], d["total"]), (1, 1, 0, 2))
        self.assertEqual(len(d["events"]), 5)
        self.assertEqual(self.client_for(R.BUILDER).get("/api/dashboard").status_code, 403)


class DeleteTests(ChainHelpers):
    """O‘chirish (soft delete): xonadon, mahsulot, paket."""

    def test_admin_deletes_case_it_disappears_everywhere(self):
        detail = self.create_case()
        cid = detail["id"]
        self.assertEqual(self.client_for(R.HR).delete(f"/api/cases/{cid}").status_code, 403)
        self.assertEqual(self.client_for(R.ADMIN).delete(f"/api/cases/{cid}").status_code, 204)
        self.assertEqual(self.client_for(R.ADMIN).get(f"/api/cases/{cid}").status_code, 404)
        self.assertEqual(self.client_for(R.HR).get("/api/cases?mine=1").json(), [])
        self.assertEqual(self.client_for(R.ADMIN).get("/api/cases").json(), [])
        self.assertTrue(Case.all_objects.filter(pk=cid, deleted_at__isnull=False).exists())
        self.assertTrue(StageEvent.objects.filter(case_id=cid).exists())  # history kept
        # The next case gets a new code, never the deleted one's.
        self.assertNotEqual(self.create_case()["code"], detail["code"])

    def test_product_delete_refused_while_in_package(self):
        admin = self.client_for(R.ADMIN)
        response = admin.delete(f"/api/products/{self.product_a.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("paket", response.json()["detail"])
        loose = Product.objects.create(name="Mix", unit="kg", category="boshqa", balance=5)
        self.assertEqual(self.client_for(R.WAREHOUSE).delete(f"/api/products/{loose.pk}").status_code, 204)
        self.assertNotIn(loose.pk, [p["id"] for p in admin.get("/api/products").json()])
        # The name is free again.
        self.assertEqual(admin.post("/api/products", {"name": "Mix", "unit": "kg", "category": "boshqa"},
                                    format="json").status_code, 201)

    def test_package_delete_refused_while_case_in_progress(self):
        self.create_case()
        admin = self.client_for(R.ADMIN)
        response = admin.delete(f"/api/packages/{self.package.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("XN-", response.json()["detail"])
        self.assertEqual(self.client_for(R.WAREHOUSE).delete(f"/api/packages/{self.package.pk}").status_code, 403)


class AdminActsForAnyStageTests(ChainHelpers):
    def test_admin_can_confirm_hr_and_director_stages(self):
        cid = self.create_case()["id"]
        detail = self.client_for(R.ADMIN).get(f"/api/cases/{cid}").json()
        self.assertTrue(detail["can"]["submit"])  # HR stage, acted on by the Administrator
        self.submit(R.ADMIN, cid, 2)
        self.submit(R.ADMIN, cid, 3)
        self.assertEqual(Case.objects.get(pk=cid).current_stage, 4)
        # Other roles still cannot touch a stage that is not theirs.
        self.submit(R.WAREHOUSE, cid, 4, photo=True, expect=403)
