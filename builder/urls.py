"""URLs of the Konstruktor (API_CONTRACT_V4 §1–§3), included under ``/api/`` (no trailing slashes)."""

from django.urls import path

from . import views

urlpatterns = [
    # §1 configuration (Super Admin; meta + reads for ADMIN)
    path("builder/meta", views.MetaView.as_view(), name="builder-meta"),
    path("builder/blueprints", views.BlueprintListView.as_view(), name="builder-blueprints"),
    path("builder/blueprints/<id:pk>", views.BlueprintDetailView.as_view(), name="builder-blueprint"),
    path("builder/blueprints/<id:pk>/test", views.BlueprintTestView.as_view(), name="builder-blueprint-test"),
    path("builder/blueprints/<id:pk>/activate", views.BlueprintActivateView.as_view(),
         name="builder-blueprint-activate"),
    path("builder/blueprints/<id:pk>/discard-pending", views.BlueprintDiscardView.as_view(),
         name="builder-blueprint-discard"),
    path("builder/blueprints/<id:pk>/archive", views.BlueprintArchiveView.as_view(), name="builder-blueprint-archive"),
    path("builder/blueprints/<id:pk>/restore", views.BlueprintRestoreView.as_view(), name="builder-blueprint-restore"),
    path("builder/blueprints/<id:pk>/versions", views.BlueprintVersionsView.as_view(),
         name="builder-blueprint-versions"),
    path("builder/blueprints/<id:pk>/revert", views.BlueprintRevertView.as_view(), name="builder-blueprint-revert"),
    path("builder/blueprints/<id:pk>/duplicate", views.BlueprintDuplicateView.as_view(),
         name="builder-blueprint-duplicate"),
    # §2 role profiles and scope
    path("builder/user-access", views.UserAccessListView.as_view(), name="builder-user-access"),
    path("builder/user-access/<id:user_id>", views.UserAccessDetailView.as_view(), name="builder-user-access-detail"),
    # §3.1 forms
    path("forms", views.FormListView.as_view(), name="forms"),
    path("forms/submissions", views.SubmissionListView.as_view(), name="form-submissions"),
    path("forms/submissions/<id:pk>", views.SubmissionDetailView.as_view(), name="form-submission"),
    path("forms/<id:pk>/submissions", views.FormSubmitView.as_view(), name="form-submit"),
    # §3.2 tasks
    path("tasks", views.TaskListView.as_view(), name="tasks"),
    path("tasks/counts", views.TaskCountsView.as_view(), name="task-counts"),
    path("tasks/<id:pk>", views.TaskDetailView.as_view(), name="task"),
    path("tasks/<id:pk>/submit", views.TaskSubmitView.as_view(), name="task-submit"),
    path("tasks/<id:pk>/approve", views.TaskApproveView.as_view(), name="task-approve"),
    path("tasks/<id:pk>/return", views.TaskReturnView.as_view(), name="task-return"),
    path("tasks/<id:pk>/cancel", views.TaskCancelView.as_view(), name="task-cancel"),
    # §3.3 processes
    path("process-runs", views.ProcessRunListView.as_view(), name="process-runs"),
    path("process-runs/<id:pk>", views.ProcessRunDetailView.as_view(), name="process-run"),
    path("process-runs/<id:pk>/cancel", views.ProcessRunCancelView.as_view(), name="process-run-cancel"),
]
