"""Exception reporter filter that also hides connection URLs (DEBUG error page, error e-mails).

Django's default filter masks settings / ``request.META`` keys matching
``API|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE``. ``DATABASE_URL`` (``postgres://user:password@host/db``)
matches none of them, and ``environs`` copies ``.env`` into ``os.environ`` (which ``runserver`` puts into the
WSGI environ), so the URL would be shown in clear text in the META section of the technical 500 page.
"""

from __future__ import annotations

import re

from django.views.debug import SafeExceptionReporterFilter


class NNPExceptionReporterFilter(SafeExceptionReporterFilter):
    hidden_settings = re.compile(
        r"API|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE|DATABASE_URL|DSN|_URL$",
        flags=re.IGNORECASE,
    )
