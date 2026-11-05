from django.apps import AppConfig


class OrdersConfig(AppConfig):
    """Orders (API_CONTRACT_V3 §6): Order, OrderItem, OrderStatusLog."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "orders"
    verbose_name = "Buyurtmalar"

    def ready(self) -> None:
        from audit import services as audit
        from audit.models import AuditObjectType
        from common.roles import Role
        from core import search

        from . import selectors, services
        from .models import Order

        audit.register_snapshot(
            Order,
            AuditObjectType.ORDER,
            lambda order: {
                "code": order.code,
                "kind": order.kind,
                "household_id": order.household_id,
                "status": order.status,
                "note": order.note,
                "desired_date": order.desired_date,
                "items": [
                    {"product_id": item.product_id, "requested_qty": item.requested_qty, "note": item.note}
                    for item in order.items.order_by("id")
                ],
            },
            op_id_attr="code",
        )
        search.register_group(
            "order", "Buyurtmalar", {Role.ADMIN, Role.WAREHOUSE, Role.OWNER}, selectors.search_group, order=40
        )

        # Logistics → orders back-channel: ``logistics/hooks.py`` imports ``orders.services.lock_order`` and
        # ``sync_order_with_task`` lazily. An older registration-style hooks module is supported as well.
        try:
            from logistics import hooks
        except ImportError:  # pragma: no cover - the app is always installed
            return
        register = getattr(hooks, "register_orders", None)
        if callable(register):
            register(lock=services.lock_order, on_task_status=services.sync_from_task, refs=services.order_refs)
