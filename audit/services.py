"""Writing audit rows (API_CONTRACT_V2 §2).

:func:`record` must be called **inside the same** ``transaction.atomic()`` **block** as the business
change, so a rolled back change leaves no audit row and a failing audit insert fails the request.
Outside an HTTP request (no :mod:`audit.context`) nothing is written: management commands and
migrations are not audited.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db import models
from django.utils import timezone

from accounts.models import User
from common.roles import role_of
from farm.models import DailyReport, Household, PoultryBatch, PoultryMovement
from warehouse.models import Product, StockTransaction, StockTransactionType

from .context import current
from .models import AuditAction, AuditLog, AuditModule, AuditObjectType, AuditStatus

#: Keys that are never stored (exact names, case-insensitive).
DENY_KEYS = frozenset(
    {"password", "temp_password", "old_password", "new_password", "token_version", "init_data", "access", "refresh"}
)
#: Any key containing one of these fragments is dropped too ...
DENY_FRAGMENTS = ("password", "token", "secret")
#: ... except these explicitly allowed flags (the contract stores ``must_change_password``).
ALLOW_KEYS = frozenset({"must_change_password"})

MAX_REPR = 200
MAX_ACTOR_NAME = 200
MAX_SUBMITTED_USERNAME = 150


def is_denied_key(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in ALLOW_KEYS:
        return False
    return lowered in DENY_KEYS or any(fragment in lowered for fragment in DENY_FRAGMENTS)


def to_json(value: Any) -> Any:
    """JSON-native value: ``Decimal`` → number, ``date`` → ``YYYY-MM-DD``, ``datetime`` → ISO 8601."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, datetime):
        return timezone.localtime(value).isoformat() if timezone.is_aware(value) else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json(item) for item in value]
    if isinstance(value, models.Model):
        return value.pk
    return str(value)


def sanitize(value: Any) -> Any:
    """Recursively drop secret keys (``password``, tokens, ``init_data`` ...) and convert to JSON."""
    if isinstance(value, Mapping):
        return {str(key): sanitize(item) for key, item in value.items() if not is_denied_key(str(key))}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return to_json(value)


# --- Snapshots (API_CONTRACT_V2 §2.3) -----------------------------------------------------


def _user_snapshot(user: User) -> dict[str, Any]:
    return {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        # Not in the contract list; added so privilege changes made in the Django admin are visible.
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
    }


def _household_snapshot(household: Household) -> dict[str, Any]:
    return {
        "code": household.code,
        "name": household.name,
        "owner_id": household.owner_id,
        "phone": household.phone,
        "region": household.region,
        "district": household.district,
        "mfy": household.mfy,
        "address": household.address,
        "latitude": household.latitude,
        "longitude": household.longitude,
        "is_active": household.is_active,
        "contract_info": household.contract_info,
        "is_demo": household.is_demo,
    }


def _batch_snapshot(batch: PoultryBatch) -> dict[str, Any]:
    return {
        "code": batch.code,
        "poultry_type": batch.poultry_type,
        "breed": batch.breed,
        "initial_quantity": batch.initial_quantity,
        "age_days": batch.age_days,
        "supplier": batch.supplier,
        "arrived_at": batch.arrived_at,
        "initial_weight_g": batch.initial_weight_g,
        "health_status": batch.health_status,
    }


def _movement_snapshot(movement: PoultryMovement) -> dict[str, Any]:
    return {
        "op_id": movement.op_id,
        "batch_id": movement.batch_id,
        "batch_code": movement.batch.code,
        "household_id": movement.household_id,
        "type": movement.type,
        "quantity": movement.quantity,
        "date": movement.date,
        "note": movement.note,
    }


def _report_snapshot(report: DailyReport) -> dict[str, Any]:
    return {
        "op_id": report.op_id,
        "household_id": report.household_id,
        "date": report.date,
        "bird_count": report.bird_count,
        "feed_kg": report.feed_kg,
        "meat_kg": report.meat_kg,
        "deaths": report.deaths,
        "avg_weight_g": report.avg_weight_g,
        "note": report.note,
        "death_op_id": report.death_movement.op_id if report.death_movement_id else None,
    }


def _product_snapshot(product: Product) -> dict[str, Any]:
    return {
        "name": product.name,
        "category": product.category,
        "unit": product.unit,
        "min_stock": product.min_stock,
        "is_active": product.is_active,
    }


def _stock_snapshot(row: StockTransaction) -> dict[str, Any]:
    sign = -1 if row.type in (StockTransactionType.OUT, "ADJUST_OUT") else 1  # V3 §5.3: ADJUST_OUT is outgoing
    return {
        "op_id": row.op_id,
        "product_id": row.product_id,
        "product_name": row.product.name,
        "type": row.type,
        "quantity": row.quantity,
        "balance_before": row.balance_after - sign * row.quantity,
        "balance_after": row.balance_after,
        "household_id": row.household_id,
        "document_no": row.document_no,
        "note": row.note,
    }


_SNAPSHOTS = {
    User: (AuditObjectType.USER, _user_snapshot),
    Household: (AuditObjectType.HOUSEHOLD, _household_snapshot),
    PoultryBatch: (AuditObjectType.BATCH, _batch_snapshot),
    PoultryMovement: (AuditObjectType.MOVEMENT, _movement_snapshot),
    DailyReport: (AuditObjectType.DAILY_REPORT, _report_snapshot),
    Product: (AuditObjectType.PRODUCT, _product_snapshot),
    StockTransaction: (AuditObjectType.STOCK_TRANSACTION, _stock_snapshot),
}


#: Models whose ``op_id`` is read from an attribute (``code`` for V3 objects: ``BY-000005``, ``LG-000012`` ...).
_OP_ID_ATTRS: dict[type[models.Model], str] = {}


def register_snapshot(
    model: type[models.Model],
    object_type: str,
    builder: Callable[[Any], dict[str, Any]],
    *,
    op_id_attr: str | None = None,
) -> None:
    """Make ``model`` auditable (V3 modules call this from ``AppConfig.ready()``).

    ``object_type`` must be an :class:`AuditObjectType` value; ``builder(obj)`` returns the business fields
    (no file bytes, storage paths or secrets). ``op_id_attr`` (e.g. ``"code"``) fills ``AuditLog.op_id``.
    """
    if object_type not in AuditObjectType.values:
        raise ValueError(f"Unknown AuditObjectType: {object_type}")
    _SNAPSHOTS[model] = (object_type, builder)
    if op_id_attr:
        _OP_ID_ATTRS[model] = op_id_attr


def object_type_of(obj: models.Model) -> str:
    return _SNAPSHOTS[type(obj)][0]


def snapshot(obj: models.Model) -> dict[str, Any]:
    """JSON-native business fields of ``obj`` (secrets are never part of a snapshot)."""
    return sanitize(_SNAPSHOTS[type(obj)][1](obj))


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only the keys whose value changed: ``(old values, new values)``."""
    keys = [key for key in after if before.get(key) != after.get(key)]
    return {key: before.get(key) for key in keys}, {key: after.get(key) for key in keys}


def op_id_of(obj: models.Model | None) -> str:
    if isinstance(obj, (PoultryMovement, DailyReport, StockTransaction)) and obj.pk:
        return obj.op_id
    attr = _OP_ID_ATTRS.get(type(obj)) if obj is not None else None
    if attr and obj.pk:
        return str(getattr(obj, attr, "") or "")[:20]
    return ""


# --- Writing -----------------------------------------------------------------------------


def _related_refs(obj: models.Model | None) -> tuple[int | None, int | None]:
    """``(household_id, product_id)`` the audited object belongs to (TZ §14), from its own fields."""
    if obj is None:
        return None, None
    name = type(obj).__name__
    household = obj.pk if name == "Household" else getattr(obj, "household_id", None)
    if household is None and name == "User":
        household = getattr(obj, "household_id_or_none", None)
    product = obj.pk if name == "Product" else getattr(obj, "product_id", None)
    return (household if isinstance(household, int) else None), (product if isinstance(product, int) else None)


def record(
    *,
    module: str,
    action: str,
    obj: models.Model | None = None,
    actor: User | None = None,
    actor_name: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    op_id: str | None = None,
    object_type: str | None = None,
) -> AuditLog | None:
    """Insert one audit row (no-op outside an HTTP request). Call inside the business transaction.

    ``object_type`` is only needed without ``obj`` (e.g. ``AuditObjectType.REPORT`` for an export).
    """
    context = current()
    if context is None:
        return None
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    if actor_name is None:
        actor_name = str(actor) if actor is not None else ""
    household_ref, product_ref = _related_refs(obj)
    return AuditLog.objects.create(
        op_id=op_id if op_id is not None else op_id_of(obj),
        actor=actor,
        actor_name=actor_name[:MAX_ACTOR_NAME],
        role=role_of(actor) or "",
        source=context.source,
        module=module,
        action=action,
        object_type=object_type_of(obj) if obj is not None else (object_type or ""),
        object_id=obj.pk if obj is not None else None,
        object_repr=str(obj)[:MAX_REPR] if obj is not None else "",
        before=sanitize(before) if before is not None else None,
        after=sanitize(after) if after is not None else None,
        ip=context.ip,
        household_ref=household_ref,
        product_ref=product_ref,
        status=AuditStatus.FAILED if action == AuditAction.LOGIN_FAILURE else AuditStatus.SUCCESS,
    )


def record_create(module: str, obj: models.Model, actor: User | None, *, extra: Mapping[str, Any] | None = None):
    after = snapshot(obj)
    if extra:
        after.update(sanitize(extra))
    return record(module=module, action=AuditAction.CREATE, obj=obj, actor=actor, after=after)


def record_update(
    module: str, obj: models.Model, actor: User | None, before_snapshot: Mapping[str, Any]
) -> AuditLog | None:
    """``update`` row with only the changed fields; nothing is written for a no-op update."""
    old, new = diff(before_snapshot, snapshot(obj))
    if not new:
        return None
    return record(module=module, action=AuditAction.UPDATE, obj=obj, actor=actor, before=old, after=new)


def context_user() -> User | None:
    """Django admin session user of the current request (``None`` elsewhere)."""
    context = current()
    user = getattr(context, "user", None) if context is not None else None
    return user if user is not None and getattr(user, "is_authenticated", False) else None


__all__ = [
    "AuditAction",
    "AuditModule",
    "context_user",
    "diff",
    "record",
    "record_create",
    "record_update",
    "register_snapshot",
    "sanitize",
    "snapshot",
]
