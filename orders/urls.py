"""URLs of the orders app, included under ``/api/`` by ``config/urls.py`` (no trailing slashes).

Use ``<id:pk>`` for ids (64-bit bounded converter registered once in ``config/urls.py``).
"""

from django.urls import path

from . import views

urlpatterns = [
    path("orders", views.OrderListCreateView.as_view(), name="order-list"),
    path("orders/<id:pk>", views.OrderDetailView.as_view(), name="order-detail"),
    path("orders/<id:pk>/approve", views.OrderApproveView.as_view(), name="order-approve"),
    path("orders/<id:pk>/reject", views.OrderRejectView.as_view(), name="order-reject"),
    path("orders/<id:pk>/cancel", views.OrderCancelView.as_view(), name="order-cancel"),
    path("orders/<id:pk>/create-task", views.OrderCreateTaskView.as_view(), name="order-create-task"),
    path("catalog/products", views.CatalogProductsView.as_view(), name="catalog-products"),
]
