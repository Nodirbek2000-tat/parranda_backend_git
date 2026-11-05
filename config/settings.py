"""Django settings for the NNP backend.

All environment-specific values are read from ``.env`` (see ``.env.example``).
"""

import sys
from datetime import timedelta
from pathlib import Path

from environs import Env

from .database import parse_database_url, sqlite_config

BASE_DIR = Path(__file__).resolve().parent.parent

env = Env()
env.read_env(str(BASE_DIR / ".env"), recurse=False)

SECRET_KEY: str = env.str("SECRET_KEY")
DEBUG: bool = env.bool("DEBUG", False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

# Telegram bot token: used only to validate Telegram Web App initData.
BOT_TOKEN: str = env.str("BOT_TOKEN", "")
# Shared secret between this API and parranda_bot (X-Bot-Token header).
BOT_API_TOKEN: str = env.str("BOT_API_TOKEN", "")

CORS_ALLOWED_ORIGINS: list[str] = env.list("CORS_ALLOWED_ORIGINS", [])

# Number of trusted reverse proxies that append to X-Forwarded-For (0 = use REMOTE_ADDR only).
NUM_PROXIES: int = env.int("NUM_PROXIES", 1)

# Production behind nginx with HTTPS (Docker): nginx sets X-Forwarded-Proto, Django trusts it,
# so request.is_secure() is true and cookies (Django admin) are sent only over HTTPS.
CSRF_TRUSTED_ORIGINS: list[str] = env.list("CSRF_TRUSTED_ORIGINS", [])
if env.bool("BEHIND_HTTPS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "accounts",
    "farm",
    "warehouse",
    "notifications",
    "dashboard",
    "audit",
    "stats",
    # V3 apps (API_CONTRACT_V3 §0.2). Module teams never edit this list again.
    "core",
    "orders",
    "logistics",
    "vet",
    "reports",
    # V4 (API_CONTRACT_V4): Konstruktor + dalolatnomalar.
    "builder",
    "acts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # V4 §2: role profiles narrow what the base role may do (403 before the view).
    "builder.middleware.AccessProfileMiddleware",
    # Source (API / BOT / ADMIN) and client IP of the request for audit rows.
    "audit.middleware.AuditContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite by default (DB_PATH lets checks such as scripts/integration_check.py run on a
# disposable copy). DATABASE_URL (postgres://user:pass@host:5432/name) switches to PostgreSQL.
# The raw URL (it contains the password) is deliberately NOT kept as a module-level setting: Django's
# debug page lists every uppercase setting. DATABASES.default.PASSWORD is masked by the reporter filter.
_database_url: str = env.str("DATABASE_URL", "")
if _database_url:
    DATABASES = {"default": parse_database_url(_database_url, conn_max_age=env.int("CONN_MAX_AGE", 60))}
else:
    DATABASES = {"default": sqlite_config(env.path("DB_PATH", BASE_DIR / "db.sqlite3"))}
del _database_url

# Masks DATABASE_URL / *_URL / DSN (settings and request.META) on the DEBUG 500 page as well.
DEFAULT_EXCEPTION_REPORTER_FILTER = "common.reporting.NNPExceptionReporterFilter"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

if len(sys.argv) > 1 and sys.argv[1] == "test":
    # Fast hashing keeps the test suite quick; never used outside "manage.py test".
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LANGUAGE_CODE = "uz"
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True

# The API contract forbids trailing slashes (the Next.js proxy strips them).
APPEND_SLASH = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Private files (API_CONTRACT_V3 §4.5) ------------------------------------------------------
# No MEDIA_URL route exists: files are streamed only by GET /api/files/{id} with permission checks.
MEDIA_ROOT = env.path("MEDIA_ROOT", BASE_DIR / "media")
FILE_STORAGE: str = env.str("FILE_STORAGE", "local")  # local | s3 (s3 needs django-storages + boto3)
S3_BUCKET: str = env.str("S3_BUCKET", "")
S3_ENDPOINT_URL: str = env.str("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY_ID: str = env.str("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY: str = env.str("S3_SECRET_ACCESS_KEY", "")
S3_REGION: str = env.str("S3_REGION", "")
#: Larger uploads are streamed to a temp file instead of memory (the 10 MB limit is checked in core.files).
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "accounts.authentication.RevocableJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": ("rest_framework.parsers.JSONParser",),
    "DEFAULT_PAGINATION_CLASS": "common.pagination.StandardPagination",
    "PAGE_SIZE": 50,
    "COERCE_DECIMAL_TO_STRING": False,
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",  # per client IP (and per Telegram id on /api/bot/link)
        "login_username": "10/min",  # per username on /api/auth/login + /api/bot/link
        "login_username_hour": "50/hour",
        "uploads": "60/hour",  # POST /api/files, /api/bot/files (per user)
        "reports": "30/min",  # report exports (per user)
        "bot_action": "60/min",  # bot task actions (per Telegram id)
    },
    # The per-IP throttle identifies clients by the X-Forwarded-For entry appended by the
    # nearest trusted proxy (ngrok in front of Next.js; Next's rewrite proxy forwards the
    # header unchanged and appends nothing). DRF's default (None) would use the whole,
    # client-controlled header. The per-username throttles do not depend on the IP at all.
    "NUM_PROXIES": NUM_PROXIES,
    "EXCEPTION_HANDLER": "common.exceptions.exception_handler",
    "DATE_INPUT_FORMATS": ["iso-8601"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "NNP API",
    "DESCRIPTION": (
        "Navobod naslli parranda qisqarmasi — API "
        "(docs/API_CONTRACT.md, docs/API_CONTRACT_V2.md, docs/API_CONTRACT_V3.md)."
    ),
    "VERSION": "3.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api",
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": False, "displayOperationId": False},
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "botToken": {"type": "apiKey", "in": "header", "name": "X-Bot-Token"},
        }
    },
    "ENUM_NAME_OVERRIDES": {
        "RoleEnum": "common.roles.Role",
        "StaffRoleEnum": "common.roles.STAFF_ROLE_CHOICES",
        "PoultryTypeEnum": "farm.models.PoultryType",
        "MovementTypeEnum": "farm.models.MovementType",
        "ProductCategoryEnum": "warehouse.models.ProductCategory",
        "UnitEnum": "warehouse.models.Unit",
        "StockTransactionTypeEnum": "warehouse.models.StockTransactionType",
        "AuditModuleEnum": "audit.models.AuditModule",
        "AuditActionEnum": "audit.models.AuditAction",
        "AuditSourceEnum": "audit.models.AuditSource",
        "AuditObjectTypeEnum": "audit.models.AuditObjectType",
        "DeltaUnitEnum": [("%", "%"), ("pp", "pp")],
        "HealthStateEnum": [("ok", "ok"), ("error", "error")],
        "DeliveryStatusEnum": [("SENT", "SENT"), ("PENDING", "PENDING"), ("FAILED", "FAILED")],
        "NotificationStatusEnum": "notifications.models.NotificationStatus",
        "AttachmentPurposeEnum": "core.models.AttachmentPurpose",
        # V4 Konstruktor
        "BlueprintKindEnum": "builder.models.BlueprintKind",
        "BlueprintStatusEnum": "builder.models.BlueprintStatus",
        "ActKindEnum": "acts.models.ActKind",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": False,
}

LOG_LEVEL: str = env.str("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "console"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        # Server errors (5xx) with tracebacks; expected 4xx answers are not logged.
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "nnp": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
