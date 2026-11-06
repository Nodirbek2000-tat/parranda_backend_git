"""The chain of a household case. Edit this one constant to change titles, roles, deadlines or fields.

Field kinds: "text", "int", "decimal" (2 dp), "choice" (with `choices`). `photos` = minimum number of photos the
stage needs. Stage 1 fields are the case creation fields (household data lives on the Case itself).
Stage 10 (growing) lasts `settings.GROWING_DAYS` days: use `days_of()` instead of `Stage.days`.
"""

from dataclasses import dataclass, field

from django.conf import settings

from accounts.models import Role


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"
    required: bool = True
    choices: tuple = ()
    min_value: int | None = None
    #: decimal fields: the value must be strictly greater than `min_value`
    exclusive_min: bool = False
    help: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.kind,
            "required": self.required,
            "choices": [{"value": value, "label": label} for value, label in self.choices],
            "min": self.min_value,
            "decimal_places": 2 if self.kind == "decimal" else None,
            "help": self.help,
        }


@dataclass(frozen=True)
class Stage:
    number: int
    key: str
    title: str
    executor: str
    approver: str | None
    days: int
    photos: int = 0
    fields: tuple = field(default_factory=tuple)
    #: name of a setting that overrides `days` (read at call time, see `days_of`)
    days_setting: str = ""


HEALTH_CHOICES = (("good", "Yaxshi"), ("normal", "O‘rtacha"), ("bad", "Yomon"))
FINANCING_CHOICES = (("grant", "Grant"), ("credit", "Kredit"), ("installment", "Muddatli to‘lov"),
                     ("own", "O‘z mablag‘i"))

STAGES: tuple[Stage, ...] = (
    Stage(1, "inspection", "Inspeksiya", Role.INSPECTOR, None, 2, photos=1,
          fields=(Field("conclusion", "Xulosa"),)),
    Stage(2, "hr", "HR tekshiruvi", Role.HR, None, 2),
    Stage(3, "director", "Direktor qarori", Role.DIRECTOR, None, 2),
    Stage(4, "construction", "Qurilish", Role.BUILDER, Role.ADMIN, 14, photos=1),
    Stage(5, "warehouse", "Ombordan berish", Role.WAREHOUSE, Role.ADMIN, 2),
    Stage(6, "incubation", "Inkubatsiya", Role.INCUBATOR, Role.ADMIN, 21, photos=1,
          fields=(Field("chicks_count", "Berilgan jo‘jalar soni", "int", min_value=1),)),
    Stage(7, "logistics", "Yetkazib berish", Role.LOGISTICS, Role.ADMIN, 2, photos=1,
          fields=(
              Field("delivered_healthy", "Sog‘lom yetkazilgan jo‘jalar", "int", min_value=0),
              Field("died_in_transit", "Yo‘lda nobud bo‘lgan", "int", min_value=0),
          )),
    Stage(8, "vet", "Veterinar ko‘rigi", Role.VET, Role.ADMIN, 2, photos=1,
          fields=(Field("health", "Sog‘lig‘i", "choice", choices=HEALTH_CHOICES),)),
    Stage(9, "acceptance", "Parrandani qabul qilish", Role.OWNER, Role.ADMIN, 2, photos=1,
          fields=(Field("received_count", "Qabul qilingan jo‘jalar soni", "int", min_value=0),)),
    Stage(10, "growing", "Parvarish", Role.ADMIN, None, 45, days_setting="GROWING_DAYS"),
    Stage(11, "pickup", "Qushxonaga olib borish", Role.LOGISTICS, Role.ADMIN, 2, photos=1,
          fields=(Field("picked_up_count", "Olib ketilgan parrandalar soni", "int", min_value=0),)),
    Stage(12, "slaughter", "Qushxona qabuli", Role.SLAUGHTERHOUSE, Role.ADMIN, 2,
          fields=(
              Field("received_count", "Qabul qilingan parrandalar soni", "int", min_value=0),
              Field("total_kg", "Umumiy vazn (kg)", "decimal", min_value=0, exclusive_min=True),
          )),
    Stage(13, "accounting", "Buxgalteriya (yakuniy hisob)", Role.ACCOUNTANT, Role.SUPERADMIN, 3,
          fields=(
              Field("financing_type", "Moliyalashtirish turi", "choice", choices=FINANCING_CHOICES),
              Field("contract_no", "Shartnoma raqami"),
              Field("revenue", "Sotuv summasi (so‘m)", "int", required=False, min_value=0,
                    help="Sotuv summasi yoki 1 kg narxidan birini kiriting."),
              Field("price_per_kg", "1 kg narxi (so‘m)", "int", required=False, min_value=0,
                    help="Faqat narx kiritilsa: summa = narx × 12-bosqichdagi umumiy vazn."),
          )),
)

BY_NUMBER: dict[int, Stage] = {stage.number: stage for stage in STAGES}
LAST = STAGES[-1].number
#: Stage whose submit issues the package items from the warehouse (stock OUT).
WAREHOUSE_STAGE = 5
INCUBATION_STAGE = 6
DELIVERY_STAGE = 7
#: Stage done by the household owner.
OWNER_STAGE = 9
#: Growing period: requests and weekly vet visits happen while this stage is ACTIVE.
GROWING_STAGE = 10
SLAUGHTER_STAGE = 12
ACCOUNTING_STAGE = 13


def get(number: int) -> Stage:
    return BY_NUMBER[number]


def days_of(number: int) -> int:
    stage = get(number)
    if stage.days_setting:
        return int(getattr(settings, stage.days_setting, stage.days))
    return stage.days


def growing_days() -> int:
    return days_of(GROWING_STAGE)


def has_act(number: int) -> bool:
    """Every finished stage gets its own act (dalolatnoma), except the owner's stage."""
    return number in BY_NUMBER and number != OWNER_STAGE
