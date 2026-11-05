from django.urls import path

from . import views

urlpatterns = [
    path("stats/overview", views.OverviewStatsView.as_view(), name="stats-overview"),
    path("warehouse/stats", views.WarehouseStatsView.as_view(), name="warehouse-stats"),
    path("my/stats", views.MyStatsView.as_view(), name="my-stats"),
    path("bot/users/<id:telegram_id>/summary", views.BotSummaryView.as_view(), name="bot-user-summary"),
]
