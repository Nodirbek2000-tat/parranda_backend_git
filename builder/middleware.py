"""Role profile enforcement (API_CONTRACT_V4 §2): a profile can only *narrow* what the base role may do.

Installed after Django's auth middleware. For every ``/api/`` request of a non-superuser with a role profile,
the path is mapped to a module and the method to an action; an action missing from
``profile.data["permissions"][module]`` answers ``403`` before the view runs. Authentication failures are left
to DRF (the request simply continues).
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

MSG_FORBIDDEN = "Rolingizda bu amal uchun ruxsat yo‘q."

EXCLUDED_PREFIXES = (
    "/api/auth/",
    "/api/me",
    "/api/health",
    "/api/bot/",
    "/api/my/notification",  # /api/my/notifications* and /api/my/notification-preferences*
    "/api/files",
    "/api/search",
    "/api/builder/meta",
)

#: First path segment → module (exact names).
SEGMENT_MODULES = {
    "households": "households",
    "my": "households",
    "batches": "batches",
    "movements": "batches",
    "products": "warehouse",
    "lots": "warehouse",
    "feed-norms": "warehouse",
    "construction-options": "warehouse",
    "orders": "orders",
    "logistics": "logistics",
    "vet": "vet",
    "reports": "reports",
    "stats": "stats",
    "dashboard": "stats",
    "map": "map",
    "users": "users",
    "notifications": "notifications",
    "notification-settings": "notifications",
    "audit": "audit",
    "tasks": "tasks",
    "process-runs": "processes",
    "forms": "forms",
    "acts": "acts",
}
#: First path segment prefixes → module (``stock*``, ``inventory*``).
PREFIX_MODULES = (("stock", "warehouse"), ("inventory", "warehouse"))

APPROVE_SEGMENTS = frozenset({"approve", "confirm", "reject", "return", "decide", "activate", "complete", "close"})
DOWNLOAD_SUFFIXES = ("/pdf", "/xlsx", "/export")


def module_of(path: str) -> str | None:
    segment = path[len("/api/"):].split("/", 1)[0]
    if segment in SEGMENT_MODULES:
        return SEGMENT_MODULES[segment]
    for prefix, module in PREFIX_MODULES:
        if segment.startswith(prefix):
            return module
    return None


def action_of(method: str, path: str, query: dict) -> str | None:
    method = method.upper()
    trimmed = path.rstrip("/")
    if method in ("GET", "HEAD"):
        fmt = (query.get("format") or "").lower()
        if trimmed.endswith(DOWNLOAD_SUFFIXES) or "/export" in trimmed or fmt in ("pdf", "xlsx"):
            return "download"
        return "view"
    if method == "POST":
        return "approve" if trimmed.rsplit("/", 1)[-1] in APPROVE_SEGMENTS else "create"
    if method in ("PUT", "PATCH", "DELETE"):
        return "edit"
    return None  # OPTIONS etc.


def _authenticated_user(request: HttpRequest):
    from rest_framework.exceptions import APIException

    from accounts.authentication import RevocableJWTAuthentication

    try:
        result = RevocableJWTAuthentication().authenticate(request)
    except (APIException, Exception):  # noqa: BLE001 - any auth problem: DRF answers as usual
        return None
    return result[0] if result else None


class AccessProfileMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._denied(request):
            return JsonResponse({"detail": MSG_FORBIDDEN}, status=403, json_dumps_params={"ensure_ascii": False})
        return self.get_response(request)

    def _denied(self, request: HttpRequest) -> bool:
        path = request.path_info
        if not path.startswith("/api/") or path.startswith(EXCLUDED_PREFIXES):
            return False
        module = module_of(path)
        if module is None:
            return False
        action = action_of(request.method or "GET", path, request.GET)
        if action is None:
            return False
        user = _authenticated_user(request)
        if user is None or user.is_superuser:
            return False
        from .access import access_of

        access = access_of(user)
        if access is None or access.profile_id is None:
            return False
        permissions = (access.profile.data or {}).get("permissions") or {}
        return action not in (permissions.get(module) or [])
