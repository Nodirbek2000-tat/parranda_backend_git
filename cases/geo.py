"""Samarqand viloyati: districts (tuman / shahar) with an approximate centre for the report map, and the
matching of a free-text `Case.district` to one of them ("Urgut", "urgut tumani", "Bulungʻur" -> the canonical name).
"""

#: (canonical name, latitude, longitude): a point well inside the district (from the geoBoundaries outline,
#: see parranda_next/lib/geo/samarqand-tumanlar.json) — the label / bubble position on the report map.
DISTRICTS: tuple[tuple[str, float, float], ...] = (
    ("Samarqand shahri", 39.6180, 66.9762),
    ("Kattaqo‘rg‘on shahri", 39.8544, 66.2790),
    ("Bulung‘ur tumani", 39.7754, 67.3481),
    ("Ishtixon tumani", 39.9365, 66.5238),
    ("Jomboy tumani", 39.7522, 67.0948),
    ("Kattaqo‘rg‘on tumani", 40.1402, 66.2975),
    ("Qo‘shrabot tumani", 40.3905, 66.4667),
    ("Narpay tumani", 39.8788, 65.8875),
    ("Nurobod tumani", 39.6525, 65.8691),
    ("Oqdaryo tumani", 39.8200, 66.7156),
    ("Pastdarg‘om tumani", 39.6550, 66.6778),
    ("Paxtachi tumani", 39.8730, 65.6119),
    ("Payariq tumani", 39.9282, 66.8932),
    ("Samarqand tumani", 39.5309, 66.9443),
    ("Toyloq tumani", 39.5107, 67.2209),
    ("Urgut tumani", 39.3842, 67.3246),
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
