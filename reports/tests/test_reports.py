"""Report export (API_CONTRACT_V3 §11, §19.1 “Reports”).

Dataset = the V2 §1.3 worked example (household XN, BREEDER 500 + BROILER 100, two reports) plus a second
household in another district with broilers only, on a frozen clock (04.11.2026 12:00 Tashkent).
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from unittest import mock
from urllib.parse import unquote

from django.db import connection
from django.test.utils import CaptureQueriesContext
from openpyxl import load_workbook

from audit.models import AuditLog
from common.roles import Role
from common.testing import make_household, make_product, make_user
from farm.models import Household, MovementType, PoultryType
from stats.tests.helpers import FrozenClockTestCase, batch, created_on, move, report, staff, worked_example
from warehouse import services as warehouse_services

from reports import pdf, services
from reports.registry import REPORTS

PERIOD = "date_from=2026-11-01&date_to=2026-11-04"
ALL_KEYS = [report_def.key for report_def in REPORTS]


class ReportTestCase(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        cls.vet = make_user("vet1", Role.VET)
        cls.logistics = make_user("log1", Role.LOGISTICS)
        data = worked_example(cls.admin)
        cls.household = data["household"]
        cls.owner = cls.household.owner
        cls.other = make_household("Bog‘ishamol 5-uy", "owner_other")
        Household.objects.filter(pk=cls.other.pk).update(district="Payariq tumani")
        cls.other.refresh_from_db()
        created_on(cls.other, date(2026, 10, 1))
        broiler = batch(cls.admin, 400, date(2026, 10, 25), PoultryType.BROILER)
        move(cls.admin, broiler, cls.other, MovementType.DISTRIBUTE, 200, date(2026, 10, 26))
        report(cls.other, date(2026, 11, 2), "12.50", "20.00", deaths=2, weight="1500")
        cls.feed = make_product("Yem PK-1", min_stock="50")
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse, product_id=cls.feed.pk, transaction_type="IN", quantity=Decimal("500"),
            document_no="YX-1", lot_no="L-1", expiry_date=date(2026, 11, 20),
        )
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse, product_id=cls.feed.pk, transaction_type="OUT", quantity=Decimal("120"),
            document_no="YX-2", household=cls.household,
        )

    def get(self, key: str, query: str = PERIOD, fmt: str = "json"):
        return self.client.get(f"/api/reports/{key}?format={fmt}&{query}")


class ReportListAndAccessTests(ReportTestCase):
    def test_reports_available_per_role(self):
        expected = {
            self.admin: ALL_KEYS,
            self.warehouse: ["daily_production", "monthly_production", "poultry_movements", "feed_consumption",
                             "stock_balance"],
            self.logistics: ["logistics_efficiency"],
            self.vet: ["mortality", "vet_status"],
            self.owner: ["daily_production", "monthly_production", "household_results"],
        }
        for user, keys in expected.items():
            self.login_as(user)
            response = self.client.get("/api/reports")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["key"] for item in response.data], keys, user.username)
        self.login_as(self.admin)
        first = self.client.get("/api/reports").data[0]
        self.assertEqual(first["formats"], ["xlsx", "pdf", "json"])
        self.assertIn("poultry_type", first["filters"])

    def test_forbidden_unknown_and_bad_format(self):
        cases = [
            (self.warehouse, "households_activity"),
            (self.logistics, "daily_production"),
            (self.vet, "stock_balance"),
            (self.owner, "stock_balance"),
            (self.owner, "mortality"),
        ]
        for user, key in cases:
            self.login_as(user)
            self.assertEqual(self.get(key).status_code, 403, (user.username, key))
        self.login_as(self.admin)
        self.assertEqual(self.get("no_such_report").status_code, 404)
        response = self.get("daily_production", fmt="doc")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["format"], ["“doc” ruxsat etilgan qiymat emas."])
        response = self.get("daily_production", "date_from=2026-11-05&date_to=2026-11-01")
        self.assertEqual(response.status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/reports").status_code, 401)

    def test_owner_forced_to_own_household(self):
        self.login_as(self.owner)
        data = self.get("daily_production").data
        self.assertEqual({row["household_code"] for row in data["rows"]}, {self.household.code})
        self.assertEqual(data["filters"]["household"], self.household.pk)
        self.assertEqual(self.get("daily_production", f"{PERIOD}&household={self.other.pk}").status_code, 404)
        results = self.get("household_results").data
        self.assertEqual([row["code"] for row in results["rows"]], [self.household.code])

    def test_regions_endpoint(self):
        self.login_as(self.vet)
        response = self.client.get("/api/households/regions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [{"region": "Samarqand viloyati",
                                          "districts": ["Navobod tumani", "Payariq tumani"]}])
        self.login_as(self.logistics)
        self.assertEqual(self.client.get("/api/households/regions").status_code, 403)


class ReportNumbersTests(ReportTestCase):
    def test_household_results_reproduce_worked_example(self):
        self.login_as(self.admin)
        example = "date_from=2026-11-01&date_to=2026-11-03"  # V2 §1.3 period
        data = self.get("household_results", f"{example}&household={self.household.pk}").data
        row = data["rows"][0]
        self.assertEqual(row["birds_end"], 596)
        self.assertEqual(row["meat_kg"], 66.0)
        self.assertEqual(row["feed_kg"], 121.0)
        self.assertEqual(row["fcr"], 1.83)  # 121 / 66
        self.assertEqual(row["avg_weight_g"], 1908.8)  # (1800×500 + 2000×596) / 1096
        self.assertEqual(row["deaths"], 4)
        self.assertEqual(row["mortality_pct"], 0.8)
        self.assertEqual(row["discipline_pct"], 66.7)
        self.assertEqual(row["stock_out"], 0)  # the OUT happened on 04.11
        row = self.get("household_results", f"{PERIOD}&household={self.household.pk}").data["rows"][0]
        self.assertEqual(row["stock_out"], 1)

    def test_daily_production_totals_equal_stats_overview(self):
        self.login_as(self.admin)
        data = self.get("daily_production").data
        overview = self.client.get(f"/api/stats/overview?{PERIOD}").data["kpis"]
        self.assertEqual(data["totals"]["meat_kg"], overview["meat_kg"]["value"])
        self.assertEqual(data["totals"]["feed_kg"], overview["feed_kg"]["value"])
        self.assertEqual(data["totals"]["fcr"], overview["fcr"]["value"])
        self.assertEqual(data["totals"]["avg_weight_g"], overview["avg_weight_g"]["value"])
        # 78.5 kg meat, 141 kg feed → FCR 1.796… → 1.80
        self.assertEqual((data["totals"]["meat_kg"], data["totals"]["fcr"]), (78.5, 1.8))
        self.assertEqual(data["row_count"], 3)
        first = data["rows"][0]
        self.assertEqual(first["date"], "2026-11-01")
        self.assertEqual(first["birds"], 500)
        self.assertEqual((first["meat_kg"], first["feed_kg"], first["fcr"], first["avg_weight_g"]),
                         (25.5, 55.0, 2.16, 1800.0))
        self.assertEqual(set(first), {"date", "household_code", "household", "district", "birds", "feed_kg", "meat_kg",
                                      "fcr", "avg_weight_g", "deaths", "note"})
        self.assertEqual([column["key"] for column in data["columns"]][:3], ["date", "household_code", "household"])
        self.assertEqual(data["period"], {"date_from": "2026-11-01", "date_to": "2026-11-04", "days": 4})

    def test_filters_change_rows(self):
        self.login_as(self.admin)
        rows = self.get("daily_production", f"{PERIOD}&district=Payariq tumani").data["rows"]
        self.assertEqual({row["household_code"] for row in rows}, {self.other.code})
        rows = self.get("mortality", f"{PERIOD}&poultry_type=BROILER").data["rows"]
        self.assertEqual({row["code"] for row in rows}, {self.household.code, self.other.code})
        rows = self.get("mortality", f"{PERIOD}&poultry_type=BREEDER").data["rows"]
        self.assertEqual([(row["code"], row["deaths"]) for row in rows], [(self.household.code, 4)])
        rows = self.get("household_results", f"{PERIOD}&poultry_type=BROILER").data["rows"]
        self.assertEqual({row["code"]: row["birds_end"] for row in rows}, {self.household.code: 100, self.other.code: 198})
        data = self.get("poultry_movements", f"{PERIOD}&poultry_type=BREEDER").data
        self.assertEqual([row["type"] for row in data["rows"]], ["Nobud bo‘lish"])

    def test_stock_balance_and_feed(self):
        self.login_as(self.warehouse)
        data = self.get("stock_balance").data
        row = data["rows"][0]
        self.assertEqual((row["opening"], row["in_qty"], row["out_qty"], row["closing"], row["balance"]),
                         (0.0, 500.0, 120.0, 380.0, 380.0))
        self.assertEqual(row["nearest_expiry"], "2026-11-20")
        self.assertFalse(row["is_low"])
        # TZ §7 "Parranda": the center's birds per batch are part of the stock report.
        birds = [item for item in data["rows"] if item["category"] == "Parranda (markaz)"]
        center = sum(item["balance"] for item in birds)
        summary = {item["label"]: item["value"] for item in data["summary"]}
        self.assertEqual(summary["Markazdagi parranda, dona"], str(int(center)))
        self.assertTrue(all(item["unit"] == "dona" for item in birds))
        feed = self.get("feed_consumption").data
        by_code = {item["code"]: item for item in feed["rows"]}
        self.assertEqual(by_code[self.household.code]["feed_issued_kg"], 120.0)
        self.assertEqual(by_code[self.household.code]["feed_reported_kg"], 121.0)
        self.assertEqual(feed["totals"]["feed_issued_kg"], 120.0)
        self.assertEqual((by_code[self.household.code]["meat_kg"], by_code[self.household.code]["fcr"]), (66.0, 1.83))
        self.assertEqual(by_code[self.other.code]["fcr"], 1.6)  # 20 / 12.5

    def test_monthly_production_aligned_to_months(self):
        self.login_as(self.admin)
        data = self.get("monthly_production", "date_from=2026-10-15&date_to=2026-11-04").data
        self.assertEqual(data["period"]["date_from"], "2026-10-01")
        self.assertEqual(data["period"]["date_to"], "2026-11-04")
        self.assertEqual([row["month"] for row in data["rows"]], ["10.2026", "11.2026"])
        november = data["rows"][1]
        self.assertEqual((november["meat_kg"], november["reports"], november["households_reported"]), (78.5, 3, 2))
        self.assertEqual(november["fcr"], 1.8)  # 141 / 78.5 = 1.796…
        default = self.get("monthly_production", "").data
        self.assertEqual(default["period"]["date_from"], "2026-01-01")

    def test_every_report_json_within_query_budget(self):
        self.login_as(self.admin)
        for key in ALL_KEYS:
            self.get(key)  # warm readiness caches
            with CaptureQueriesContext(connection) as ctx:
                response = self.get(key)
            self.assertEqual(response.status_code, 200, (key, response.data))
            self.assertLessEqual(len(ctx.captured_queries), 12, key)
            self.assertFalse(response.data["truncated"])


class ReportFileTests(ReportTestCase):
    def test_every_report_as_xlsx_and_pdf(self):
        self.login_as(self.admin)
        for key in ALL_KEYS:
            response = self.get(key, fmt="xlsx")
            self.assertEqual(response.status_code, 200, key)
            self.assertEqual(response["Content-Type"],
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.assertEqual(response["Cache-Control"], "no-store")
            workbook = load_workbook(io.BytesIO(response.content))
            self.assertEqual(workbook.sheetnames, ["Hisobot", "Xulosa"])
            sheet = workbook["Hisobot"]
            self.assertEqual(sheet["A1"].value, "Navobod naslli parranda qisqarmasi")
            self.assertTrue(sheet["A1"].font.bold)
            self.assertEqual(sheet["A3"].value, "Davr: 01.11.2026 – 04.11.2026" if key != "monthly_production"
                             else "Davr: 01.11.2026 – 04.11.2026")
            self.assertTrue(sheet["A6"].font.bold)
            self.assertEqual(sheet["A6"].fill.fgColor.rgb[-6:], "12372A")
            self.assertEqual(sheet.freeze_panes, "A7")

            response = self.get(key, fmt="pdf")
            self.assertEqual(response.status_code, 200, key)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF"), key)
            self.assertGreaterEqual(response.content.count(b"/Type /Page\n") + response.content.count(b"/Type /Page "),
                                    1, key)

    def test_xlsx_numbers_formats_totals_and_filename(self):
        self.login_as(self.admin)
        response = self.get("daily_production", f"{PERIOD}&district=Navobod tumani", fmt="xlsx")
        disposition = response["Content-Disposition"]
        self.assertIn('filename="kunlik-ishlab-chiqarish_2026-11-01_2026-11-04.xlsx"', disposition)
        encoded = disposition.split("filename*=UTF-8''")[1]
        self.assertEqual(unquote(encoded), "Kunlik ishlab chiqarish 01.11.2026-04.11.2026.xlsx")
        sheet = load_workbook(io.BytesIO(response.content))["Hisobot"]
        self.assertEqual(sheet["A4"].value, "Filtrlar: Tuman — Navobod tumani")
        headers = [cell.value for cell in sheet[6]]
        self.assertEqual(headers, ["Sana", "Xonadon kodi", "Xonadon", "Tuman", "Parranda", "Yem kg", "Go‘sht kg",
                                   "FCR", "O‘rtacha vazn g", "Nobud", "Izoh"])
        first = sheet[7]
        self.assertEqual(first[0].value.date(), date(2026, 11, 1))
        self.assertEqual(first[0].number_format, "DD.MM.YYYY")
        self.assertEqual(first[4].number_format, "#,##0")
        self.assertEqual((first[5].value, first[6].value), (55, 25.5))
        self.assertEqual(first[6].number_format, "#,##0.00")
        self.assertEqual(first[8].number_format, "#,##0.0")
        totals = sheet[9]
        self.assertEqual(totals[0].value, "Jami")
        self.assertTrue(totals[0].font.bold)
        self.assertEqual(totals[6].value, 66)
        self.assertEqual(sheet.auto_filter.ref, "A6:K8")
        summary = load_workbook(io.BytesIO(response.content))["Xulosa"]
        labels = [row[0].value for row in summary.iter_rows()]
        self.assertIn("Yaratdi", labels)

    def test_export_is_audited_and_json_is_not(self):
        self.login_as(self.warehouse)
        self.get("stock_balance")
        self.assertFalse(AuditLog.objects.filter(module="reports").exists())
        self.get("stock_balance", fmt="pdf")
        row = AuditLog.objects.get(module="reports")
        self.assertEqual((row.action, row.object_type), ("export", "Report"))
        self.assertEqual(row.after["key"], "stock_balance")
        self.assertEqual(row.after["format"], "pdf")
        self.assertEqual(row.after["filters"]["date_from"], "2026-11-01")

    def test_row_limits(self):
        self.login_as(self.admin)
        with mock.patch.dict(services.ROW_LIMITS, {"pdf": 1, "json": 1}):
            response = self.get("daily_production", fmt="pdf")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["non_field_errors"],
                             ["Ma’lumot juda ko‘p (3 qator). Davrni yoki filtrlarni toraytiring."])
            data = self.get("daily_production").data
            self.assertTrue(data["truncated"])
            self.assertEqual((len(data["rows"]), data["row_count"]), (1, 3))


class PdfTextTests(ReportTestCase):
    def test_uzbek_characters_survive(self):
        text = "O‘g‘il, ma’lumot, G‘alla"
        self.assertEqual(pdf.pdf_text(text), text)
        self.assertIn(ord("‘"), pdf.CMAP)
        self.assertIn(ord("’"), pdf.CMAP)
        self.assertEqual(pdf.pdf_text("Oʻgʻil maʼlumot №5"), "O‘g‘il ma’lumot No5")
        self.assertNotIn("?", pdf.pdf_text(text))
        from reports.core import ReportData, col

        content = pdf.build(
            ReportData([col("name", "Xonadon")], [{"name": text}], {}, [("G‘alla", "ma’lumot")]),
            company="NNP", title="Sinov hisobot", period_line="Davr", filters_line="Filtrlar: yo‘q",
            generated_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
        )
        self.assertTrue(content.startswith(b"%PDF"))


class ReportVerifierTests(ReportTestCase):
    """Independent verification of §11.1 / §11.4 (regressions of the bugs found in review)."""

    def test_unsupported_filters_are_ignored(self):
        self.login_as(self.admin)
        for key in ("households_activity", "feed_consumption", "vet_status", "logistics_efficiency"):
            plain = self.get(key).data
            filtered = self.get(key, f"{PERIOD}&poultry_type=BREEDER").data
            self.assertEqual(filtered["rows"], plain["rows"], key)
            self.assertNotIn("poultry_type", filtered["filters"], key)
        plain = self.get("stock_balance").data
        filtered = self.get("stock_balance", f"{PERIOD}&district=Payariq tumani&household={self.other.pk}").data
        self.assertEqual(filtered["rows"], plain["rows"])
        self.assertEqual(filtered["filters"], {})
        response = self.get("stock_balance", f"{PERIOD}&district=Payariq tumani", fmt="xlsx")
        sheet = load_workbook(io.BytesIO(response.content))["Hisobot"]
        self.assertEqual(sheet["A4"].value, "Filtrlar: yo‘q")

    def test_xlsx_never_stores_user_text_as_formula(self):
        from farm.models import DailyReport

        payload = '=HYPERLINK("http://example.invalid","x")'
        DailyReport.objects.filter(household=self.household).update(note=payload)
        Household.objects.filter(pk=self.household.pk).update(name="=1+1")
        self.login_as(self.admin)
        response = self.get("daily_production", fmt="xlsx")
        self.assertEqual(response.status_code, 200)
        sheet = load_workbook(io.BytesIO(response.content))["Hisobot"]
        headers = [cell.value for cell in sheet[6]]
        note_col, name_col = headers.index("Izoh"), headers.index("Xonadon")
        cells = [row for row in sheet.iter_rows(min_row=7) if row[1].value == self.household.code]
        self.assertTrue(cells)
        for row in cells:
            self.assertEqual(row[note_col].value, payload)
            self.assertEqual(row[note_col].data_type, "s")
            self.assertEqual(row[name_col].value, "=1+1")
            self.assertEqual(row[name_col].data_type, "s")
        self.assertNotIn(b"<f>", response.content)

    def test_owner_without_household_and_vet_scope(self):
        orphan = make_user("owner_none", Role.OWNER)
        self.login_as(orphan)
        self.assertEqual(self.get("daily_production").status_code, 404)
        self.login_as(self.vet)
        data = self.get("mortality", f"{PERIOD}&district=Payariq tumani").data
        self.assertEqual([row["code"] for row in data["rows"]], [self.other.code])
        self.assertEqual(self.get("mortality", f"{PERIOD}&household=999999").status_code, 404)
        self.assertEqual(self.get("mortality", f"{PERIOD}&poultry_type=DUCK").status_code, 400)

    def test_stock_balance_counts_inventory_adjustments(self):
        from warehouse import inventory

        count = inventory.create_count(actor=self.admin, product_ids=[self.feed.pk])
        inventory.update_lines(actor=self.admin, count_id=count.pk,
                               lines=[{"product": self.feed.pk, "counted_qty": Decimal("370")}])
        inventory.approve(actor=self.admin, count_id=count.pk)
        self.login_as(self.warehouse)
        row = self.get("stock_balance").data["rows"][0]
        self.assertEqual((row["in_qty"], row["out_qty"], row["adjust_qty"], row["closing"], row["balance"]),
                         (500.0, 120.0, -10.0, 370.0, 370.0))
