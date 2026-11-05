"""Server side validation of ``Telegram.WebApp.initData`` (never trust the client)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 86_400


class InitDataInvalid(Exception):
    """Signature mismatch, malformed payload or expired ``auth_date``."""


def build_data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields))


def sign(data_check_string: str, bot_token: str) -> str:
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class InitData:
    user_id: int
    #: Unix time at which Telegram signed the payload (when the Web App was opened).
    auth_date: int


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Return the Telegram user id from a correctly signed, fresh ``initData`` string."""
    return parse_init_data(init_data, bot_token, max_age=max_age, now=now).user_id


def parse_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> InitData:
    """Validated ``initData``: Telegram user id and ``auth_date``."""
    if not bot_token or not init_data:
        raise InitDataInvalid("empty")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataInvalid("malformed") from exc
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InitDataInvalid("duplicate keys")
    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise InitDataInvalid("no hash")

    expected = sign(build_data_check_string(fields), bot_token)
    # Compare bytes: str comparison raises TypeError on non-ASCII input (would be a 500).
    if not hmac.compare_digest(expected.encode(), received_hash.encode()):
        raise InitDataInvalid("bad signature")

    try:
        auth_date = int(fields["auth_date"])
        user_id = int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataInvalid("missing fields") from exc

    current = time.time() if now is None else now
    if current - auth_date > max_age:
        raise InitDataInvalid("expired")
    return InitData(user_id=user_id, auth_date=auth_date)
