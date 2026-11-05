"""Validation of blueprint ``data`` per kind (API_CONTRACT_V4 §1.2), the safe formula engine of computed
fields and validation / normalisation of form values.

Every schema error is a readable Uzbek sentence raised as ``ValidationError({"data": ["…"]})``; form value
errors are ``ValidationError({"data": {"<key>": ["…"]}})``. Formulas are parsed by a small tokenizer +
recursive descent parser and evaluated on an AST: Python ``eval`` is never used.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from common.roles import ALL_ROLES, Role, role_of

from .access import PROFILE_PREFIX, matches_any, profile_id_of
from .models import BlueprintKind, BlueprintStatus

# --- Reference lists (meta, §1.3) ----------------------------------------------------------------

MODULES: tuple[tuple[str, str], ...] = (
    ("households", "Xonadonlar"),
    ("batches", "Partiyalar"),
    ("warehouse", "Ombor"),
    ("orders", "Buyurtmalar"),
    ("logistics", "Logistika"),
    ("vet", "Veterinariya"),
    ("reports", "Hisobotlar"),
    ("stats", "Statistika"),
    ("map", "Xarita"),
    ("users", "Foydalanuvchilar"),
    ("notifications", "Bildirishnomalar"),
    ("audit", "Audit jurnali"),
    ("tasks", "Vazifalar"),
    ("processes", "Jarayonlar"),
    ("forms", "Formalar"),
    ("acts", "Dalolatnomalar"),
)
MODULE_KEYS = tuple(key for key, _ in MODULES)

ACTIONS: tuple[tuple[str, str], ...] = (
    ("view", "Ko‘rish"),
    ("create", "Kiritish"),
    ("edit", "Tahrirlash"),
    ("approve", "Tasdiqlash"),
    ("download", "Yuklab olish"),
)
ACTION_KEYS = tuple(key for key, _ in ACTIONS)

FIELD_TYPES: tuple[tuple[str, str], ...] = (
    ("text", "Matn"),
    ("number", "Raqam"),
    ("date", "Sana"),
    ("time", "Vaqt"),
    ("phone", "Telefon"),
    ("select", "Ro‘yxatdan tanlash"),
    ("multiselect", "Bir nechtasini tanlash"),
    ("file", "Fayl"),
    ("photo", "Rasm"),
    ("geo", "Geolokatsiya"),
    ("bool", "Ha / Yo‘q"),
    ("computed", "Hisoblanadigan"),
)
FIELD_TYPE_KEYS = tuple(key for key, _ in FIELD_TYPES)
CHOICE_TYPES = frozenset({"select", "multiselect"})
FILE_TYPES = frozenset({"file", "photo"})
NUMERIC_TYPES = frozenset({"number", "computed"})

PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("{{company}}", "Kompaniya nomi"),
    ("{{date}}", "Sana"),
    ("{{period}}", "Davr"),
    ("{{act.code}}", "Dalolatnoma raqami"),
    ("{{role}}", "Rol"),
    ("{{responsible}}", "Mas’ul shaxs"),
    ("{{household.code}}", "Xonadon ID"),
    ("{{household.name}}", "Xonadon nomi"),
    ("{{household.owner}}", "Xonadon egasi"),
    ("{{household.district}}", "Tuman"),
    ("{{household.address}}", "Manzil"),
    ("{{task.code}}", "Vazifa raqami"),
    ("{{task.title}}", "Vazifa nomi"),
    ("{{field.<key>}}", "Forma maydoni qiymati"),
    ("{{quantities}}", "Miqdorlar jadvali"),
    ("{{photos}}", "Rasmlar"),
    ("{{approvals}}", "Tasdiqlar"),
)

TASK_CONDITIONS = ("approved", "submitted")
DOCUMENT_SCOPES = ("task", "role", "general")

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PHONE_RE = re.compile(r"^\+998\d{9}$")

MAX_FIELDS = 60
MAX_OPTIONS = 50
MAX_STAGES = 30
MAX_REFS = 30
MAX_TEXT = 2000
MAX_FORMULA = 500
MAX_BODY = 20000

MSG_REQUIRED = "Bu maydon to‘ldirilishi shart."


def _fail(message: str) -> ValidationError:
    return ValidationError({"data": [message]})


def _field_label(index: int, field: Mapping[str, Any] | None = None) -> str:
    key = field.get("key") if isinstance(field, Mapping) else None
    return f"{index + 1}-maydon" + (f" ({key})" if isinstance(key, str) and key else "")


# --- Small typed readers -------------------------------------------------------------------------


def _text(value: Any, where: str, *, required: bool = False, max_length: int = 200) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _fail(f"{where}: matn bo‘lishi kerak.")
    value = value.strip()
    if required and not value:
        raise _fail(f"{where}: to‘ldirilishi shart.")
    if len(value) > max_length:
        raise _fail(f"{where}: ko‘pi bilan {max_length} ta belgi.")
    return value


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _fail(f"{where}: true yoki false bo‘lishi kerak.")
    return value


def _number_or_none(value: Any, where: str) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{where}: son bo‘lishi kerak.")
    return value


def _int_or_none(value: Any, where: str, *, low: int = 0, high: int = 10**9) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{where}: butun son bo‘lishi kerak.")
    if not low <= value <= high:
        raise _fail(f"{where}: {low} dan {high} gacha bo‘lishi kerak.")
    return value


def _date_or_none(value: Any, where: str) -> str | None:
    if value in (None, ""):
        return None
    parsed = parse_date(value) if isinstance(value, str) else None
    if parsed is None:
        raise _fail(f"{where}: sana YYYY-MM-DD formatida bo‘lishi kerak.")
    return parsed.isoformat()


def _list(value: Any, where: str, *, max_length: int) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail(f"{where}: ro‘yxat bo‘lishi kerak.")
    if len(value) > max_length:
        raise _fail(f"{where}: ko‘pi bilan {max_length} ta element.")
    return value


def _dict(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail(f"{where}: obyekt bo‘lishi kerak.")
    return value


# --- Role references -----------------------------------------------------------------------------


def role_ref(value: Any, where: str, *, profiles: Callable[[set[int]], set[int]], allow_blank: bool = False) -> str:
    """``"VET"`` / ``"profile:7"`` (the profile must exist); ``""`` only with ``allow_blank``."""
    if value in (None, "") and allow_blank:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{where}: rol tanlanmagan.")
    value = value.strip()
    if value in ALL_ROLES:
        return value
    pid = profile_id_of(value)
    if pid is None:
        raise _fail(f"{where}: “{value}” noma’lum rol.")
    if pid not in profiles({pid}):
        raise _fail(f"{where}: {value} rol profili topilmadi.")
    return f"{PROFILE_PREFIX}{pid}"


def role_refs(value: Any, where: str, *, profiles: Callable[[set[int]], set[int]]) -> list[str]:
    items = _list(value, where, max_length=MAX_REFS)
    result: list[str] = []
    for item in items:
        ref = role_ref(item, where, profiles=profiles)
        if ref not in result:
            result.append(ref)
    return result


def refs_in(data: Mapping[str, Any], kind: str) -> dict[str, set[int]]:
    """Blueprint ids referenced by ``data``: ``{"form": {3}, "task": {5}, "document": {9}, "role": {7}}``."""
    found: dict[str, set[int]] = {"form": set(), "task": set(), "document": set(), "role": set()}

    def add_refs(values: Any) -> None:
        for ref in values or ():
            if isinstance(ref, str):
                pid = profile_id_of(ref)
                if pid is not None:
                    found["role"].add(pid)

    if not isinstance(data, Mapping):
        return found
    if kind == BlueprintKind.TASK:
        for name in ("form_id", "document_id"):
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                found[name[:-3]].add(value)
        add_refs([data.get("executor"), data.get("approver")])
    elif kind == BlueprintKind.PROCESS:
        for stage in data.get("stages") or ():
            if isinstance(stage, Mapping):
                if isinstance(stage.get("task_id"), int):
                    found["task"].add(stage["task_id"])
                add_refs(stage.get("notify_roles"))
    elif kind == BlueprintKind.FORM:
        add_refs(data.get("roles"))
        for field in data.get("fields") or ():
            if isinstance(field, Mapping):
                add_refs(field.get("visible_roles"))
                add_refs(field.get("editable_roles"))
    return found


# --- Formula engine ------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([a-z][a-z0-9_]*)|(.))")


class FormulaError(ValueError):
    pass


def tokenize(formula: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    text = formula.rstrip()
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:  # pragma: no cover - the pattern matches any character
            raise FormulaError("Formulada noto‘g‘ri belgi.")
        number, name, symbol = match.groups()
        position = match.end()
        if number is not None:
            tokens.append(("num", number))
        elif name is not None:
            tokens.append(("name", name))
        elif symbol in "+-*/()":
            tokens.append(("op", symbol))
        else:
            raise FormulaError(f"Formulada ruxsat etilmagan belgi: “{symbol}”.")
    return tokens


class _Parser:
    """``expr := term (('+'|'-') term)*; term := unary (('*'|'/') unary)*; unary := '-' unary | atom``."""

    MAX_DEPTH = 50

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.index = 0
        self.depth = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise FormulaError("Formula to‘liq emas.")
        self.index += 1
        return token

    def parse(self) -> tuple:
        if not self.tokens:
            raise FormulaError("Formula bo‘sh.")
        node = self.expr()
        if self.peek() is not None:
            raise FormulaError(f"Formulada ortiqcha belgi: “{self.peek()[1]}”.")
        return node

    def expr(self) -> tuple:
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            node = (op, node, self.term())
        return node

    def term(self) -> tuple:
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            node = (op, node, self.unary())
        return node

    def unary(self) -> tuple:
        if self.peek() == ("op", "-"):
            self.take()
            return ("neg", self.unary())
        if self.peek() == ("op", "+"):
            self.take()
            return self.unary()
        return self.atom()

    def atom(self) -> tuple:
        kind, value = self.take()
        if kind == "num":
            return ("num", Decimal(value))
        if kind == "name":
            return ("var", value)
        if value == "(":
            self.depth += 1
            if self.depth > self.MAX_DEPTH:
                raise FormulaError("Formula juda murakkab.")
            node = self.expr()
            if self.take() != ("op", ")"):
                raise FormulaError("Qavs yopilmagan.")
            self.depth -= 1
            return node
        raise FormulaError(f"Kutilmagan belgi: “{value}”.")


def parse_formula(formula: str) -> tuple:
    """AST of ``formula`` or :class:`FormulaError` with an Uzbek message."""
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("Formula bo‘sh.")
    if len(formula) > MAX_FORMULA:
        raise FormulaError(f"Formula ko‘pi bilan {MAX_FORMULA} ta belgi.")
    return _Parser(tokenize(formula)).parse()


def formula_names(node: tuple) -> set[str]:
    if node[0] == "var":
        return {node[1]}
    if node[0] == "num":
        return set()
    return set().union(*(formula_names(child) for child in node[1:]))


def _evaluate(node: tuple, values: Mapping[str, Any]) -> Decimal | None:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        value = values.get(node[1])
        if value is None or isinstance(value, bool):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if kind == "neg":
        inner = _evaluate(node[1], values)
        return None if inner is None else -inner
    left, right = _evaluate(node[1], values), _evaluate(node[2], values)
    if left is None or right is None:
        return None
    if kind == "+":
        return left + right
    if kind == "-":
        return left - right
    if kind == "*":
        return left * right
    if right == 0:
        return None
    return left / right


def evaluate_formula(formula: str | tuple, values: Mapping[str, Any]) -> float | int | None:
    """Result rounded to 2 decimals (``int`` when whole); ``None`` for a missing operand or ÷0."""
    try:
        node = parse_formula(formula) if isinstance(formula, str) else formula
        result = _evaluate(node, values)
    except (FormulaError, InvalidOperation, ArithmeticError):
        return None
    if result is None or not result.is_finite():
        return None
    rounded = result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(rounded) if rounded == rounded.to_integral_value() else float(rounded)


def computed_order(fields: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Computed fields in dependency order (assumes a validated, acyclic form)."""
    computed = {f["key"]: f for f in fields if f.get("type") == "computed"}
    ordered: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    def visit(key: str, stack: tuple[str, ...]) -> None:
        if key in seen:
            return
        if key in stack:
            raise FormulaError(f"Formulalar bir-biriga aylanib bog‘langan: {' → '.join((*stack, key))}.")
        for name in sorted(formula_names(parse_formula(computed[key].get("formula", "")))):
            if name in computed:
                visit(name, (*stack, key))
        seen.add(key)
        ordered.append(computed[key])

    for key in computed:
        visit(key, ())
    return ordered


# --- Schemas per kind ----------------------------------------------------------------------------


class Lookup:
    """Database lookups the validators need (injected by ``services`` so this module stays testable)."""

    def __init__(self) -> None:
        from .models import Blueprint

        self._model = Blueprint

    def profiles(self, ids: set[int]) -> set[int]:
        return set(self._model.objects.filter(pk__in=ids, kind=BlueprintKind.ROLE).values_list("pk", flat=True))

    def blueprint(self, pk: int, kind: str):
        return self._model.objects.filter(pk=pk, kind=kind).first()


def _validate_role(data: dict, lookup: Lookup) -> dict:
    base = data.get("base_role")
    if base not in ALL_ROLES or base == Role.OWNER:
        raise _fail("Asosiy rol: ADMIN, WAREHOUSE, LOGISTICS yoki VET bo‘lishi kerak.")
    raw = _dict(data.get("permissions"), "Vakolatlar")
    permissions: dict[str, list[str]] = {}
    for module, actions in raw.items():
        if module not in MODULE_KEYS:
            raise _fail(f"Vakolatlar: “{module}” noma’lum bo‘lim.")
        actions = _list(actions, f"Vakolatlar ({module})", max_length=len(ACTION_KEYS) * 2)
        unknown = [a for a in actions if a not in ACTION_KEYS]
        if unknown:
            raise _fail(f"Vakolatlar ({module}): “{unknown[0]}” noma’lum amal.")
        permissions[module] = [a for a in ACTION_KEYS if a in actions]
    ordered = {module: permissions[module] for module in MODULE_KEYS if module in permissions}
    return {"base_role": base, "permissions": ordered}


def _validate_field(index: int, field: Any, lookup: Lookup) -> dict:
    if not isinstance(field, dict):
        raise _fail(f"{index + 1}-maydon: obyekt bo‘lishi kerak.")
    where = _field_label(index, field)
    key = field.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise _fail(f"{where}: kalit lotin kichik harfi bilan boshlanib, faqat a-z, 0-9 va _ dan iborat bo‘lsin "
                    "(ko‘pi bilan 40 ta belgi).")
    label = _text(field.get("label"), f"{where}: nomi", required=True)
    ftype = field.get("type")
    if ftype not in FIELD_TYPE_KEYS:
        raise _fail(f"{where}: “{ftype}” noma’lum maydon turi.")
    options: list[str] = []
    if ftype in CHOICE_TYPES:
        raw = _list(field.get("options"), f"{where}: variantlar", max_length=MAX_OPTIONS)
        for option in raw:
            if not isinstance(option, str) or not option.strip():
                raise _fail(f"{where}: variantlar bo‘sh bo‘lmagan matn bo‘lishi kerak.")
            option = option.strip()[:200]
            if option in options:
                raise _fail(f"{where}: “{option}” varianti takrorlangan.")
            options.append(option)
        if not options:
            raise _fail(f"{where}: kamida bitta variant kiriting.")
    raw_rules = _dict(field.get("validation"), f"{where}: tekshiruv")
    rules = {
        "min": _number_or_none(raw_rules.get("min"), f"{where}: minimal qiymat"),
        "max": _number_or_none(raw_rules.get("max"), f"{where}: maksimal qiymat"),
        "min_length": _int_or_none(raw_rules.get("min_length"), f"{where}: minimal uzunlik", high=MAX_TEXT),
        "max_length": _int_or_none(raw_rules.get("max_length"), f"{where}: maksimal uzunlik", high=MAX_TEXT),
        "pattern": _text(raw_rules.get("pattern"), f"{where}: shablon", max_length=200),
    }
    if rules["min"] is not None and rules["max"] is not None and rules["min"] > rules["max"]:
        raise _fail(f"{where}: minimal qiymat maksimaldan katta.")
    if (
        rules["min_length"] is not None
        and rules["max_length"] is not None
        and rules["min_length"] > rules["max_length"]
    ):
        raise _fail(f"{where}: minimal uzunlik maksimaldan katta.")
    if rules["pattern"]:
        try:
            re.compile(rules["pattern"])
        except re.error:
            raise _fail(f"{where}: shablon (regex) noto‘g‘ri.") from None
    formula = _text(field.get("formula"), f"{where}: formula", max_length=MAX_FORMULA)
    if ftype == "computed":
        if not formula:
            raise _fail(f"{where}: hisoblanadigan maydon uchun formula kiriting.")
        try:
            parse_formula(formula)
        except FormulaError as exc:
            raise _fail(f"{where}: {exc}") from None
    else:
        formula = ""
    return {
        "key": key,
        "label": label,
        "type": ftype,
        "required": False if ftype == "computed" else _bool(field.get("required"), f"{where}: majburiy", False),
        "help": _text(field.get("help"), f"{where}: izoh", max_length=500),
        "options": options,
        "validation": rules,
        "formula": formula,
        "visible_roles": role_refs(field.get("visible_roles"), f"{where}: ko‘radigan rollar", profiles=lookup.profiles),
        "editable_roles": role_refs(
            field.get("editable_roles"), f"{where}: tahrirlaydigan rollar", profiles=lookup.profiles
        ),
    }


def _validate_form(data: dict, lookup: Lookup) -> dict:
    roles = role_refs(data.get("roles"), "Formani to‘ldiradigan rollar", profiles=lookup.profiles)
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise _fail("Formada kamida bitta maydon bo‘lishi kerak.")
    if len(raw_fields) > MAX_FIELDS:
        raise _fail(f"Formada ko‘pi bilan {MAX_FIELDS} ta maydon bo‘lishi mumkin.")
    fields = [_validate_field(index, field, lookup) for index, field in enumerate(raw_fields)]
    types: dict[str, str] = {}
    for index, field in enumerate(fields):
        if field["key"] in types:
            raise _fail(f"{_field_label(index, field)}: “{field['key']}” kaliti takrorlangan.")
        types[field["key"]] = field["type"]
    for index, field in enumerate(fields):
        if field["type"] != "computed":
            continue
        for name in sorted(formula_names(parse_formula(field["formula"]))):
            if name == field["key"]:
                raise _fail(f"{_field_label(index, field)}: formula o‘z-o‘ziga murojaat qilmasin.")
            if name not in types:
                raise _fail(f"{_field_label(index, field)}: formulada noma’lum maydon “{name}”.")
            if types[name] not in NUMERIC_TYPES:
                raise _fail(f"{_field_label(index, field)}: formulada faqat raqamli maydonlar ishlatiladi (“{name}”).")
    try:
        computed_order(fields)
    except FormulaError as exc:
        raise _fail(str(exc)) from None
    return {"roles": roles, "fields": fields}


def _validate_task(data: dict, lookup: Lookup) -> dict:
    executor = role_ref(data.get("executor"), "Bajaruvchi", profiles=lookup.profiles)
    approver = role_ref(data.get("approver"), "Tasdiqlovchi", profiles=lookup.profiles, allow_blank=True)
    form_id = _int_or_none(data.get("form_id"), "Forma", low=1, high=2**63 - 1)
    if form_id is not None:
        form = lookup.blueprint(form_id, BlueprintKind.FORM)
        if form is None or form.status == BlueprintStatus.ARCHIVED:
            raise _fail("Forma topilmadi yoki arxivlangan.")
    document_id = _int_or_none(data.get("document_id"), "Hujjat shabloni", low=1, high=2**63 - 1)
    if document_id is not None:
        document = lookup.blueprint(document_id, BlueprintKind.DOCUMENT)
        if document is None or document.status == BlueprintStatus.ARCHIVED:
            raise _fail("Hujjat shabloni topilmadi yoki arxivlangan.")
        if (document.data or {}).get("scope") != "task":
            raise _fail("Vazifaga faqat “vazifa” turidagi hujjat shabloni biriktiriladi.")
    start = _date_or_none(data.get("start_date"), "Boshlanish sanasi")
    end = _date_or_none(data.get("end_date"), "Tugash sanasi")
    if start and end and start > end:
        raise _fail("Tugash sanasi boshlanish sanasidan oldin bo‘lishi mumkin emas.")
    condition = data.get("next_condition") or "approved"
    if condition not in TASK_CONDITIONS:
        raise _fail("Keyingi bosqich sharti: “approved” yoki “submitted”.")
    return {
        "executor": executor,
        "approver": approver,
        "form_id": form_id,
        "document_id": document_id,
        "duration_days": _int_or_none(data.get("duration_days"), "Davomiylik (kun)", low=1, high=365),
        "start_date": start,
        "end_date": end,
        "requires_household": _bool(data.get("requires_household"), "Xonadon majburiy", False),
        "next_condition": condition,
    }


def _validate_process(data: dict, lookup: Lookup) -> dict:
    raw = data.get("stages")
    if not isinstance(raw, list) or not raw:
        raise _fail("Jarayonda kamida bitta bosqich bo‘lishi kerak.")
    if len(raw) > MAX_STAGES:
        raise _fail(f"Jarayonda ko‘pi bilan {MAX_STAGES} ta bosqich bo‘lishi mumkin.")
    stages = []
    for index, stage in enumerate(raw):
        where = f"{index + 1}-bosqich"
        if not isinstance(stage, dict):
            raise _fail(f"{where}: obyekt bo‘lishi kerak.")
        task_id = _int_or_none(stage.get("task_id"), f"{where}: vazifa", low=1, high=2**63 - 1)
        if task_id is None:
            raise _fail(f"{where}: vazifa shablonini tanlang.")
        task = lookup.blueprint(task_id, BlueprintKind.TASK)
        if task is None or task.status == BlueprintStatus.ARCHIVED:
            raise _fail(f"{where}: vazifa shabloni topilmadi yoki arxivlangan.")
        condition = stage.get("condition") or ""
        if condition and condition not in TASK_CONDITIONS:
            raise _fail(f"{where}: shart “approved” yoki “submitted” bo‘lishi kerak.")
        stages.append(
            {
                "title": _text(stage.get("title"), f"{where}: nomi") or task.name[:200],
                "task_id": task_id,
                "notify_roles": role_refs(stage.get("notify_roles"), f"{where}: xabar oladigan rollar",
                                          profiles=lookup.profiles),
                "condition": condition,
            }
        )
    return {"stages": stages}


def _validate_document(data: dict, lookup: Lookup) -> dict:
    scope = data.get("scope")
    if scope not in DOCUMENT_SCOPES:
        raise _fail("Hujjat turi: “task”, “role” yoki “general” bo‘lishi kerak.")
    role = ""
    if scope == "role":
        role = data.get("role")
        if role not in ALL_ROLES:
            raise _fail("Rol hujjati uchun tizim rolini tanlang.")
    signatures = []
    for item in _list(data.get("signatures"), "Imzolar", max_length=20):
        signatures.append(_text(item, "Imzolar", required=True))
    return {
        "scope": scope,
        "role": role,
        "title": _text(data.get("title"), "Sarlavha", required=True),
        "body": _text(data.get("body"), "Matn", max_length=MAX_BODY),
        "signatures": signatures,
        "include_photos": _bool(data.get("include_photos"), "Rasmlar", True),
        "include_approvals": _bool(data.get("include_approvals"), "Tasdiqlar", True),
        "include_quantities": _bool(data.get("include_quantities"), "Miqdorlar", True),
    }


_VALIDATORS = {
    BlueprintKind.ROLE: _validate_role,
    BlueprintKind.FORM: _validate_form,
    BlueprintKind.TASK: _validate_task,
    BlueprintKind.PROCESS: _validate_process,
    BlueprintKind.DOCUMENT: _validate_document,
}


def validate_data(kind: str, data: Any, lookup: Lookup | None = None) -> dict:
    """Normalised ``data`` of a blueprint ``kind`` (defaults filled, canonical order) or 400 ``{"data": [...]}``."""
    if not isinstance(data, dict):
        raise _fail("Ta’rif obyekt (JSON) bo‘lishi kerak.")
    return _VALIDATORS[kind](data, lookup or Lookup())


# --- Form runtime --------------------------------------------------------------------------------


def field_visible(field: Mapping[str, Any], user) -> bool:
    """ADMIN (and Super Admin) see every field; others need an empty ``visible_roles`` or a match."""
    if role_of(user) == Role.ADMIN:
        return True
    roles = field.get("visible_roles") or []
    return not roles or matches_any(user, roles)


def field_editable(field: Mapping[str, Any], user) -> bool:
    if field.get("type") == "computed":
        return False
    if getattr(user, "is_superuser", False):
        return True
    roles = field.get("editable_roles") or []
    return field_visible(field, user) and (not roles or matches_any(user, roles))


def visible_fields(fields: Iterable[Mapping[str, Any]], user) -> list[Mapping[str, Any]]:
    return [field for field in fields if field_visible(field, user)]


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _clean_number(value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError
    return int(number) if number == number.to_integral_value() and abs(number) < 2**53 else float(number)


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"[\s\-()]", "", value)
    if re.fullmatch(r"\d{9}", digits):
        digits = "+998" + digits
    elif re.fullmatch(r"998\d{9}", digits):
        digits = "+" + digits
    return digits if PHONE_RE.match(digits) else None


class FileChecker:
    """Checks ``file`` / ``photo`` values: the attachment exists, has purpose ``FORM_FILE`` and is either the
    actor's own fresh unbound upload or already bound to one of ``reusable`` (previous submissions of the task).
    """

    def __init__(self, actor, reusable_submission_ids: Iterable[int] = ()) -> None:
        self.actor = actor
        self.reusable = set(reusable_submission_ids)
        self.new_ids: set[int] = set()

    def check(self, value: Any, ftype: str) -> int:
        from core import files
        from core.models import Attachment, AttachmentPurpose

        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Fayl identifikatori noto‘g‘ri.")
        row = Attachment.objects.filter(pk=value).first()
        if row is None or row.purpose != AttachmentPurpose.FORM_FILE:
            raise ValueError("Fayl topilmadi.")
        if row.is_bound:
            if not (row.object_type == SUBMISSION_OBJECT_TYPE and row.object_id in self.reusable):
                raise ValueError("Fayl topilmadi yoki allaqachon biriktirilgan.")
        else:
            if row.uploaded_by_id != self.actor.pk or row.created_at < timezone.now() - files.BIND_WINDOW:
                raise ValueError("Fayl topilmadi yoki allaqachon biriktirilgan.")
            self.new_ids.add(row.pk)
        if ftype == "photo" and not row.content_type.startswith("image/"):
            raise ValueError("Bu maydonga faqat rasm (JPG, PNG, WEBP) yuklanadi.")
        return row.pk


#: ``Attachment.object_type`` of files bound to a :class:`builder.models.FormSubmission`.
SUBMISSION_OBJECT_TYPE = "form_submission"


def clean_value(field: Mapping[str, Any], value: Any, files: FileChecker) -> Any:
    """Normalised value of one field or ``ValueError`` with an Uzbek message."""
    ftype = field["type"]
    rules = field.get("validation") or {}
    if ftype == "text":
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError("Matn kiriting.")
        value = str(value).strip()
        if len(value) > (rules.get("max_length") or MAX_TEXT):
            raise ValueError(f"Ko‘pi bilan {rules.get('max_length') or MAX_TEXT} ta belgi kiriting.")
        if rules.get("min_length") and len(value) < rules["min_length"]:
            raise ValueError(f"Kamida {rules['min_length']} ta belgi kiriting.")
        if rules.get("pattern") and not re.fullmatch(rules["pattern"], value):
            raise ValueError("Qiymat talab qilingan formatga mos emas.")
        return value
    if ftype == "number":
        try:
            number = _clean_number(value)
        except (ValueError, InvalidOperation, TypeError):
            raise ValueError("Son kiriting.") from None
        if rules.get("min") is not None and number < rules["min"]:
            raise ValueError(f"Qiymat {rules['min']} dan kichik bo‘lmasligi kerak.")
        if rules.get("max") is not None and number > rules["max"]:
            raise ValueError(f"Qiymat {rules['max']} dan katta bo‘lmasligi kerak.")
        return number
    if ftype == "date":
        parsed = parse_date(value) if isinstance(value, str) else None
        if parsed is None:
            raise ValueError("Sana noto‘g‘ri formatda (YYYY-MM-DD).")
        return parsed.isoformat()
    if ftype == "time":
        if not isinstance(value, str) or not TIME_RE.match(value.strip()):
            raise ValueError("Vaqt noto‘g‘ri formatda (HH:MM).")
        return value.strip()
    if ftype == "phone":
        phone = normalize_phone(value) if isinstance(value, str) else None
        if phone is None:
            raise ValueError("Telefon raqami +998XXXXXXXXX formatida bo‘lishi kerak.")
        return phone
    if ftype == "select":
        if not isinstance(value, str) or value.strip() not in field.get("options", []):
            raise ValueError("Ro‘yxatdagi variantlardan birini tanlang.")
        return value.strip()
    if ftype == "multiselect":
        if not isinstance(value, list):
            raise ValueError("Variantlar ro‘yxatini yuboring.")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or item.strip() not in field.get("options", []):
                raise ValueError("Ro‘yxatdagi variantlardan tanlang.")
            if item.strip() not in result:
                result.append(item.strip())
        return result
    if ftype in FILE_TYPES:
        return files.check(value, ftype)
    if ftype == "geo":
        if not isinstance(value, dict):
            raise ValueError("Joylashuv {lat, lng} ko‘rinishida bo‘lishi kerak.")
        try:
            lat, lng = _clean_number(value.get("lat")), _clean_number(value.get("lng"))
        except (ValueError, InvalidOperation, TypeError):
            raise ValueError("Joylashuv koordinatalari son bo‘lishi kerak.") from None
        if not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise ValueError("Koordinatalar noto‘g‘ri (lat −90…90, lng −180…180).")
        return {"lat": float(lat), "lng": float(lng)}
    if ftype == "bool":
        if not isinstance(value, bool):
            raise ValueError("Ha yoki Yo‘q tanlang.")
        return value
    raise ValueError("Noma’lum maydon turi.")  # pragma: no cover - computed handled by the caller


def validate_values(
    fields: list[Mapping[str, Any]],
    values: Any,
    user,
    *,
    previous: Mapping[str, Any] | None = None,
    files: FileChecker,
) -> dict[str, Any]:
    """Clean form values (§1.2, §3.2): fields the user may not edit keep their ``previous`` value; computed
    fields are calculated here. Errors → ``ValidationError({"data": {key: [msg]}})``.
    """
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValidationError({"data": ["Qiymatlar obyekt (JSON) ko‘rinishida yuborilsin."]})
    previous = previous or {}
    result: dict[str, Any] = {}
    errors: dict[str, list[str]] = {}
    for field in fields:
        key, ftype = field["key"], field["type"]
        if ftype == "computed":
            continue
        if not field_editable(field, user):
            if key in previous:
                result[key] = previous[key]
            continue
        raw = values.get(key)
        if _empty(raw):
            if field.get("required"):
                errors[key] = [MSG_REQUIRED]
            result[key] = [] if ftype == "multiselect" else None
            continue
        try:
            result[key] = clean_value(field, raw, files)
        except ValueError as exc:
            errors[key] = [str(exc)]
    if errors:
        raise ValidationError({"data": errors})
    for field in computed_order(list(fields)):
        result[field["key"]] = evaluate_formula(field["formula"], result)
    return result


def display_value(field: Mapping[str, Any], value: Any, file_names: Mapping[int, str]) -> str:
    """Human readable value for ``Submission.display`` and acts."""
    if value is None or value == "" or value == []:
        return ""
    ftype = field.get("type")
    if ftype == "bool":
        return "Ha" if value else "Yo‘q"
    if ftype == "multiselect" and isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if ftype in FILE_TYPES:
        return file_names.get(value, f"Fayl #{value}") if isinstance(value, int) else ""
    if ftype == "geo" and isinstance(value, dict):
        return f"{value.get('lat')}, {value.get('lng')}"
    if ftype == "date" and isinstance(value, str):
        parsed = parse_date(value)
        return parsed.strftime("%d.%m.%Y") if parsed else value
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def due_date(starts_on: date, data: Mapping[str, Any]) -> date | None:
    """``end_date`` of the template or ``starts_on + duration_days``."""
    if data.get("end_date"):
        return parse_date(data["end_date"])
    if data.get("duration_days"):
        return starts_on + timedelta(days=int(data["duration_days"]))
    return None
