"""Service entry points of the ``core`` app (re-exported for module teams).

- Company settings: :func:`get_settings`, :func:`lock_settings`, :func:`update_settings` (``settings_service.py``).
- Files: :mod:`core.files` (``upload``, ``bind_files``, ``files_for``, ``files_by_object``, ``can_view``,
  ``register_visibility``, ``FileIdsField``).
- Global search: :mod:`core.search` (``register_group``).
"""

from .settings_service import EDITABLE_FIELDS, get_settings, lock_settings, update_settings

__all__ = ["EDITABLE_FIELDS", "get_settings", "lock_settings", "update_settings"]
