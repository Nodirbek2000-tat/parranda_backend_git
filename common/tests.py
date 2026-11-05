"""Health check, OpenAPI schema/docs access and ``DATABASE_URL`` parsing (API_CONTRACT_V2 §3)."""

from __future__ import annotations

import io
import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.db.utils import OperationalError
from django.test import SimpleTestCase, override_settings

from common.roles import Role
from common.testing import BaseAPITestCase, make_user
from config.database import DatabaseUrlError, parse_database_url


class HealthTests(BaseAPITestCase):
    def test_ok_without_auth_and_with_bad_header(self):
        for headers in ({}, {"HTTP_AUTHORIZATION": "Bearer broken"}):
            response = self.client.get("/api/health", **headers)
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual((body["status"], body["db"]), ("ok", "ok"))
            self.assertRegex(body["time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+05:00$")
        self.assertEqual(self.client.post("/api/health").status_code, 405)

    def test_database_failure_is_503_without_details(self):
        with mock.patch("common.health.connection.cursor", side_effect=OperationalError("disk I/O error")), \
                self.assertLogs("nnp.health", level="ERROR"), self.assertLogs("django.request", level="ERROR"):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(set(body), {"status", "db", "time"})
        self.assertEqual((body["status"], body["db"]), ("error", "error"))
        self.assertNotIn("disk", response.content.decode())


class SchemaAccessTests(BaseAPITestCase):
    def test_debug_off_requires_admin(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/schema").status_code, 401)
        self.assertEqual(self.client.get("/api/docs").status_code, 401)
        for user in (self.warehouse, self.owner_a):
            self.client.force_authenticate(user=user)
            self.assertEqual(self.client.get("/api/schema").status_code, 403)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/schema")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["info"]["title"], "NNP API")
        self.assertEqual(schema["info"]["version"], "3.0.0")
        self.assertIn("/api/stats/overview", schema["paths"])
        self.assertEqual(set(schema["components"]["securitySchemes"]), {"jwtAuth", "botToken"})
        self.assertIn("openapi: 3", self.client.get("/api/schema?format=yaml").content.decode())
        docs = self.client.get("/api/docs")
        self.assertEqual(docs.status_code, 200)
        self.assertIn("swagger-ui", docs.content.decode())
        self.assertNotIn("cdn.jsdelivr", docs.content.decode())

    def test_accept_application_json(self):
        """JSON clients (Web App proxy, integration script) send ``Accept: application/json``: no ``406``."""
        self.client.force_authenticate(user=None)
        response = self.client.get("/api/schema", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertIn("detail", response.json())
        self.client.force_authenticate(user=self.owner_a)
        self.assertEqual(self.client.get("/api/schema", HTTP_ACCEPT="application/json").status_code, 403)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/schema", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["openapi"].startswith("3."))

    def test_admin_session_login_opens_docs(self):
        superuser = make_user("root", Role.ADMIN, is_superuser=True, is_staff=True)
        self.client.force_login(superuser)
        self.assertEqual(self.client.get("/api/docs").status_code, 200)
        self.assertEqual(self.client.get("/api/schema").status_code, 200)

    @override_settings(DEBUG=True)
    def test_debug_on_is_public(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/schema").status_code, 200)
        self.assertEqual(self.client.get("/api/docs").status_code, 200)


class SchemaValidationTests(SimpleTestCase):
    def test_schema_validates_without_warnings(self):
        """Acceptance (§3.2): ``manage.py spectacular --validate --fail-on-warn`` exits 0."""
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "schema.yaml")
            call_command("spectacular", "--validate", "--fail-on-warn", "--file", target,
                         stdout=io.StringIO(), stderr=io.StringIO())
            self.assertTrue(os.path.getsize(target) > 10_000)


class DatabaseUrlTests(SimpleTestCase):
    def test_postgres(self):
        config = parse_database_url("postgres://nnp:p%40ss@db.example:5433/nnp_prod?sslmode=require", conn_max_age=60)
        self.assertEqual(
            config,
            {"ENGINE": "django.db.backends.postgresql", "NAME": "nnp_prod", "USER": "nnp", "PASSWORD": "p@ss",
             "HOST": "db.example", "PORT": "5433", "CONN_MAX_AGE": 60, "CONN_HEALTH_CHECKS": True,
             "OPTIONS": {"sslmode": "require"}},
        )
        self.assertEqual(parse_database_url("postgresql://localhost/nnp")["PORT"], "")

    def test_sqlite(self):
        self.assertEqual(parse_database_url("sqlite:///db.sqlite3")["NAME"], "db.sqlite3")
        self.assertEqual(parse_database_url("sqlite:////var/lib/nnp.db")["NAME"], "/var/lib/nnp.db")
        self.assertEqual(parse_database_url("sqlite:///E:/data/nnp.db")["NAME"], "E:/data/nnp.db")
        self.assertEqual(parse_database_url("sqlite:///x.db")["OPTIONS"]["transaction_mode"], "IMMEDIATE")

    def test_errors(self):
        for url in ("mysql://u@h/db", "postgres://host", "postgres://h/db?foo=1", "postgres://h:99999/db", "sqlite://"):
            with self.subTest(url=url), self.assertRaises(DatabaseUrlError):
                parse_database_url(url)


class ErrorPageMaskingTests(SimpleTestCase):
    """The DEBUG 500 page / error reports never show the database URL (it contains the password)."""

    FAKE_URL = "postgres://nnp:FAKEPASS123@db.example:5432/nnp"

    def test_database_url_is_not_a_setting(self):
        from django.conf import settings

        self.assertFalse(hasattr(settings, "DATABASE_URL"))

    @override_settings(DATABASE_URL=FAKE_URL, WEBAPP_URL="https://user:pw@example.com")
    def test_settings_and_meta_are_masked(self):
        from django.test import RequestFactory
        from django.views.debug import get_default_exception_reporter_filter

        reporter = get_default_exception_reporter_filter()
        self.assertEqual(type(reporter).__name__, "NNPExceptionReporterFilter")
        safe = reporter.get_safe_settings()
        self.assertNotIn("FAKEPASS123", str(safe))
        self.assertEqual(safe["DATABASE_URL"], reporter.cleansed_substitute)
        self.assertNotIn("FAKEPASS123", str(safe["DATABASES"]))
        request = RequestFactory().get("/", DATABASE_URL=self.FAKE_URL, SENTRY_DSN="https://k@sentry.example/1")
        meta = reporter.get_safe_request_meta(request)
        self.assertEqual(meta["DATABASE_URL"], reporter.cleansed_substitute)
        self.assertEqual(meta["SENTRY_DSN"], reporter.cleansed_substitute)
        self.assertNotEqual(meta["REQUEST_METHOD"], reporter.cleansed_substitute)
