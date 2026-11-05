from django.urls import path

from . import views, web_views

urlpatterns = [
    # Bot-only (X-Bot-Token)
    path("bot/users/<id:telegram_id>", views.BotUserView.as_view(), name="bot-user"),
    path("bot/users/<id:telegram_id>/notifications", views.BotInboxView.as_view(), name="bot-user-notifications"),
    path("bot/link", views.BotLinkView.as_view(), name="bot-link"),
    path("bot/unlink", views.BotUnlinkView.as_view(), name="bot-unlink"),
    path("bot/location", views.BotLocationView.as_view(), name="bot-location"),
    path("bot/notifications/pending", views.PendingNotificationsView.as_view(), name="bot-notifications-pending"),
    path(
        "bot/notifications/<id:pk>/result",
        views.NotificationResultView.as_view(),
        name="bot-notification-result",
    ),
    path("bot/reminders/run", views.BotReminderRunView.as_view(), name="bot-reminders-run"),
    # Monitor (ADMIN)
    path("notifications", web_views.NotificationListView.as_view(), name="notification-list"),
    path("notifications/stats", web_views.NotificationStatsView.as_view(), name="notification-stats"),
    path("notifications/retry-failed", web_views.NotificationRetryFailedView.as_view(), name="notification-retry-failed"),
    path("notifications/<id:pk>", web_views.NotificationDetailView.as_view(), name="notification-detail"),
    path("notifications/<id:pk>/retry", web_views.NotificationRetryView.as_view(), name="notification-retry"),
    # Settings (GET ADMIN, PATCH Super Admin)
    path("notification-settings", web_views.NotificationSettingListView.as_view(), name="notification-settings"),
    path(
        "notification-settings/<str:event>",
        web_views.NotificationSettingDetailView.as_view(),
        name="notification-setting-detail",
    ),
    path(
        "notification-settings/<str:event>/versions",
        web_views.NotificationSettingVersionsView.as_view(),
        name="notification-setting-versions",
    ),
    path(
        "notification-settings/<str:event>/revert",
        web_views.NotificationSettingRevertView.as_view(),
        name="notification-setting-revert",
    ),
    # Bell (any role, own rows)
    path("my/notifications", web_views.MyNotificationListView.as_view(), name="my-notifications"),
    path(
        "my/notification-preferences",
        web_views.MyNotificationPreferencesView.as_view(),
        name="my-notification-preferences",
    ),
    path(
        "my/notification-preferences/<str:event>",
        web_views.MyNotificationPreferenceView.as_view(),
        name="my-notification-preference",
    ),
    path("my/notifications/unread-count", web_views.MyUnreadCountView.as_view(), name="my-notifications-unread"),
    path("my/notifications/read-all", web_views.MyNotificationReadAllView.as_view(), name="my-notifications-read-all"),
    path("my/notifications/<id:pk>/read", web_views.MyNotificationReadView.as_view(), name="my-notification-read"),
]
