"""URLs of the logistics app, included under ``/api/`` by ``config/urls.py`` (no trailing slashes)."""

from django.urls import path

from . import views

urlpatterns = [
    path("logistics/vehicles", views.VehicleListView.as_view(), name="logistics-vehicles"),
    path("logistics/vehicles/<id:pk>", views.VehicleDetailView.as_view(), name="logistics-vehicle"),
    path("logistics/drivers", views.DriverListView.as_view(), name="logistics-drivers"),
    path("logistics/drivers/<id:pk>", views.DriverDetailView.as_view(), name="logistics-driver"),
    path("logistics/summary", views.SummaryView.as_view(), name="logistics-summary"),
    path("logistics/tasks", views.TaskListView.as_view(), name="logistics-tasks"),
    path("logistics/tasks/<id:pk>", views.TaskDetailView.as_view(), name="logistics-task"),
    path("logistics/tasks/<id:pk>/assign", views.TaskAssignView.as_view(), name="logistics-task-assign"),
    path("logistics/tasks/<id:pk>/accept", views.TaskAcceptView.as_view(), name="logistics-task-accept"),
    path("logistics/tasks/<id:pk>/start", views.TaskStartView.as_view(), name="logistics-task-start"),
    path("logistics/tasks/<id:pk>/deliver", views.TaskDeliverView.as_view(), name="logistics-task-deliver"),
    path("logistics/tasks/<id:pk>/confirm", views.TaskConfirmView.as_view(), name="logistics-task-confirm"),
    path("logistics/tasks/<id:pk>/cancel", views.TaskCancelView.as_view(), name="logistics-task-cancel"),
    # Bot (X-Bot-Token, §14.1)
    path("bot/users/<id:telegram_id>/tasks", views.BotTasksView.as_view(), name="bot-user-tasks"),
    path("bot/tasks/<id:pk>/accept", views.BotTaskAcceptView.as_view(), name="bot-task-accept"),
    path("bot/tasks/<id:pk>/start", views.BotTaskStartView.as_view(), name="bot-task-start"),
    path("bot/tasks/<id:pk>/deliver", views.BotTaskDeliverView.as_view(), name="bot-task-deliver"),
    path("bot/tasks/<id:pk>/confirm", views.BotTaskConfirmView.as_view(), name="bot-task-confirm"),
    path("bot/files", views.BotFileUploadView.as_view(), name="bot-files"),
]
