"""Samarqand viloyati: districts (tuman / shahar) with an approximate centre for the report map, and the
matching of a free-text `Case.district` to one of them ("Urgut", "urgut tumani", "Bulungʻur" -> the canonical name).
"""

#: (canonical name, latitude, longitude) — the centre is approximate: it places the district label on the map.
DISTRICTS: tuple[tuple[str, float, float], ...] = (
    ("Samarqand shahri", 39.654, 66.960),
    ("Kattaqo‘rg‘on shahri", 39.899, 66.257),
    ("Bulung‘ur tumani", 39.770, 67.270),
    ("Ishtixon tumani", 39.970, 66.490),
    ("Jomboy tumani", 39.700, 67.090),
    ("Kattaqo‘rg‘on tumani", 39.800, 66.100),
    ("Qo‘shrabot tumani", 40.270, 66.680),
    ("Narpay tumani", 39.930, 65.850),
    ("Nurobod tumani", 39.550, 66.280),
    ("Oqdaryo tumani", 39.860, 66.750),
    ("Pastdarg‘om tumani", 39.700, 66.620),
    ("Paxtachi tumani", 40.080, 65.650),
    ("Payariq tumani", 40.030, 66.920),
    ("Samarqand tumani", 39.570, 66.860),
    ("Toyloq tumani", 39.580, 67.180),
    ("Urgut tumani", 39.400, 67.250),
)
OTHER = "Boshqa / ko‘rsatilmagan"

_APOSTROPHES = str.maketrans({c: "‘" for c in "ʻʼ'`´’‛"})


def _norm(value: str) -> str:
    return " ".join((value or "").translate(_APOSTROPHES).lower().split())


def _base(value: str) -> str:
    return value.removesuffix(" tumani").removesuffix(" shahri").strip()


_FULL = {_norm(name): name for name, _, _ in DISTRICTS}
#: "samarqand" / "kattaqo‘rg‘on" alone mean the city (listed first)
_BASE: dict[str, str] = {}
for _name, _, _ in DISTRICTS:
    _BASE.setdefault(_base(_norm(_name)), _name)


def canonical(district: str) -> str | None:
    """The canonical district name of a free-text value, or None when it is empty / unknown."""
    key = _norm(district)
    if not key:
        return None
    return _FULL.get(key) or _BASE.get(_base(key)) or _BASE.get(_base(key.removesuffix(" t.").removesuffix(" sh.")))


def district_q(value: str):
    """Q for cases in district `value` (any spelling stored on the case that maps to the same district)."""
    from django.db.models import Q

    from .models import Case

    value = (value or "").strip()
    target = canonical(value)
    if target is None:
        base = value.removesuffix(" tumani").removesuffix(" shahri").strip()
        return Q(district__iexact=value) | Q(district__iexact=base)
    stored = [v for v in Case.all_objects.values_list("district", flat=True).distinct() if canonical(v) == target]
    return Q(district__in=stored)
