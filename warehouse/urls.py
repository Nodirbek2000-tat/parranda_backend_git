from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter(trailing_slash=False)
router.register("products", views.ProductViewSet, basename="product")
router.register("stock-transactions", views.StockTransactionViewSet, basename="stock-transaction")
router.register("stock-lots", views.StockLotViewSet, basename="stock-lot")
router.register("inventory-counts", views.InventoryCountViewSet, basename="inventory-count")

urlpatterns = router.urls
