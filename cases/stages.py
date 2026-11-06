"""The chain of a household case. Edit this one constant to change titles, roles, deadlines or fields.

Field kinds: "text", "int", "choice" (with `choices`). `photos` = minimum number of photos the stage needs.
Stage 1 fields are the case creation fields (household data lives on the Case itself).
"""

from dataclasses import dataclass, field

from accounts.models import Role


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = "text"
    required: bool = True
    choices: tuple = ()
    min_value: int | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.kind,
            "required": self.required,
            "choices": [{"value": value, "label": label} for value, label in self.choices],
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


HEALTH_CHOICES = (("good", "Yaxshi"), ("normal", "O‘rtacha"), ("bad", "Yomon"))

STAGES: tuple[Stage, ...] = (
    Stage(1, "inspection", "Inspeksiya", Role.INSPECTOR, None, 2, photos=1,
          fields=(Field("conclusion", "Xulosa"),)),
    Stage(2, "hr", "HR tekshiruvi", Role.HR, None, 2),
    Stage(3, "director", "Direktor qarori", Role.DIRECTOR, None, 2),
    Stage(4, "construction", "Qurilish", Role.BUILDER, Role.ADMIN, 14, photos=1),
    Stage(5, "warehouse", "Ombordan berish", Role.WAREHOUSE, Role.ADMIN, 2),
    Stage(6, "incubation", "Inkubatsiya", Role.INCUBATOR, Role.ADMIN, 21, photos=1,
          fields=(Field("chicks_count", "Jo‘jalar soni", "int", min_value=1),)),
    Stage(7, "logistics", "Yetkazib berish", Role.LOGISTICS, Role.ADMIN, 2, photos=1),
    Stage(8, "vet", "Veterinar ko‘rigi", Role.VET, Role.ADMIN, 2, photos=1,
          fields=(Field("health", "Sog‘lig‘i", "choice", choices=HEALTH_CHOICES),)),
    Stage(9, "acceptance", "Parrandani qabul qilish", Role.OWNER, Role.ADMIN, 2, photos=1,
          fields=(Field("received_count", "Qabul qilingan jo‘jalar soni", "int", min_value=0),)),
    Stage(10, "accounting", "Buxgalteriya", Role.ACCOUNTANT, Role.SUPERADMIN, 3,
          fields=(
              Field("financing_type", "Moliyalashtirish turi"),
              Field("amount", "Summa (so‘m)", "int", min_value=0),
              Field("contract_no", "Shartnoma raqami"),
          )),
)

BY_NUMBER: dict[int, Stage] = {stage.number: stage for stage in STAGES}
LAST = STAGES[-1].number
#: Stage whose submit issues the package items from the warehouse (stock OUT).
WAREHOUSE_STAGE = 5
#: Stage done by the household owner (ADMIN may act for the owner).
OWNER_STAGE = 9


def get(number: int) -> Stage:
    return BY_NUMBER[number]
