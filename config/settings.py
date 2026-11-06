"""NNP backend v2 settings. Every environment-specific value comes from `.env` (see `.env.example`)."""

import sys
from datetime import timedelta
from pathlib import Path

from environs import Env

from .database import parse_database_url, sqlite_config

BASE_DIR = Path(__file__).resolve().parent.parent

env = Env()
env.read_env(str(BASE_DIR / ".env"), recurse=False)

TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

SECRET_KEY: str = env.str("SECRET_KEY")
DEBUG: bool = env.bool("DEBUG", False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
if TESTING and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")

# Telegram bot token: validates Telegram Web App initData. Empty -> /api/auth/telegram always 401.
BOT_TOKEN: str = env.str("BOT_TOKEN", "")
# Shared secret with parranda_bot (X-Bot-Token header). Empty -> every /api/bot/... answers 403.
BOT_API_TOKEN: str = env.str("BOT_API_TOKEN", "")

CORS_ALLOWED_ORIGINS: list[str] = env.list("CORS_ALLOWED_ORIGINS", [])
CSRF_TRUSTED_ORIGINS: list[str] = env.list("CSRF_TRUSTED_ORIGINS", [])
NUM_PROXIES: int = env.int("NUM_PROXIES", 1)

# Behind nginx with HTTPS (Docker): trust X-Forwarded-Proto, secure cookies for Django admin.
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
    "accounts",
    "cases",
    "stock",
    "notify",
    "audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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

# SQLite by default (DB_PATH or db.sqlite3); DATABASE_URL (postgres://...) switches to PostgreSQL.
# The raw URL holds the password, so it is not kept as a setting (the DEBUG page lists settings).
_database_url: str = env.str("DATABASE_URL", "")
if _database_url:
    DATABASES = {"default": parse_database_url(_database_url, conn_max_age=env.int("CONN_MAX_AGE", 60))}
else:
    DATABASES = {"default": sqlite_config(env.path("DB_PATH", BASE_DIR / "db.sqlite3"))}
del _database_url

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LANGUAGE_CODE = "uz"
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True

# The API has no trailing slashes.
APPEND_SLASH = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Private files: there is no MEDIA_URL route. Files are streamed by GET /api/files/{kind}/{id}.
MEDIA_ROOT = env.path("MEDIA_ROOT", BASE_DIR / "media")
FILE_MAX_BYTES = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2_621_440
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("accounts.auth.NNPJWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ),
    "COERCE_DECIMAL_TO_STRING": False,
    "EXCEPTION_HANDLER": "config.exceptions.exception_handler",
    "NUM_PROXIES": NUM_PROXIES,
    "DEFAULT_THROTTLE_RATES": {"login": "20/min"},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": False,
}

# Optional TrueType font for the PDF (must contain Uzbek Latin glyphs); default: DejaVu/Vera fallback.
REPORT_FONT_PATH: str = env.str("REPORT_FONT_PATH", "")
REPORT_FONT_BOLD_PATH: str = env.str("REPORT_FONT_BOLD_PATH", "")

LOG_LEVEL: str = env.str("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"console": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "console"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "nnp": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
