"""Event registry (API_CONTRACT_V3 §9.2, §9.4): every notification event the system can send.

A module never hard-codes recipients: it calls :func:`notifications.services.notify_on_commit` with the event
key, the composed text and the contextual users it knows (``household_owner``, ``assigned_vet`` ...). Role
recipients come from ``default_roles`` here, overridden per event by ``NotificationSetting`` (Super Admin).
"""

from __future__ import annotations

from dataclasses import dataclass

from common.roles import Role

# Contextual recipient names (§9.2). A module passes ``contextual={HOUSEHOLD_OWNER: owner, ...}``.
HOUSEHOLD_OWNER = "household_owner"
ASSIGNED_VET = "assigned_vet"  # the caller passes all active VETs when the object has no vet
DRIVER_USER = "driver_user"
RESPONSIBLE = "responsible"
SUBJECT_USER = "subject_user"
OTHER_PARTY = "other_party"

CONTEXTUAL_NAMES = frozenset({HOUSEHOLD_OWNER, ASSIGNED_VET, DRIVER_USER, RESPONSIBLE, SUBJECT_USER, OTHER_PARTY})

A, W, L, V = Role.ADMIN.value, Role.WAREHOUSE.value, Role.LOGISTICS.value, Role.VET.value


@dataclass(frozen=True)
class EventDef:
    key: str
    emoji: str
    #: Plain title without emoji/HTML (``Notification.title``), e.g. ``NNP — YANGI XONADON``.
    title: str
    display: str
    description: str
    default_roles: tuple[str, ...] = ()
    contextual: tuple[str, ...] = ()
    #: Roles used when no contextual user exists (``driver_assigned`` → LOGISTICS).
    fallback_roles: tuple[str, ...] = ()
    #: Additionally switched off by ``SystemSettings.low_stock_alert_enabled``.
    low_stock_gate: bool = False

    @property
    def title_html(self) -> str:
        return f"{self.emoji} <b>{self.title}</b>"


_DEFS: tuple[EventDef, ...] = (
    # --- Phase 1 / 2 ---------------------------------------------------------------------
    EventDef("household_created", "🏠", "NNP — YANGI XONADON", "Yangi xonadon ro‘yxatga olindi",
             "Administrator yangi xonadon yaratganda", (A,)),
    EventDef("user_created", "👤", "NNP — YANGI FOYDALANUVCHI", "Yangi foydalanuvchi yaratildi",
             "Yangi xodim yoki xonadon egasi hisobi yaratilganda", (A,), (SUBJECT_USER,)),
    EventDef("batch_created", "🐣", "NNP — YANGI PARRANDA PARTIYASI", "Yangi parranda partiyasi",
             "Markazga yangi partiya qabul qilinganda", (A, W)),
    EventDef("poultry_distributed", "🐔", "NNP — PARRANDA TARQATILDI", "Parranda tarqatildi",
             "Parranda xonadonga berilganda", (W, A), (HOUSEHOLD_OWNER,)),
    EventDef("poultry_returned", "↩️", "NNP — PARRANDA QAYTARILDI", "Parranda qaytarildi",
             "Parranda xonadondan markazga qaytarilganda", (W,), (HOUSEHOLD_OWNER,)),
    EventDef("poultry_death", "⚠️", "NNP — NOBUD BO‘LISH QAYD ETILDI", "Nobud bo‘lish qayd etildi",
             "Parranda nobud bo‘lgani kiritilganda", (A, V), (HOUSEHOLD_OWNER,)),
    EventDef("poultry_out", "📤", "NNP — PARRANDA CHIQIM QILINDI", "Parranda chiqim qilindi",
             "Markazdan parranda chiqim qilinganda", (A,)),
    EventDef("stock_in", "📦", "NNP — OMBORGA KIRIM", "Omborga kirim", "Omborga mahsulot kirim qilinganda", (W,)),
    EventDef("stock_out", "🚚", "NNP — OMBORDAN CHIQIM", "Ombordan chiqim",
             "Ombordan mahsulot chiqim qilinganda (qo‘lda chiqimda xonadon egasiga ham)", (W,), (HOUSEHOLD_OWNER,)),
    EventDef("stock_running_low", "📉", "NNP — OMBOR QOLDIG‘I KAMAYMOQDA", "Ombor qoldig‘i kamaymoqda",
             "Qoldiq minimal zaxiraning 1,5 baravaridan pastga tushganda", (W, A), low_stock_gate=True),
    EventDef("low_stock", "🔻", "NNP — MINIMAL ZAXIRAGA YETILDI", "Minimal zaxiraga yetildi",
             "Qoldiq minimal zaxiraga yetganda", (W, A), low_stock_gate=True),
    EventDef("daily_report", "📝", "NNP — KUNLIK HISOBOT", "Kunlik hisobot kiritildi",
             "Xonadon egasi kunlik hisobot yuborganda", (A,)),
    # --- Orders ----------------------------------------------------------------------------
    EventDef("order_created", "🛒", "NNP — YANGI BUYURTMA", "Yangi buyurtma berildi",
             "Xonadon egasi buyurtma yoki talab yuborganda", (A,)),
    EventDef("order_approved", "✅", "NNP — BUYURTMA TASDIQLANDI", "Buyurtma tasdiqlandi",
             "Administrator buyurtmani tasdiqlaganda", (), (HOUSEHOLD_OWNER,)),
    EventDef("order_rejected", "⛔", "NNP — BUYURTMA RAD ETILDI", "Buyurtma rad etildi",
             "Administrator buyurtmani rad etganda", (), (HOUSEHOLD_OWNER,)),
    EventDef("order_cancelled", "❌", "NNP — BUYURTMA BEKOR QILINDI", "Buyurtma bekor qilindi",
             "Buyurtma bekor qilinganda (ikkinchi tomonga)", (), (OTHER_PARTY,)),
    # --- Logistics -------------------------------------------------------------------------
    EventDef("task_created", "🚚", "NNP — LOGISTIKA TOPSHIRIG‘I YARATILDI", "Logistika topshirig‘i yaratildi",
             "Yangi yetkazib berish topshirig‘i yaratilganda", (L,)),
    EventDef("driver_assigned", "👷", "NNP — HAYDOVCHI BIRIKTIRILDI", "Haydovchi biriktirildi",
             "Topshiriqqa transport va haydovchi biriktirilganda", (), (DRIVER_USER, RESPONSIBLE), (L,)),
    EventDef("delivery_started", "🛻", "NNP — YETKAZIB BERISH BOSHLANDI", "Yetkazib berish boshlandi",
             "Haydovchi yo‘lga chiqqanda", (), (HOUSEHOLD_OWNER,)),
    EventDef("delivery_delivered", "📦", "NNP — MAHSULOT YETKAZILDI", "Mahsulot yetkazildi",
             "Mahsulot xonadonga yetkazilganda", (A,), (HOUSEHOLD_OWNER,)),
    EventDef("delivery_confirmed", "🤝", "NNP — YETKAZIB BERISH TASDIQLANDI", "Yetkazib berish tasdiqlandi",
             "Xonadon egasi qabul qilganini tasdiqlaganda", (A,), (RESPONSIBLE,)),
    EventDef("task_cancelled", "❌", "NNP — TOPSHIRIQ BEKOR QILINDI", "Topshiriq bekor qilindi",
             "Logistika topshirig‘i bekor qilinganda", (), (RESPONSIBLE, DRIVER_USER, HOUSEHOLD_OWNER)),
    # --- Veterinary ------------------------------------------------------------------------
    EventDef("vet_visit_planned", "🩺", "NNP — VETERINARIYA TEKSHIRUVI BELGILANDI", "Tekshiruv belgilandi",
             "Veterinariya tekshiruvi rejalashtirilganda yoki sanasi o‘zgarganda", (), (ASSIGNED_VET, HOUSEHOLD_OWNER)),
    EventDef("vet_visit_cancelled", "❌", "NNP — TEKSHIRUV BEKOR QILINDI", "Tekshiruv bekor qilindi",
             "Rejalashtirilgan tekshiruv bekor qilinganda", (), (ASSIGNED_VET, HOUSEHOLD_OWNER)),
    EventDef("vet_conclusion", "📋", "NNP — VETERINARIYA XULOSASI", "Veterinariya xulosasi kiritildi",
             "Veterinar tekshiruv xulosasini kiritganda", (A,), (HOUSEHOLD_OWNER,)),
    EventDef("vaccination_planned", "💉", "NNP — EMLASH REJALASHTIRILDI", "Emlash rejalashtirildi",
             "Emlash rejalashtirilganda", (), (ASSIGNED_VET, HOUSEHOLD_OWNER)),
    EventDef("vaccination_done", "💉", "NNP — EMLASH O‘TKAZILDI", "Emlash o‘tkazildi",
             "Emlash natijasi kiritilganda", (A,), (HOUSEHOLD_OWNER,)),
    EventDef("vaccination_cancelled", "❌", "NNP — EMLASH BEKOR QILINDI", "Emlash bekor qilindi",
             "Rejalashtirilgan emlash bekor qilinganda", (), (ASSIGNED_VET, HOUSEHOLD_OWNER)),
    EventDef("vaccination_due", "⏰", "NNP — EMLASH MUDDATI YAQINLASHMOQDA", "Emlash muddati yaqinlashmoqda",
             "Eslatma: emlash sanasi yaqin", (A,), (ASSIGNED_VET, HOUSEHOLD_OWNER)),
    EventDef("vaccination_overdue", "⚠️", "NNP — EMLASH MUDDATI O‘TDI", "Emlash muddati o‘tdi",
             "Eslatma: emlash o‘z vaqtida o‘tkazilmagan (1, 7, 14, 30-kun)", (A,), (ASSIGNED_VET,)),
    EventDef("vet_visit_today", "⏰", "NNP — BUGUNGI TEKSHIRUV", "Bugungi tekshiruv",
             "Eslatma: bugun rejalashtirilgan tekshiruv", (), (ASSIGNED_VET,)),
    EventDef("disease_registered", "🦠", "NNP — KASALLIK QAYD ETILDI", "Kasallik qayd etildi",
             "Veterinar kasallik holatini kiritganda", (A,), (HOUSEHOLD_OWNER,)),
    EventDef("treatment_prescribed", "💊", "NNP — DAVOLASH TAVSIYASI", "Davolash tavsiyasi",
             "Veterinar davolash yoki tavsiya kiritganda", (), (HOUSEHOLD_OWNER,)),
    EventDef("vet_request_created", "📨", "NNP — VETERINARGA MUROJAAT", "Veterinarga murojaat",
             "Xonadon egasi veterinarga murojaat yuborganda", (V,)),
    EventDef("vet_request_updated", "📨", "NNP — MUROJAAT HOLATI O‘ZGARDI", "Murojaat holati o‘zgardi",
             "Murojaat ko‘rib chiqila boshlanganda yoki yopilganda", (), (HOUSEHOLD_OWNER,)),
    # --- Warehouse V3 ----------------------------------------------------------------------
    EventDef("inventory_approved", "🧮", "NNP — INVENTARIZATSIYA YAKUNLANDI", "Inventarizatsiya yakunlandi",
             "Inventarizatsiya tasdiqlanganda", (A, W)),
    EventDef("lot_expiring", "⏳", "NNP — YAROQLILIK MUDDATI YAQINLASHMOQDA", "Yaroqlilik muddati yaqinlashmoqda",
             "Eslatma: partiyaning yaroqlilik muddati yaqin", (W, A)),
    EventDef("lot_expired", "⛔", "NNP — YAROQLILIK MUDDATI O‘TDI", "Yaroqlilik muddati o‘tdi",
             "Eslatma: partiyaning yaroqlilik muddati o‘tgan, qoldiq bor", (W, A)),
)

#: Registry order = order of the settings table in the Web App.
EVENTS: dict[str, EventDef] = {definition.key: definition for definition in _DEFS}

for _definition in _DEFS:  # import-time sanity check
    assert set(_definition.contextual) <= CONTEXTUAL_NAMES, _definition.key
    assert len(_definition.key) <= 50, _definition.key


def get(event: str) -> EventDef:
    """Definition of ``event``; ``KeyError`` for an unknown key (a programming error)."""
    return EVENTS[event]


def all_events() -> tuple[EventDef, ...]:
    """Every event in registry order."""
    return _DEFS


def display(event: str) -> str:
    definition = EVENTS.get(event)
    return definition.display if definition is not None else event
