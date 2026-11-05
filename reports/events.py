"""Notification texts and recipients of the reports app (API_CONTRACT_V3 §9.4).

Pattern (see notifications/events.py for complete examples)::

    from notifications import registry
    from notifications.compose import compose, household_rows, items_text
    from notifications.services import notify_on_commit

    def order_created(order) -> None:
        rows = [("Buyurtma", order.code), *household_rows(order.household), ...]
        notify_on_commit("order_created", key=order.pk, text=compose("order_created", rows, "Yangi", order.code),
                         staff_path=f"/orders/{order.pk}", owner_path=f"/my/orders/{order.pk}",
                         related_op=order.code, object_type="Order", object_id=order.pk,
                         contextual={registry.HOUSEHOLD_OWNER: order.household.owner})

Events of this app (already defined in notifications/registry.py):
#   (none: exports are audited only)
"""
