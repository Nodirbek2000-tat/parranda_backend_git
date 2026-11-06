"""v2.1: 13-stage chain rules, owner requests, vet assignment and visits, scheduler jobs, finance, reports,
bot tasks and the data migration from the 10-stage chain."""

import importlib
import io
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.test import override_settings
from django.utils import timezone

from accounts.models import Role, User
from notify.models import Notification
from stock.models import Product, StockMove

from .growing import growing_alerts, plan_weekly_visits, visit_reminders
from .models import Case, CaseStage, Photo, Request, StageEvent, VetVisit
from .tests import ChainHelpers

R = Role


class GrowingHelpers(ChainHelpers):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.feed = Product.objects.create(name="Start yem", unit="kg", category="yem", balance=100, price=5000)
        cls.drug = Product.objects.create(name="Vaksina", unit="dona", category="dori", balance=5, price=20000)
        cls.cement = cls.product_a  # category material
        cls.owner = cls.users[R.OWNER]
        cls.vet = cls.users[R.VET]

    def growing_case(self, vet=True) -> int:
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 10)
        if vet:
            self.assign_vet(case_id, self.vet.pk)
        return case_id

    def assign_vet(self, case_id, vet_id, role=R.ADMIN, expect=200):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client_for(role).post(f"/api/cases/{case_id}/assign-vet", {"vet_id": vet_id}, format="json")
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def post(self, who, path, body=None, expect=200):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client_for(who).post(path, body or {}, format="json")
        self.assertEqual(r.status_code, expect, r.content)
        return r.json() if r.content else None

    def new_request(self, case_id, kind="FEED", who=None, expect=201, **extra):
        body = {"kind": kind, "text": "Yem tugab qoldi", "qty_kg": "50", **extra}
        return self.post(who or self.owner, f"/api/cases/{case_id}/requests", body, expect)

    def req_action(self, who, rid, action, body=None, expect=200):
        return self.post(who, f"/api/requests/{rid}/{action}", body, expect)

    def notified(self, path_or_text, *, path=True):
        qs = Notification.objects.filter(button_path=path_or_text) if path else Notification.objects.filter(
            text__contains=path_or_text)
        return set(qs.values_list("user__role", flat=True))


class ChainV21Tests(GrowingHelpers):
    def test_thirteen_stages_and_roles(self):
        d = self.create_case()
        self.assertEqual([s["number"] for s in d["stages"]], list(range(1, 14)))
        roles = {s["number"]: (s["executor_role"], s["approver_role"]) for s in d["stages"]}
        self.assertEqual(roles[10], ("ADMIN", None))
        self.assertEqual(roles[11], ("LOGISTICS", "ADMIN"))
        self.assertEqual(roles[12], ("SLAUGHTERHOUSE", "ADMIN"))
        self.assertEqual(roles[13], ("ACCOUNTANT", "SUPERADMIN"))
        kg = next(f for f in self.stage(d, 12)["fields"] if f["name"] == "total_kg")
        self.assertEqual((kg["type"], kg["decimal_places"], kg["min"]), ("decimal", 2, 0))
        self.assertEqual(User.objects.get(username="slaughterhouse").get_role_display(), "Qushxona")

    @override_settings(GROWING_DAYS=14)
    def test_growing_days_setting(self):
        case_id = self.growing_case(vet=False)
        d = self.client_for(R.ADMIN).get(f"/api/cases/{case_id}").json()
        self.assertEqual(self.stage(d, 10)["days"], 14)
        st = CaseStage.objects.get(case_id=case_id, number=10)
        self.assertEqual((st.due_at - st.activated_at).days, 14)
        self.assertEqual(d["growing"]["total_days"], 14)

    def test_stage_seven_and_nine_limits(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 7)  # stage 6: 100 chicks given
        d = self.submit(R.LOGISTICS, case_id, 7, {"delivered_healthy": 95, "died_in_transit": 6}, photo=True,
                        expect=400)
        self.assertEqual(set(d["errors"]), {"delivered_healthy", "died_in_transit"})
        self.submit(R.LOGISTICS, case_id, 7, {"delivered_healthy": -1, "died_in_transit": 0}, expect=400)
        self.submit(R.LOGISTICS, case_id, 7, {"delivered_healthy": 95, "died_in_transit": 5}, photo=True)
        self.act(R.ADMIN, case_id, 7, "approve")
        self.submit(R.VET, case_id, 8, {"health": "good"}, photo=True)
        self.act(R.ADMIN, case_id, 8, "approve")
        self.link_owner(case_id)
        d = self.submit(R.OWNER, case_id, 9, {"received_count": 96}, photo=True, expect=400)
        self.assertIn("received_count", d["errors"])
        self.submit(R.OWNER, case_id, 9, {"received_count": 95}, photo=True)

    def test_stage_twelve_and_thirteen_validation(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 12)
        for bad in (0, "-1", "abc"):
            d = self.submit(R.SLAUGHTERHOUSE, case_id, 12, {"received_count": 90, "total_kg": bad}, expect=400)
            self.assertIn("total_kg", d["errors"])
        self.submit(R.SLAUGHTERHOUSE, case_id, 12, {"received_count": 90, "total_kg": "200,5"})
        self.act(R.ADMIN, case_id, 12, "approve")
        self.submit(R.ACCOUNTANT, case_id, 13, {"financing_type": "Grant", "contract_no": "1", "revenue": 1},
                    expect=400)  # choice
        d = self.submit(R.ACCOUNTANT, case_id, 13, {"financing_type": "credit", "contract_no": "K-7",
                                                    "revenue": 5000000, "price_per_kg": 1})
        data = self.stage(d, 13)["data"]
        self.assertEqual((data["revenue"], data["price_per_kg"]), (5000000, 1))  # entered revenue wins
        self.act(R.SUPERADMIN, case_id, 13, "return", {"reason": "Shartnoma"})
        d = self.submit(R.ACCOUNTANT, case_id, 13, {"financing_type": "own", "contract_no": "K-8",
                                                    "price_per_kg": 20000})
        self.assertEqual(self.stage(d, 13)["data"]["revenue"], 4010000)

    def test_stage_activation_notifies_new_roles(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 13)
        for text, role in (("10. Parvarish", R.ADMIN), ("11. Qushxonaga", R.LOGISTICS),
                           ("12. Qushxona qabuli", R.SLAUGHTERHOUSE), ("13. Buxgalteriya", R.ACCOUNTANT)):
            rows = Notification.objects.filter(text__contains="YANGI VAZIFA").filter(text__contains=text)
            self.assertEqual(set(rows.values_list("user__role", flat=True)), {role}, text)
            self.assertEqual(rows.first().button_path, f"/cases/{case_id}")


class AssignVetTests(GrowingHelpers):
    def test_assign_vet(self):
        case_id = self.create_case()["id"]
        self.assign_vet(case_id, self.vet.pk, role=R.HR, expect=403)
        self.assign_vet(case_id, self.vet.pk, role=R.VET, expect=403)
        d = self.assign_vet(case_id, self.users[R.HR].pk, expect=400)
        self.assertIn("vet_id", d["errors"])
        blocked = User.objects.create_user(username="vet_off", password="x", role=R.VET, is_active=False)
        self.assign_vet(case_id, blocked.pk, expect=400)
        d = self.assign_vet(case_id, self.vet.pk)
        self.assertEqual(d["vet_user"]["id"], self.vet.pk)
        self.assertTrue(d["can"]["assign_vet"])
        n = Notification.objects.get(user=self.vet, text__contains="BIRIKTIRILDI")
        self.assertEqual(n.button_path, f"/cases/{case_id}")
        # changing the vet moves planned visits to the new vet
        vet2 = User.objects.create_user(username="vet2", password="x", role=R.VET)
        VetVisit.objects.create(case_id=case_id, vet=self.vet, kind="ROUTINE", week=1,
                                planned_date=timezone.localdate())
        d = self.assign_vet(case_id, vet2.pk, role=R.SUPERADMIN)
        self.assertEqual(d["vet_user"]["id"], vet2.pk)
        self.assertEqual(VetVisit.objects.get(case_id=case_id).vet, vet2)
        self.assertIsNone(self.assign_vet(case_id, None)["vet_user"])
        self.assertFalse(self.client_for(R.HR).get(f"/api/cases/{case_id}").json()["can"]["assign_vet"])


class FeedRequestTests(GrowingHelpers):
    def test_feed_full_flow_with_stock_out(self):
        case_id = self.growing_case()
        d = self.client_for(self.owner).get(f"/api/cases/{case_id}").json()
        self.assertTrue(d["can"]["create_request"] and d["can"]["request"])
        self.assertIsNone(d["finance"])  # owners do not see finance
        photo = self.upload_photo(self.owner)
        req = self.new_request(case_id, photo_ids=[photo])
        rid = req["id"]
        self.assertEqual((req["status"], req["kind"], req["qty_kg"]), ("PENDING_LOGISTICS", "FEED", 50.0))
        self.assertEqual(len(req["photos"]), 1)
        self.assertEqual(self.notified(f"/requests/{rid}"), {R.LOGISTICS})

        # role checks at the logistics step
        for who in (R.ADMIN, R.VET, self.owner, R.WAREHOUSE):
            self.req_action(who, rid, "approve", expect=404 if who in (R.VET, R.WAREHOUSE) else 403)
        self.req_action(R.ADMIN, rid, "reject", {"reason": "x"}, expect=403)
        self.req_action(R.LOGISTICS, rid, "deliver", {"product_id": self.feed.pk, "qty": 1,
                                                      "photo_ids": [self.upload_photo(R.LOGISTICS)]}, expect=409)
        lst = self.client_for(R.LOGISTICS).get("/api/requests?mine=1").json()
        self.assertEqual([r["id"] for r in lst], [rid])
        self.assertTrue(lst[0]["can"]["approve"] and lst[0]["can"]["reject"])
        req = self.req_action(R.LOGISTICS, rid, "approve")
        self.assertEqual(req["status"], "PENDING_ADMIN")
        self.assertEqual(req["logistics_by"]["role"], "LOGISTICS")
        self.assertIn(R.ADMIN, self.notified("SO‘ROV TASDIQLASH KUTILMOQDA", path=False))

        self.req_action(R.LOGISTICS, rid, "approve", expect=403)
        req = self.req_action(R.ADMIN, rid, "approve")
        self.assertEqual(req["status"], "APPROVED")
        self.assertEqual(self.notified("YEM YETKAZISH VAZIFASI", path=False), {R.LOGISTICS})

        # deliver: FEED product only, photo required, owner/admin cannot
        lphoto = self.upload_photo(R.LOGISTICS)
        body = {"product_id": self.feed.pk, "qty": "40", "photo_ids": [lphoto], "note": "Darvozagacha"}
        self.req_action(R.ADMIN, rid, "deliver", body, expect=403)
        d = self.req_action(R.LOGISTICS, rid, "deliver", {**body, "product_id": self.drug.pk}, expect=400)
        self.assertIn("product_id", d["errors"])
        self.req_action(R.LOGISTICS, rid, "deliver", {**body, "photo_ids": []}, expect=400)
        d = self.req_action(R.LOGISTICS, rid, "deliver", {**body, "qty": "150"}, expect=409)
        self.assertEqual(d["missing"][0]["product_id"], self.feed.pk)
        self.assertEqual(Product.objects.get(pk=self.feed.pk).balance, Decimal("100"))
        self.assertEqual(Request.objects.get(pk=rid).status, "APPROVED")
        self.assertIsNone(Photo.objects.get(pk=lphoto).request_id)  # rolled back

        req = self.req_action(R.LOGISTICS, rid, "deliver", body)
        self.assertEqual(req["status"], "DELIVERED")
        self.assertEqual((req["delivery"]["product_id"], req["delivery"]["qty"]), (self.feed.pk, 40.0))
        self.assertEqual([p["id"] for p in req["delivery"]["photos"]], [lphoto])
        self.assertEqual([p["id"] for p in req["photos"]], [photo])
        self.assertEqual(Product.objects.get(pk=self.feed.pk).balance, Decimal("60"))
        move = StockMove.objects.get(case_id=case_id, purpose="request")
        self.assertEqual((move.type, move.qty), ("OUT", Decimal("40")))
        self.assertEqual(self.notified("YEM YETKAZILDI", path=False), {R.OWNER})

        self.req_action(R.LOGISTICS, rid, "confirm", expect=403)
        mine = self.client_for(self.owner).get("/api/requests?mine=1").json()
        self.assertTrue(mine[0]["can"]["confirm"])
        req = self.req_action(self.owner, rid, "confirm")
        self.assertEqual((req["status"], req["done_by"]["role"]), ("DONE", "OWNER"))
        self.req_action(self.owner, rid, "confirm", expect=409)

        # finance: delivered feed is counted (40 × 5000), the stage-5 package is not counted twice
        fin = self.client_for(R.ADMIN).get(f"/api/cases/{case_id}").json()["finance"]
        self.assertEqual(fin["feed_cost"], 200000)
        self.assertEqual(fin["total_cost"], 1000 + 800000 + 200000)
        self.assertIsNone(fin["revenue"])
        self.assertEqual(self.client_for(R.ADMIN).get(f"/api/requests/{rid}").json()["id"], rid)
        self.assertEqual(self.client_for(R.ADMIN).get(f"/api/stock/moves?case={case_id}").json()[0]["purpose"],
                         "request")

    def test_reject_with_reason_and_admin_close(self):
        case_id = self.growing_case()
        rid = self.new_request(case_id)["id"]
        self.req_action(R.LOGISTICS, rid, "reject", {"reason": ""}, expect=400)
        req = self.req_action(R.LOGISTICS, rid, "reject", {"reason": "Ombor bo‘sh"})
        self.assertEqual((req["status"], req["reject_reason"]), ("REJECTED", "Ombor bo‘sh"))
        self.assertEqual(self.notified("SO‘ROV RAD ETILDI", path=False), {R.OWNER})
        self.req_action(R.LOGISTICS, rid, "approve", expect=409)
        # ADMIN may close a delivered request too
        rid = self.new_request(case_id)["id"]
        self.req_action(R.LOGISTICS, rid, "approve")
        self.req_action(R.ADMIN, rid, "approve")
        self.req_action(R.LOGISTICS, rid, "deliver", {"product_id": self.feed.pk, "qty": 5,
                                                      "photo_ids": [self.upload_photo(R.LOGISTICS)]})
        self.assertEqual(self.req_action(R.ADMIN, rid, "confirm")["status"], "DONE")

    def test_create_permissions_and_growing_period(self):
        case_id = self.create_case()["id"]
        self.advance_to(case_id, 9)
        self.link_owner(case_id)
        d = self.new_request(case_id, expect=409)  # stage 9: not growing yet
        self.assertIn("parvarish", d["detail"])
        self.submit(R.OWNER, case_id, 9, {"received_count": 90}, photo=True)
        self.act(R.ADMIN, case_id, 9, "approve")
        for role in (R.ADMIN, R.LOGISTICS, R.SUPERADMIN):
            self.new_request(case_id, who=role, expect=403)
        other_owner = User.objects.create_user(username="owner_b", password="x", role=R.OWNER)
        self.new_request(case_id, who=other_owner, expect=404)
        self.post(self.owner, f"/api/cases/{case_id}/requests", {"kind": "EGG", "text": "x"}, expect=400)
        self.post(self.owner, f"/api/cases/{case_id}/requests", {"kind": "FEED", "text": ""}, expect=400)
        rid = self.new_request(case_id)["id"]
        # leaving the growing period: create and approve are refused, reject still works
        self.submit(R.ADMIN, case_id, 10)
        self.new_request(case_id, expect=409)
        self.req_action(R.LOGISTICS, rid, "approve", expect=409)
        self.assertFalse(self.client_for(R.LOGISTICS).get(f"/api/requests/{rid}").json()["can"]["approve"])
        self.req_action(R.LOGISTICS, rid, "reject", {"reason": "Parvarish tugadi"})

    def test_visibility(self):
        case_id = self.growing_case()
        feed = self.new_request(case_id)["id"]
        vet = self.new_request(case_id, kind="VET", text="Jo‘jalar kasal")["id"]
        ids = lambda who: sorted(r["id"] for r in self.client_for(who).get("/api/requests").json())  # noqa: E731
        self.assertEqual(ids(R.ADMIN), sorted([feed, vet]))
        self.assertEqual(ids(self.owner), sorted([feed, vet]))
        self.assertEqual(ids(R.LOGISTICS), [feed])
        self.assertEqual(ids(R.VET), [vet])
        self.assertEqual(ids(R.HR), [])
        vet2 = User.objects.create_user(username="vet2", password="x", role=R.VET)
        self.assertEqual(ids(vet2), [])  # the case has another vet
        self.assertEqual(self.client_for(R.HR).get(f"/api/requests/{feed}").status_code, 404)
        self.assertEqual(self.client_for(R.LOGISTICS).get(f"/api/requests/{vet}").status_code, 404)
        self.assertEqual(ids(R.ADMIN), sorted([feed, vet]))
        admin = self.client_for(R.ADMIN)
        self.assertEqual([r["id"] for r in admin.get("/api/requests?kind=VET").json()], [vet])
        self.assertEqual([r["id"] for r in admin.get("/api/requests?status=PENDING_LOGISTICS,DONE").json()], [feed])
        d = admin.get(f"/api/cases/{case_id}").json()
        self.assertEqual(len(d["requests"]), 2)
        Case.objects.filter(pk=case_id).update(deleted_at=timezone.now())
        self.assertEqual(ids(R.ADMIN), [])


class VetRequestAndVisitTests(GrowingHelpers):
    def test_vet_request_creates_visit_and_completing_it_closes_request(self):
        case_id = self.growing_case(vet=False)
        rid = self.new_request(case_id, kind="VET", text="Jo‘jalar yemayapti")["id"]
        req = Request.objects.get(pk=rid)
        self.assertEqual(req.status, "PENDING_ADMIN")
        self.assertEqual(self.notified(f"/requests/{rid}"), {R.ADMIN, R.VET})  # no vet yet: all vets
        self.req_action(R.LOGISTICS, rid, "approve", expect=404)
        self.req_action(R.VET, rid, "approve", expect=403)
        d = self.req_action(R.ADMIN, rid, "approve", expect=400)
        self.assertIn("veterinar", d["detail"])
        self.assign_vet(case_id, self.vet.pk)
        req = self.req_action(R.ADMIN, rid, "approve")
        self.assertEqual(req["status"], "APPROVED")
        visit = VetVisit.objects.get(request_id=rid)
        self.assertEqual((visit.kind, visit.vet, visit.planned_date), ("REQUEST", self.vet, timezone.localdate()))
        self.assertEqual(req["visit_id"], visit.pk)
        self.assertEqual(self.notified(f"/visits/{visit.pk}"), {R.VET})

        v = self.client_for(R.VET).get(f"/api/visits/{visit.pk}").json()
        self.assertEqual((v["kind"], v["request"], v["can"]), ("REQUEST", rid, {"complete": True, "cancel": False}))
        self.assertEqual(self.client_for(self.owner).get(f"/api/visits/{visit.pk}").json()["can"],
                         {"complete": False, "cancel": False})
        self.assertEqual(self.client_for(R.LOGISTICS).get(f"/api/visits/{visit.pk}").status_code, 404)

        body = {"chicks_count": 88, "health": "normal", "note": "Vaksina qilindi",
                "photo_ids": [self.upload_photo(R.VET)], "drugs": [{"product_id": self.drug.pk, "qty": 2}]}
        self.post(R.ADMIN, f"/api/visits/{visit.pk}/complete", body, expect=403)
        vet2 = User.objects.create_user(username="vet2", password="x", role=R.VET)
        self.post(vet2, f"/api/visits/{visit.pk}/complete", body, expect=404)  # not visible to another vet
        d = self.post(R.VET, f"/api/visits/{visit.pk}/complete", {**body, "photo_ids": []}, expect=400)
        self.assertIn("photo_ids", d["errors"])
        d = self.post(R.VET, f"/api/visits/{visit.pk}/complete",
                      {**body, "drugs": [{"product_id": self.feed.pk, "qty": 1}]}, expect=400)
        self.assertIn("drugs", d["errors"])
        d = self.post(R.VET, f"/api/visits/{visit.pk}/complete",
                      {**body, "drugs": [{"product_id": self.drug.pk, "qty": 9}]}, expect=409)
        self.assertEqual(d["missing"][0]["have"], 5.0)
        self.assertEqual(Product.objects.get(pk=self.drug.pk).balance, Decimal("5"))

        v = self.post(R.VET, f"/api/visits/{visit.pk}/complete", body)
        self.assertEqual((v["status"], v["chicks_count"], v["health"]), ("DONE", 88, "normal"))
        self.assertEqual(v["drugs"], [{"product_id": self.drug.pk, "product_name": "Vaksina", "unit": "dona",
                                       "qty": 2.0, "price": 20000.0, "cost": 40000}])
        self.assertEqual(len(v["photos"]), 1)
        self.assertEqual(Product.objects.get(pk=self.drug.pk).balance, Decimal("3"))
        self.assertTrue(StockMove.objects.filter(case_id=case_id, purpose="visit", type="OUT").exists())
        self.assertEqual(Request.objects.get(pk=rid).status, "DONE")
        self.post(R.VET, f"/api/visits/{visit.pk}/complete", body, expect=409)
        fin = self.client_for(R.ADMIN).get(f"/api/cases/{case_id}").json()["finance"]
        self.assertEqual(fin["vet_cost"], 40000)

    def test_cancel_visit_rejects_request(self):
        case_id = self.growing_case()
        rid = self.new_request(case_id, kind="VET", text="Kasal")["id"]
        self.req_action(R.ADMIN, rid, "approve")
        visit = VetVisit.objects.get(request_id=rid)
        self.post(R.VET, f"/api/visits/{visit.pk}/cancel", {"reason": "x"}, expect=403)
        self.post(R.ADMIN, f"/api/visits/{visit.pk}/cancel", {"reason": ""}, expect=400)
        v = self.post(R.ADMIN, f"/api/visits/{visit.pk}/cancel", {"reason": "Veterinar kasal"})
        self.assertEqual((v["status"], v["cancel_reason"]), ("CANCELLED", "Veterinar kasal"))
        req = Request.objects.get(pk=rid)
        self.assertEqual(req.status, "REJECTED")
        self.assertIn("Veterinar kasal", req.reject_reason)

    def test_visit_lists(self):
        case_id = self.growing_case()
        today = timezone.localdate()
        old = VetVisit.objects.create(case_id=case_id, vet=self.vet, kind="ROUTINE", week=1,
                                      planned_date=today - timedelta(days=2))
        new = VetVisit.objects.create(case_id=case_id, vet=self.vet, kind="ROUTINE", week=2,
                                      planned_date=today + timedelta(days=5))
        rows = self.client_for(R.VET).get("/api/visits?mine=1").json()
        self.assertEqual([r["id"] for r in rows], [old.pk, new.pk])
        self.assertTrue(rows[0]["is_overdue"])
        self.assertEqual(len(self.client_for(R.ADMIN).get(f"/api/visits?case={case_id}").json()), 2)
        self.assertEqual(len(self.client_for(self.owner).get("/api/visits").json()), 2)
        self.assertEqual(self.client_for(R.HR).get("/api/visits").json(), [])
        d = self.client_for(R.ADMIN).get(f"/api/cases/{case_id}").json()
        self.assertEqual([v["id"] for v in d["visits"]], [old.pk, new.pk])
        # sending to the slaughterhouse cancels the planned routine visits
        self.submit(R.ADMIN, case_id, 10)
        self.assertEqual(set(VetVisit.objects.values_list("status", flat=True)), {"CANCELLED"})


class SchedulerTests(GrowingHelpers):
    def _start(self, case_id):
        return timezone.localdate(CaseStage.objects.get(case_id=case_id, number=10).activated_at)

    def test_weekly_visits_idempotent(self):
        case_id = self.growing_case()
        no_vet = self.growing_case(vet=False)
        start = self._start(case_id)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(plan_weekly_visits(start + timedelta(days=5)), 0)  # day 7 is 2 days away
            self.assertEqual(plan_weekly_visits(start + timedelta(days=6)), 1)  # due within 1 day
            self.assertEqual(plan_weekly_visits(start + timedelta(days=6)), 0)
            self.assertEqual(plan_weekly_visits(start + timedelta(days=7)), 0)
        visit = VetVisit.objects.get(case_id=case_id)
        self.assertEqual((visit.kind, visit.week, visit.planned_date, visit.vet),
                         ("ROUTINE", 1, start + timedelta(days=7), self.vet))
        self.assertFalse(VetVisit.objects.filter(case_id=no_vet).exists())
        self.assertEqual(Notification.objects.filter(button_path=f"/visits/{visit.pk}").count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(plan_weekly_visits(start + timedelta(days=20)), 1)  # week 3 (no back-fill of 2)
            self.assertEqual(plan_weekly_visits(start + timedelta(days=60)), 1)  # last week: 42 ≤ 45
            self.assertEqual(plan_weekly_visits(start + timedelta(days=61)), 0)
        self.assertEqual(sorted(VetVisit.objects.filter(case_id=case_id).values_list("week", flat=True)), [1, 3, 6])

    def test_visit_due_and_overdue_reminders(self):
        case_id = self.growing_case()
        start = self._start(case_id)
        with self.captureOnCommitCallbacks(execute=True):
            plan_weekly_visits(start + timedelta(days=6))
        visit = VetVisit.objects.get(case_id=case_id)
        day = visit.planned_date
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(visit_reminders(day - timedelta(days=1)), 0)
            self.assertEqual(visit_reminders(day), 1)
            self.assertEqual(visit_reminders(day), 0)
        self.assertEqual(self.notified("BUGUN VETERINAR TASHRIFI", path=False), {R.VET})
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(visit_reminders(day + timedelta(days=1)), 1)
            self.assertEqual(visit_reminders(day + timedelta(days=1)), 0)
            self.assertEqual(visit_reminders(day + timedelta(days=2)), 1)  # once per day
        self.assertEqual(self.notified("TASHRIF MUDDATI O‘TDI", path=False), {R.VET, R.ADMIN})
        self.assertEqual(Notification.objects.filter(text__contains="TASHRIF MUDDATI", user=self.vet).count(), 2)

    def test_growing_alerts_idempotent(self):
        case_id = self.growing_case()
        stage = CaseStage.objects.get(case_id=case_id, number=10)
        end = timezone.localdate(stage.due_at)
        code = Case.objects.get(pk=case_id).code
        for days_before, sent in ((3, 0), (2, 1), (1, 0), (2, 0)):  # each run commits like the real scheduler
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(growing_alerts(end - timedelta(days=days_before)), sent, days_before)
        rows = Notification.objects.filter(text__contains="PARVARISH YAKUNLANMOQDA")
        self.assertEqual(set(rows.values_list("user__role", flat=True)), {R.ADMIN})
        self.assertIn(f"{code}: 45 kunlik parvarish yakuniga yetmoqda — qushxonaga yuborish vaqti", rows[0].text)
        self.assertEqual(rows[0].button_path, f"/cases/{case_id}")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(growing_alerts(end), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(growing_alerts(end + timedelta(days=3)), 0)
        self.assertEqual(rows.count(), 2)
        self.submit(R.ADMIN, case_id, 10)  # sent to the slaughterhouse: no more alerts
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(growing_alerts(end + timedelta(days=30)), 0)

    def test_send_reminders_command(self):
        from django.core.management import call_command

        self.growing_case()
        out = io.StringIO()
        call_command("send_reminders", stdout=out)
        self.assertIn("yangi tashriflar: 0", out.getvalue())


class BotAndStockAccessTests(GrowingHelpers):
    def test_bot_tasks_have_kind_title_path(self):
        case_id = self.growing_case()
        rid = self.new_request(case_id)["id"]
        logistics = self.users[R.LOGISTICS]
        logistics.telegram_id = 777
        logistics.save()
        VetVisit.objects.create(case_id=case_id, vet=self.vet, kind="ROUTINE", week=1,
                                planned_date=timezone.localdate())
        self.vet.telegram_id = 778
        self.vet.save()
        from rest_framework.test import APIClient

        bot = APIClient()
        bot.credentials(HTTP_X_BOT_TOKEN="bot-secret")
        items = bot.get("/api/bot/tasks/777").json()
        req = next(i for i in items if i["kind"] == "request")
        self.assertEqual((req["id"], req["path"], req["action"]), (rid, f"/requests/{rid}", "approve"))
        self.assertIn("title", req)
        items = bot.get("/api/bot/tasks/778").json()
        visit = next(i for i in items if i["kind"] == "visit")
        self.assertTrue(visit["path"].startswith("/visits/"))
        self.assertEqual(visit["planned_date"], timezone.localdate().isoformat())

    def test_products_readable_by_logistics_and_vet(self):
        for role in (R.LOGISTICS, R.VET):
            client = self.client_for(role)
            self.assertEqual(client.get("/api/products?category=yem").status_code, 200)
            self.assertEqual(client.post("/api/products", {"name": "X", "unit": "kg", "category": "yem"},
                                         format="json").status_code, 403)
        self.assertEqual(self.client_for(R.HR).get("/api/products").status_code, 403)
        vets = self.client_for(R.ADMIN).get("/api/users?role=VET").json()
        self.assertEqual([u["role"] for u in vets], ["VET"])


class ReportTests(GrowingHelpers):
    def test_reports_shape_and_xlsx(self):
        from openpyxl import load_workbook

        case_id = self.growing_case()
        rid = self.new_request(case_id)["id"]
        self.req_action(R.LOGISTICS, rid, "approve")
        self.req_action(R.ADMIN, rid, "approve")
        self.req_action(R.LOGISTICS, rid, "deliver", {"product_id": self.feed.pk, "qty": 2,
                                                      "photo_ids": [self.upload_photo(R.LOGISTICS)]})
        other = self.create_case()["id"]
        CaseStage.objects.filter(case_id=other, number=2).update(due_at=timezone.now() - timedelta(hours=5))
        admin = self.client_for(R.ADMIN)

        o = admin.get("/api/reports/overview").json()
        self.assertEqual(o["cases"], {"total": 2, "in_progress": 2, "done": 0, "rejected": 0})
        self.assertEqual(len(o["per_stage"]), 13)
        self.assertEqual(o["per_stage"][9]["active"], 1)
        self.assertEqual(o["timing"]["open_late"], 1)
        self.assertEqual(o["requests"]["total"], 1)
        self.assertEqual(o["requests"]["by_kind"], {"FEED": 1, "VET": 0})
        self.assertEqual(set(o["visits"]), {"planned", "done", "overdue", "cancelled"})
        self.assertEqual(o["finance"]["in_progress"]["cases"], 2)

        rows = admin.get("/api/reports/cases").json()
        late = next(r for r in rows if r["id"] == other)
        self.assertEqual((late["late_stages"], late["worst_delay"]["stage"]), (1, 2))
        self.assertIsNone(late["worst_delay"]["who"])
        grow = next(r for r in rows if r["id"] == case_id)
        self.assertEqual(grow["cost"], 1000 + 800000 + 10000)

        staff = admin.get("/api/reports/staff").json()
        logi = next(r for r in staff if r["user"]["role"] == "LOGISTICS")
        self.assertEqual(logi["requests_handled"], 1)
        self.assertGreaterEqual(logi["stages_submitted"], 1)
        adm = next(r for r in staff if r["user"]["role"] == "ADMIN")
        self.assertGreaterEqual(adm["approvals"], 5)

        fin = admin.get("/api/reports/finance").json()
        self.assertEqual(len(fin["rows"]), 2)
        self.assertEqual(fin["totals"]["feed_cost"], 10000)

        for name in ("overview", "cases", "staff", "finance"):
            r = admin.get(f"/api/reports/{name}?format=xlsx")
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual(r["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.assertIn(f"hisobot-{name}-", r["Content-Disposition"])
            wb = load_workbook(io.BytesIO(r.content))
            self.assertGreaterEqual(wb.worksheets[0].max_row, 2)

        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(admin.get(f"/api/reports/overview?date_from={tomorrow}").json()["cases"]["total"], 0)
        district = Case.objects.exclude(district="").values_list("district", flat=True).first()
        self.assertTrue(district)
        total = admin.get("/api/reports/overview").json()["cases"]["total"]
        same = admin.get("/api/reports/overview", {"district": f"{district} tumani"}).json()
        self.assertEqual(same["district"], f"{district} tumani")
        self.assertEqual(same["cases"]["total"], Case.objects.filter(district__iexact=district).count())
        self.assertLessEqual(same["cases"]["total"], total)
        self.assertEqual(admin.get("/api/reports/finance", {"district": "Mavjud emas tumani"}).json()["rows"], [])
        self.assertEqual(admin.get("/api/reports/cases?date_to=bad").status_code, 400)
        self.assertEqual(self.client_for(R.SUPERADMIN).get("/api/reports/finance").status_code, 200)
        for role in (R.HR, R.DIRECTOR, R.OWNER):
            self.assertEqual(self.client_for(role).get("/api/reports/overview").status_code, 403)


class DataMigrationTests(GrowingHelpers):
    migration = importlib.import_module("cases.migrations.0005_v21_stages")

    def _old_layout(self, case_id, *, current, status="IN_PROGRESS", old10_status="WAITING", old10_data=None):
        """Turn a 13-stage case into what a v2 (10-stage) database holds."""
        CaseStage.objects.filter(case_id=case_id, number__in=(10, 11, 12)).delete()
        CaseStage.objects.filter(case_id=case_id, number=13).update(
            number=10, key="accounting", status=old10_status, data=old10_data or {})
        CaseStage.objects.filter(case_id=case_id, number__lt=current).update(status="DONE")
        if current < 10:
            CaseStage.objects.filter(case_id=case_id, number=current).update(status="ACTIVE")
        Case.objects.filter(pk=case_id).update(current_stage=current, status=status)

    def test_case_at_old_stage_ten(self):
        case_id = self.create_case()["id"]
        old = {"financing_type": "Grant", "amount": 12000000, "contract_no": "N-1"}
        self._old_layout(case_id, current=10, old10_status="SUBMITTED", old10_data=old)
        StageEvent.objects.create(case_id=case_id, stage_number=10, action="submit", note="old accounting")
        photo = Photo.objects.get(case_id=case_id)
        Photo.objects.filter(pk=photo.pk).update(stage=10)
        move = StockMove.objects.create(product=self.product_a, type="OUT", qty=1, balance_after=99, case_id=case_id)

        self.migration.forwards(django_apps, None)
        self.migration.forwards(django_apps, None)  # idempotent

        stages = {s.number: s for s in CaseStage.objects.filter(case_id=case_id)}
        self.assertEqual(sorted(stages), list(range(1, 14)))
        self.assertEqual((stages[13].key, stages[13].status, stages[13].data), ("accounting", "SUBMITTED", old))
        for n, key in ((10, "growing"), (11, "pickup"), (12, "slaughter")):
            self.assertEqual((stages[n].key, stages[n].status, stages[n].note), (key, "DONE", "migratsiya"))
        self.assertEqual(Case.objects.get(pk=case_id).current_stage, 13)
        self.assertEqual(StageEvent.objects.filter(case_id=case_id, note="migratsiya").count(), 3)
        self.assertEqual(StageEvent.objects.get(note="old accounting").stage_number, 13)
        self.assertEqual(Photo.objects.get(pk=photo.pk).stage, 13)
        self.assertEqual(StockMove.objects.get(pk=move.pk).purpose, "package")

        d = self.client_for(R.SUPERADMIN).get(f"/api/cases/{case_id}").json()
        self.assertEqual((d["current_stage"], d["can"]["approve"]), (13, True))
        d = self.act(R.SUPERADMIN, case_id, 13, "approve")
        self.assertEqual(d["status"], "DONE")

    def test_done_and_early_cases(self):
        done_id = self.create_case()["id"]
        self._old_layout(done_id, current=10, status="DONE", old10_status="DONE",
                         old10_data={"financing_type": "Grant", "amount": 1, "contract_no": "N"})
        early_id = self.create_case()["id"]
        self._old_layout(early_id, current=4)
        self.migration.forwards(django_apps, None)

        done = Case.objects.get(pk=done_id)
        self.assertEqual((done.status, done.current_stage), ("DONE", 13))
        self.assertTrue(all(s.status == "DONE" for s in done.stages.all()))
        d = self.client_for(R.ADMIN).get(f"/api/cases/{done_id}").json()
        self.assertEqual(len(d["stages"]), 13)

        early = Case.objects.get(pk=early_id)
        self.assertEqual(early.current_stage, 4)
        statuses = dict(early.stages.values_list("number", "status"))
        self.assertEqual([statuses[n] for n in (10, 11, 12, 13)], ["WAITING"] * 4)
        self.assertFalse(StageEvent.objects.filter(case_id=early_id, note="migratsiya").exists())
        # the early case runs the new chain normally
        self.submit(R.BUILDER, early_id, 4, photo=True)

        self.migration.backwards(django_apps, None)
        self.assertEqual(Case.objects.get(pk=done_id).stages.count(), 10)
        self.assertEqual(Case.objects.get(pk=done_id).current_stage, 10)


class CaseListSummaryTests(GrowingHelpers):
    def test_list_has_summary_and_district_filter(self):
        cid = self.growing_case()
        rows = self.client_for(R.ADMIN).get("/api/cases").json()
        row = next(r for r in rows if r["id"] == cid)
        summary = row["summary"]
        self.assertEqual(summary["birds_received"], 98)
        self.assertEqual(summary["birds"], 98)
        self.assertEqual(summary["days_since_delivery"], 0)
        self.assertEqual(summary["growing_days"], 45)
        self.assertEqual(summary["vet_visits"], 0)
        self.assertGreater(summary["total_cost"], 0)
        self.assertTrue(summary["revenue_is_estimate"])
        self.assertEqual(summary["revenue"], int(98 * 2.5 * 30000))
        # district filter: "Pop tumani" matches a household saved as "Pop"
        admin = self.client_for(R.ADMIN)
        self.assertEqual([r["id"] for r in admin.get("/api/cases", {"district": "Pop tumani"}).json()], [cid])
        self.assertEqual(admin.get("/api/cases", {"district": "Urgut tumani"}).json(), [])
