"""Example Konstruktor configuration (API_CONTRACT_V4 §1.2). Idempotent; everything is created as DRAFT.

    python manage.py seed_builder_examples

Objects are matched by ``(kind, name)``: an existing one (any status) is reused and never changed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User
from builder import services
from builder.models import Blueprint, BlueprintKind

ROLES = {
    "Buxgalter": {
        "base_role": "ADMIN",
        "permissions": {
            "orders": ["view", "download"],
            "reports": ["view", "download"],
            "stats": ["view", "download"],
            "acts": ["view", "create", "download"],
            # Buxgalteriya bosqichining bajaruvchisi: o‘z vazifasini ko‘rishi va topshirishi kerak.
            "tasks": ["view", "create"],
            "processes": ["view"],
        },
    },
    "Qushxona xodimi": {
        "base_role": "WAREHOUSE",
        "permissions": {
            "households": ["view"],
            "warehouse": ["view", "create", "edit"],
            "tasks": ["view", "create"],
            "forms": ["view", "create"],
            "acts": ["view", "download"],
        },
    },
}


def _field(key: str, label: str, ftype: str, **extra) -> dict:
    return {"key": key, "label": label, "type": ftype, **extra}


CONSTRUCTION_FORM = {
    "roles": [],
    "fields": [
        _field("checked_on", "Ko‘rik sanasi", "date", required=True),
        _field("area_m2", "Maydon (m²)", "number", required=True, validation={"min": 1, "max": 10000}),
        _field("birds", "Parranda soni (rejada)", "number", required=True, validation={"min": 1, "max": 100000}),
        _field("area_per_10", "10 ta parrandaga maydon (m²)", "computed", formula="area_m2 * 10 / birds"),
        _field("roof_type", "Tom turi", "select", options=["Shifer", "Tunuka", "Polikarbonat", "Boshqa"]),
        _field("ventilation", "Shamollatish bor", "bool"),
        _field("issues", "Kamchiliklar", "multiselect",
               options=["Isitish yo‘q", "Yorug‘lik yetarli emas", "Suv yo‘q", "Pol nam"]),
        _field("contact_phone", "Aloqa telefoni", "phone"),
        _field("location", "Joylashuv", "geo"),
        _field("photo", "Bino rasmi", "photo", required=True),
        _field("note", "Izoh", "text", validation={"max_length": 1000}),
    ],
}

DOCUMENTS = [
    ("Vazifa dalolatnomasi", {
        "scope": "task", "title": "Bajarilgan ishlar dalolatnomasi",
        "body": "Biz, quyida imzo chekuvchilar, {{date}} kuni {{household.code}} ({{household.name}}) xonadonida "
                "“{{task.title}}” ({{task.code}}) vazifasi bajarilganini tasdiqlaymiz.",
        "signatures": ["Bajaruvchi", "Xonadon egasi"],
    }),
    ("Umumiy dalolatnoma", {
        "scope": "general", "title": "Umumiy dalolatnoma",
        "body": "{{company}} tomonidan {{period}} davrida {{household.code}} xonadoni bo‘yicha bajarilgan barcha "
                "ishlar quyida keltirilgan.",
        "signatures": ["Administrator", "Omborchi", "Logistika xodimi", "Veterinar", "Xonadon egasi", "Direktor"],
    }),
    ("Administrator dalolatnomasi", {"scope": "role", "role": "ADMIN", "title": "Administrator bajargan ishlar",
                                     "body": "{{period}} davrida {{responsible}} bajargan ishlar.",
                                     "signatures": ["Administrator"]}),
    ("Omborchi dalolatnomasi", {"scope": "role", "role": "WAREHOUSE", "title": "Ombor bajargan ishlar",
                                "body": "{{period}} davrida ombor tomonidan bajarilgan kirim va chiqimlar.",
                                "signatures": ["Omborchi"]}),
    ("Logistika dalolatnomasi", {"scope": "role", "role": "LOGISTICS", "title": "Logistika bajargan ishlar",
                                 "body": "{{period}} davrida yetkazib berilgan mahsulotlar.",
                                 "signatures": ["Logistika xodimi", "Xonadon egasi"]}),
    ("Veterinar dalolatnomasi", {"scope": "role", "role": "VET", "title": "Veterinar bajargan ishlar",
                                 "body": "{{period}} davrida o‘tkazilgan tekshiruv va emlashlar.",
                                 "signatures": ["Veterinar", "Xonadon egasi"]}),
    ("Xonadon egasi dalolatnomasi", {"scope": "role", "role": "OWNER", "title": "Xonadon hisoboti",
                                     "body": "{{household.name}} xonadonining {{period}} davridagi natijalari.",
                                     "signatures": ["Xonadon egasi"]}),
]

#: (stage title, executor, approver, uses form, duration days)
STAGES = [
    ("Xonadon tasdiqlandi", "ADMIN", "", False, 1),
    ("Qurilish", "ADMIN", "ADMIN", True, 14),
    ("Ombor", "WAREHOUSE", "ADMIN", False, 3),
    ("Inkubatsiya", "WAREHOUSE", "ADMIN", False, 21),
    ("Logistika", "LOGISTICS", "ADMIN", False, 2),
    ("Veterinar", "VET", "ADMIN", False, 3),
    ("Qushxona", "profile:Qushxona xodimi", "ADMIN", False, 2),
    ("Buxgalteriya", "profile:Buxgalter", "", False, 3),
]

PROCESS_NAME = "Xonadon to‘liq sikli"


class Command(BaseCommand):
    help = "Konstruktor uchun namunaviy qoralamalar (rollar, forma, vazifalar, jarayon, hujjatlar)."

    def handle(self, *args, **options) -> None:
        actor = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
        self.created = 0
        with transaction.atomic():
            roles = {name: self._get(BlueprintKind.ROLE, name, data, actor) for name, data in ROLES.items()}
            form = self._get(BlueprintKind.FORM, "Qurilish ko‘rigi", CONSTRUCTION_FORM, actor,
                             "Qurilish holatini ko‘zdan kechirish formasi")
            documents = {name: self._get(BlueprintKind.DOCUMENT, name, data, actor) for name, data in DOCUMENTS}
            task_document = documents["Vazifa dalolatnomasi"]
            stages = []
            for title, executor, approver, uses_form, days in STAGES:
                if executor.startswith("profile:"):
                    executor = f"profile:{roles[executor.split(':', 1)[1]].pk}"
                task = self._get(BlueprintKind.TASK, f"{title} bosqichi", {
                    "executor": executor,
                    "approver": approver,
                    "form_id": form.pk if uses_form else None,
                    "document_id": task_document.pk,
                    "duration_days": days,
                    "requires_household": True,
                    "next_condition": "approved",
                }, actor)
                stages.append({"title": title, "task_id": task.pk, "notify_roles": ["ADMIN"], "condition": ""})
            self._get(BlueprintKind.PROCESS, PROCESS_NAME, {"stages": stages}, actor,
                      "Xonadon tasdiqlanganidan buxgalteriyagacha bo‘lgan to‘liq sikl")
        self.stdout.write(self.style.SUCCESS(f"Tayyor: {self.created} ta yangi qoralama yaratildi."))

    def _get(self, kind: str, name: str, data: dict, actor, description: str = "") -> Blueprint:
        existing = Blueprint.objects.filter(kind=kind, name=name).order_by("id").first()
        if existing is not None:
            return existing
        self.created += 1
        return services.create_blueprint(actor=actor, kind=kind, name=name, description=description, data=data)
