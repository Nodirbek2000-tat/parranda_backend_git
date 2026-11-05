"""Products catalogue data, feed norms and construction options (API_CONTRACT_V4 §5).

Every change writes a :class:`builder.models.ConfigVersion` row (full state after the change) and an audit row
(module ``products``). Feed norm and construction option rows have no :class:`audit.models.AuditObjectType`
of their own, so they are audited without an object binding: ``op_id`` = their code (``YM-000001`` /
``QV-000001``) and ``after`` carries ``{"object": "feed_norm", "id": …, …}``.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Max
from rest_framework.exceptions import ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from builder.models import ConfigVersion

from .models import ConstructionOption, FeedNorm, Product

MSG_NORM_OVERLAP = "Bu parranda turi uchun {day_from}–{day_to} kun oralig‘i boshqa me’yor bilan kesishadi."
MSG_OPTION_DUPLICATE = "Bu nomdagi qurilish varianti allaqachon mavjud"

#: Product fields added by V4 §5 (not part of the V2 audit snapshot, so audited here).
PRODUCT_CATALOGUE_FIELDS = ("price", "consumption_norm", "dosage", "description")

OBJECT_PRODUCT = "product"
OBJECT_FEED_NORM = "feed_norm"
OBJECT_CONSTRUCTION_OPTION = "construction_option"


# --- Config versions ----------------------------------------------------------------------------


def write_version(
    *, object_type: str, object_id: int, name: str, data: dict[str, Any], actor: User | None,
    status: str = "", comment: str = "",
) -> ConfigVersion:
    """Append the next version of ``object_type``/``object_id`` (call inside the change's transaction)."""
    last = ConfigVersion.objects.filter(object_type=object_type, object_id=object_id).aggregate(v=Max("version"))["v"]
    return ConfigVersion.objects.create(
        object_type=object_type,
        object_id=object_id,
        version=(last or 0) + 1,
        name=name[:200],
        data=audit.sanitize(data),
        status=status,
        comment=comment[:500],
        created_by=actor if actor is not None and actor.is_authenticated else None,
    )


# --- Products ------------------------------------------------------------------------------------


def product_state(product: Product) -> dict[str, Any]:
    """Audit snapshot of a product including the V4 catalogue fields (JSON-native)."""
    state = audit.snapshot(product)
    state.update(audit.sanitize({field: getattr(product, field) for field in PRODUCT_CATALOGUE_FIELDS}))
    return state


def record_product_created(product: Product, actor: User | None) -> None:
    state = product_state(product)
    audit.record(module=AuditModule.PRODUCTS, action=AuditAction.CREATE, obj=product, actor=actor, after=state)
    write_version(object_type=OBJECT_PRODUCT, object_id=product.pk, name=product.name, data=state, actor=actor)


def record_product_updated(product: Product, actor: User | None, before: dict[str, Any]) -> None:
    """Audit ``update`` with the changed fields and a new config version; nothing for a no-op."""
    state = product_state(product)
    old, new = audit.diff(before, state)
    if not new:
        return
    audit.record(module=AuditModule.PRODUCTS, action=AuditAction.UPDATE, obj=product, actor=actor, before=old, after=new)
    write_version(object_type=OBJECT_PRODUCT, object_id=product.pk, name=product.name, data=state, actor=actor)


# --- Shared CRUD plumbing ------------------------------------------------------------------------


def _state(obj: FeedNorm | ConstructionOption) -> dict[str, Any]:
    if isinstance(obj, FeedNorm):
        return audit.sanitize(
            {
                "poultry_type": obj.poultry_type,
                "day_from": obj.day_from,
                "day_to": obj.day_to,
                "grams_per_bird_day": obj.grams_per_bird_day,
                "product_id": obj.product_id,
                "note": obj.note,
            }
        )
    return audit.sanitize(
        {
            "name": obj.name,
            "description": obj.description,
            "unit": obj.unit,
            "price": obj.price,
            "is_active": obj.is_active,
        }
    )


def _object_type(obj: FeedNorm | ConstructionOption) -> str:
    return OBJECT_FEED_NORM if isinstance(obj, FeedNorm) else OBJECT_CONSTRUCTION_OPTION


def _track(obj, actor: User | None, action: str, *, pk: int, before: dict | None, after: dict | None) -> None:
    object_type = _object_type(obj)
    code = f"{'YM' if object_type == OBJECT_FEED_NORM else 'QV'}-{pk:06d}"
    old, new = (before, after) if action != AuditAction.UPDATE else audit.diff(before or {}, after or {})
    audit.record(
        module=AuditModule.PRODUCTS,
        action=action,
        actor=actor,
        op_id=code,
        before={"object": object_type, "id": pk, **old} if old is not None else None,
        after={"object": object_type, "id": pk, **new} if new is not None else None,
    )
    state = after if after is not None else before
    write_version(
        object_type=object_type,
        object_id=pk,
        name=str(obj),
        data=state or {},
        actor=actor,
        status="deleted" if action == AuditAction.DELETE else "",
    )


# --- Feed norms ----------------------------------------------------------------------------------


def _check_overlap(poultry_type: str, day_from: int, day_to: int, exclude_pk: int | None = None) -> None:
    # Lock the type's norms so two concurrent writes cannot both pass the check.
    others = FeedNorm.objects.select_for_update().filter(poultry_type=poultry_type)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    clash = others.filter(day_from__lte=day_to, day_to__gte=day_from).order_by("day_from").first()
    if clash is not None:
        raise ValidationError(
            {"day_from": [MSG_NORM_OVERLAP.format(day_from=clash.day_from, day_to=clash.day_to)]}
        )


def create_feed_norm(*, actor: User | None, data: dict[str, Any]) -> FeedNorm:
    with transaction.atomic():
        _check_overlap(data["poultry_type"], data["day_from"], data["day_to"])
        norm = FeedNorm.objects.create(**data)
        _track(norm, actor, AuditAction.CREATE, pk=norm.pk, before=None, after=_state(norm))
    return norm


def update_feed_norm(*, actor: User | None, norm_id: int, changes: dict[str, Any]) -> FeedNorm:
    with transaction.atomic():
        norm = FeedNorm.objects.select_for_update().get(pk=norm_id)
        before = _state(norm)
        for field, value in changes.items():
            setattr(norm, field, value)
        if norm.day_from > norm.day_to:
            raise ValidationError({"day_to": ["“Kungacha” “Kundan”dan kichik bo‘lmasligi kerak."]})
        _check_overlap(norm.poultry_type, norm.day_from, norm.day_to, exclude_pk=norm.pk)
        norm.save()
        after = _state(norm)
        if after != before:
            _track(norm, actor, AuditAction.UPDATE, pk=norm.pk, before=before, after=after)
    return norm


def delete_feed_norm(*, actor: User | None, norm_id: int) -> None:
    with transaction.atomic():
        norm = FeedNorm.objects.select_for_update().get(pk=norm_id)
        before = _state(norm)
        pk = norm.pk
        norm.delete()
        _track(norm, actor, AuditAction.DELETE, pk=pk, before=before, after=None)


# --- Construction options -----------------------------------------------------------------------


def create_construction_option(*, actor: User | None, data: dict[str, Any]) -> ConstructionOption:
    try:
        with transaction.atomic():
            option = ConstructionOption.objects.create(**data)
            _track(option, actor, AuditAction.CREATE, pk=option.pk, before=None, after=_state(option))
    except IntegrityError:
        raise ValidationError({"name": [MSG_OPTION_DUPLICATE]}) from None
    return option


def update_construction_option(*, actor: User | None, option_id: int, changes: dict[str, Any]) -> ConstructionOption:
    try:
        with transaction.atomic():
            option = ConstructionOption.objects.select_for_update().get(pk=option_id)
            before = _state(option)
            for field, value in changes.items():
                setattr(option, field, value)
            option.save()
            after = _state(option)
            if after != before:
                _track(option, actor, AuditAction.UPDATE, pk=option.pk, before=before, after=after)
    except IntegrityError:
        raise ValidationError({"name": [MSG_OPTION_DUPLICATE]}) from None
    return option


def delete_construction_option(*, actor: User | None, option_id: int) -> None:
    with transaction.atomic():
        option = ConstructionOption.objects.select_for_update().get(pk=option_id)
        before = _state(option)
        pk = option.pk
        option.delete()
        _track(option, actor, AuditAction.DELETE, pk=pk, before=before, after=None)
