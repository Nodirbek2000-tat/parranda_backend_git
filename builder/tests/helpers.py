"""Shared fixtures of the Konstruktor tests."""

from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.test import override_settings

from accounts.models import User
from core.storage import reset_storage_cache
from builder import services
from builder.models import Blueprint, BlueprintKind
from common.roles import Role
from common.testing import BaseAPITestCase, make_user

API = "/api/builder/blueprints"


def number_form(**extra) -> dict:
    data = {
        "roles": [],
        "fields": [
            {"key": "area_m2", "label": "Maydon", "type": "number", "required": True,
             "validation": {"min": 0, "max": 10000}},
            {"key": "birds", "label": "Parranda", "type": "number", "required": True},
            {"key": "density", "label": "Zichlik", "type": "computed", "formula": "area_m2 * 10 / birds"},
            {"key": "note", "label": "Izoh", "type": "text"},
        ],
    }
    data.update(extra)
    return data


class BuilderTestCase(BaseAPITestCase):
    """Uploads go to a temporary MEDIA_ROOT; ``notify_on_commit`` callbacks run immediately."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="nnp-builder-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media, FILE_STORAGE="local")
        cls._override.enable()
        reset_storage_cache()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._override.disable()
        reset_storage_cache()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(
            "notifications.services.transaction.on_commit",
            side_effect=lambda func, using=None, robust=False: func(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.superuser = make_user("super1", Role.ADMIN, is_superuser=True, is_staff=True)
        cls.vet = make_user("vet1", Role.VET)
        cls.vet2 = make_user("vet2", Role.VET)
        cls.logist = make_user("logist1", Role.LOGISTICS)

    # -- blueprint helpers (services directly; the API is tested separately) --

    def blueprint(self, kind: str, data: dict, *, name: str = "", activate: bool = True) -> Blueprint:
        bp = services.create_blueprint(actor=self.superuser, kind=kind, name=name or f"{kind} namuna", data=data)
        if activate:
            services.activate_blueprint(actor=self.superuser, pk=bp.pk)
            bp.refresh_from_db()
        return bp

    def form(self, **kwargs) -> Blueprint:
        return self.blueprint(BlueprintKind.FORM, number_form(), **kwargs)

    def task_template(self, *, executor: str = Role.VET, approver: str = Role.ADMIN, form: Blueprint | None = None,
                      activate: bool = True, **extra) -> Blueprint:
        name = extra.pop("name", "Vazifa")
        data = {"executor": executor, "approver": approver, "form_id": form.pk if form else None,
                "duration_days": 3, "requires_household": False, **extra}
        return self.blueprint(BlueprintKind.TASK, data, name=name, activate=activate)

    def role_profile(self, permissions: dict, base_role: str = Role.WAREHOUSE, name: str = "Katta omborchi"):
        return self.blueprint(BlueprintKind.ROLE, {"base_role": base_role, "permissions": permissions}, name=name)

    def as_user(self, user: User | None) -> None:
        self.client.force_authenticate(user=user)
