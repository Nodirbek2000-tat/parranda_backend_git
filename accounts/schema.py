"""drf-spectacular extension: the project's JWT authentication is documented as ``jwtAuth`` (Bearer)."""

from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme


class RevocableJWTScheme(SimpleJWTScheme):
    target_class = "accounts.authentication.RevocableJWTAuthentication"
    name = "jwtAuth"
