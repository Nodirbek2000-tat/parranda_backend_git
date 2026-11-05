"""JSON fallbacks for errors raised outside DRF views (unknown URL, unhandled exception)."""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse


def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return JsonResponse({"detail": "Topilmadi."}, status=404)


def server_error(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"detail": "Serverda kutilmagan xatolik yuz berdi."}, status=500)
