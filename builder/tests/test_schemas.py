"""Blueprint data validation, the formula engine and form value validation (API_CONTRACT_V4 §1.2)."""

from __future__ import annotations

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from builder import schemas
from builder.models import BlueprintKind
from common.roles import Role

from .helpers import BuilderTestCase, number_form


class FormulaTests(SimpleTestCase):
    def test_precedence_parentheses_and_unary_minus(self):
        self.assertEqual(schemas.evaluate_formula("2 + 3 * 4", {}), 14)
        self.assertEqual(schemas.evaluate_formula("(2 + 3) * 4", {}), 20)
        self.assertEqual(schemas.evaluate_formula("-a + 10", {"a": 3}), 7)
        self.assertEqual(schemas.evaluate_formula("- (a - 10)", {"a": 3}), 7)

    def test_rounding_and_null_results(self):
        self.assertEqual(schemas.evaluate_formula("a / b", {"a": 10, "b": 3}), 3.33)
        self.assertIsNone(schemas.evaluate_formula("a / b", {"a": 10, "b": 0}))
        self.assertIsNone(schemas.evaluate_formula("a + b", {"a": 1}))
        self.assertIsNone(schemas.evaluate_formula("a + 1", {"a": None}))

    def test_rejects_code(self):
        for bad in ("__import__('os')", "a ** 2", "a.b", "1 +", "(1", "a b", "", "1;2"):
            with self.subTest(bad=bad), self.assertRaises(schemas.FormulaError):
                schemas.parse_formula(bad)

    def test_names(self):
        self.assertEqual(schemas.formula_names(schemas.parse_formula("area_m2 * 10 / birds")), {"area_m2", "birds"})


class DataSchemaTests(BuilderTestCase):
    def assertDataError(self, kind, data, fragment):
        with self.assertRaises(ValidationError) as ctx:
            schemas.validate_data(kind, data)
        message = ctx.exception.detail["data"][0]
        self.assertIn(fragment, str(message))

    def test_role_schema(self):
        data = schemas.validate_data(BlueprintKind.ROLE, {
            "base_role": "WAREHOUSE", "permissions": {"warehouse": ["edit", "view", "view"], "orders": ["view"]},
        })
        self.assertEqual(data["permissions"], {"warehouse": ["view", "edit"], "orders": ["view"]})
        self.assertDataError(BlueprintKind.ROLE, {"base_role": "OWNER", "permissions": {}}, "Asosiy rol")
        self.assertDataError(BlueprintKind.ROLE, {"base_role": "VET", "permissions": {"unknown_module": ["view"]}},
                             "noma’lum bo‘lim")
        self.assertDataError(BlueprintKind.ROLE, {"base_role": "VET", "permissions": {"vet": ["fly"]}},
                             "noma’lum amal")

    def test_form_schema_defaults_and_errors(self):
        data = schemas.validate_data(BlueprintKind.FORM, number_form())
        density = data["fields"][2]
        self.assertFalse(density["required"])
        self.assertEqual(density["visible_roles"], [])
        self.assertEqual(data["fields"][0]["options"], [])
        bad_key = number_form(fields=[{"key": "1abc", "label": "X", "type": "text"}])
        self.assertDataError(BlueprintKind.FORM, bad_key, "kalit")
        dup = number_form(fields=[{"key": "a", "label": "A", "type": "text"}, {"key": "a", "label": "B", "type": "text"}])
        self.assertDataError(BlueprintKind.FORM, dup, "takrorlangan")
        self.assertDataError(BlueprintKind.FORM, {"fields": []}, "kamida bitta")
        select = {"fields": [{"key": "s", "label": "S", "type": "select", "options": []}]}
        self.assertDataError(BlueprintKind.FORM, select, "variant")
        unknown = {"fields": [{"key": "c", "label": "C", "type": "computed", "formula": "x * 2"}]}
        self.assertDataError(BlueprintKind.FORM, unknown, "noma’lum maydon “x”")
        text_ref = {"fields": [{"key": "t", "label": "T", "type": "text"},
                               {"key": "c", "label": "C", "type": "computed", "formula": "t * 2"}]}
        self.assertDataError(BlueprintKind.FORM, text_ref, "raqamli")
        cycle = {"fields": [{"key": "a", "label": "A", "type": "computed", "formula": "b + 1"},
                            {"key": "b", "label": "B", "type": "computed", "formula": "a + 1"}]}
        self.assertDataError(BlueprintKind.FORM, cycle, "aylanib")
        bad_ref = {"roles": ["profile:999"], "fields": [{"key": "a", "label": "A", "type": "text"}]}
        self.assertDataError(BlueprintKind.FORM, bad_ref, "profili topilmadi")

    def test_task_process_document_schemas(self):
        form = self.form()
        data = schemas.validate_data(BlueprintKind.TASK, {"executor": "VET", "form_id": form.pk})
        self.assertEqual(data["approver"], "")
        self.assertEqual(data["next_condition"], "approved")
        self.assertDataError(BlueprintKind.TASK, {"executor": "VET", "form_id": 99999}, "Forma topilmadi")
        self.assertDataError(BlueprintKind.TASK, {"executor": "NOBODY"}, "noma’lum rol")
        self.assertDataError(BlueprintKind.TASK, {"executor": "VET", "start_date": "2026-11-10",
                                                  "end_date": "2026-11-01"}, "Tugash sanasi")
        general = self.blueprint(BlueprintKind.DOCUMENT, {"scope": "general", "title": "Umumiy"})
        self.assertDataError(BlueprintKind.TASK, {"executor": "VET", "document_id": general.pk}, "vazifa")
        task = self.task_template()
        process = schemas.validate_data(BlueprintKind.PROCESS, {"stages": [{"title": "", "task_id": task.pk}]})
        self.assertEqual(process["stages"][0]["title"], task.name)
        self.assertDataError(BlueprintKind.PROCESS, {"stages": []}, "kamida bitta")
        self.assertDataError(BlueprintKind.PROCESS, {"stages": [{"title": "X", "task_id": task.pk,
                                                                 "condition": "maybe"}]}, "shart")
        self.assertDataError(BlueprintKind.DOCUMENT, {"scope": "role", "title": "X"}, "tizim rolini")


class ValueValidationTests(BuilderTestCase):
    def fields(self, *extra):
        return schemas.validate_data(BlueprintKind.FORM, {"fields": list(extra)})["fields"]

    def clean(self, fields, values, user=None, previous=None):
        user = user or self.admin
        return schemas.validate_values(fields, values, user, previous=previous, files=schemas.FileChecker(user))

    def errors(self, fields, values, user=None):
        with self.assertRaises(ValidationError) as ctx:
            self.clean(fields, values, user)
        return ctx.exception.detail["data"]

    def test_types(self):
        fields = self.fields(
            {"key": "n", "label": "N", "type": "number", "validation": {"min": 1, "max": 5}},
            {"key": "d", "label": "D", "type": "date"},
            {"key": "t", "label": "T", "type": "time"},
            {"key": "p", "label": "P", "type": "phone"},
            {"key": "s", "label": "S", "type": "select", "options": ["A", "B"]},
            {"key": "m", "label": "M", "type": "multiselect", "options": ["A", "B"]},
            {"key": "g", "label": "G", "type": "geo"},
            {"key": "b", "label": "B", "type": "bool"},
            {"key": "x", "label": "X", "type": "text", "validation": {"pattern": "[A-Z]{2}", "max_length": 2}},
        )
        cleaned = self.clean(fields, {"n": "3", "d": "2026-11-05", "t": "09:30", "p": "90 123 45 67", "s": "A",
                                      "m": ["B", "A", "B"], "g": {"lat": 40.1, "lng": 71.7}, "b": False, "x": "AB"})
        self.assertEqual(cleaned["n"], 3)
        self.assertEqual(cleaned["p"], "+998901234567")
        self.assertEqual(cleaned["m"], ["B", "A"])
        self.assertEqual(cleaned["g"], {"lat": 40.1, "lng": 71.7})
        errors = self.errors(fields, {"n": 9, "d": "05.11.2026", "t": "25:00", "p": "12345", "s": "C", "m": ["Z"],
                                      "g": {"lat": 100, "lng": 0}, "b": "yes", "x": "ab"})
        self.assertEqual(set(errors), {"n", "d", "t", "p", "s", "m", "g", "b", "x"})

    def test_required_and_computed(self):
        fields = schemas.validate_data(BlueprintKind.FORM, number_form())["fields"]
        errors = self.errors(fields, {"birds": 10})
        self.assertEqual(errors["area_m2"], ["Bu maydon to‘ldirilishi shart."])
        cleaned = self.clean(fields, {"area_m2": 30, "birds": 7, "density": 999})
        self.assertEqual(cleaned["density"], 42.86)  # client value ignored

    def test_editable_and_visible_roles(self):
        fields = self.fields(
            {"key": "a", "label": "A", "type": "text", "editable_roles": ["ADMIN"]},
            {"key": "v", "label": "V", "type": "text", "visible_roles": ["ADMIN"]},
            {"key": "o", "label": "O", "type": "text"},
        )
        cleaned = self.clean(fields, {"a": "new", "v": "x", "o": "own"}, self.vet, previous={"a": "old", "v": "keep"})
        self.assertEqual(cleaned, {"a": "old", "v": "keep", "o": "own"})
        self.assertEqual([f["key"] for f in schemas.visible_fields(fields, self.vet)], ["a", "o"])
        self.assertEqual(len(schemas.visible_fields(fields, self.admin)), 3)

    def test_files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from core import files
        from core.models import AttachmentPurpose

        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        pdf = b"%PDF-1.4 test"
        image = files.upload(actor=self.vet, file=SimpleUploadedFile("a.png", png, "image/png"),
                             purpose=AttachmentPurpose.FORM_FILE)
        document = files.upload(actor=self.vet, file=SimpleUploadedFile("a.pdf", pdf, "application/pdf"),
                                purpose=AttachmentPurpose.FORM_FILE)
        fields = self.fields({"key": "ph", "label": "Rasm", "type": "photo"}, {"key": "f", "label": "F", "type": "file"})
        checker = schemas.FileChecker(self.vet)
        cleaned = schemas.validate_values(fields, {"ph": image.pk, "f": document.pk}, self.vet, files=checker)
        self.assertEqual(cleaned, {"ph": image.pk, "f": document.pk})
        self.assertEqual(checker.new_ids, {image.pk, document.pk})
        errors = self.errors(fields, {"ph": document.pk, "f": 999999}, self.vet)
        self.assertIn("rasm", errors["ph"][0])
        self.assertIn("topilmadi", errors["f"][0])
        errors = self.errors(fields, {"f": document.pk}, self.vet2)  # someone else's upload
        self.assertIn("f", errors)

    def test_display(self):
        self.assertEqual(schemas.display_value({"type": "bool"}, True, {}), "Ha")
        self.assertEqual(schemas.display_value({"type": "date"}, "2026-11-05", {}), "05.11.2026")
        self.assertEqual(schemas.display_value({"type": "photo"}, 3, {3: "a.png"}), "a.png")
        self.assertEqual(schemas.display_value({"type": "multiselect"}, ["A", "B"], {}), "A, B")
        self.assertEqual(schemas.display_value({"type": "computed"}, 42.5, {}), "42.5")
        self.assertEqual(Role.VET, "VET")
