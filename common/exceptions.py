"""Exception handler that keeps DRF's error shape but guarantees Uzbek (Latin) messages.

DRF ships without an Uzbek catalog, so its built-in messages would leak to the UI in
English. They are translated here; messages written by this project are already Uzbek
and pass through unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler

_EXACT: dict[str, str] = {
    "This field is required.": "Bu maydon to‘ldirilishi shart.",
    # Django's own "uz" catalog already translates a few DRF msgids; normalise them too.
    "Ushbu maydon to'ldirilishi shart.": "Bu maydon to‘ldirilishi shart.",
    "This field may not be blank.": "Bu maydon bo‘sh bo‘lishi mumkin emas.",
    "This field may not be null.": "Bu maydon bo‘sh bo‘lishi mumkin emas.",
    "This list may not be empty.": "Ro‘yxat bo‘sh bo‘lishi mumkin emas.",
    "A valid integer is required.": "Butun son kiriting.",
    "A valid number is required.": "Son kiriting.",
    "Must be a valid boolean.": "Qiymat true yoki false bo‘lishi kerak.",
    "Not a valid string.": "Matn kiriting.",
    "String value too large.": "Qiymat juda katta.",
    "Not found.": "Topilmadi.",
    "Invalid page.": "Sahifa topilmadi.",
    "Authentication credentials were not provided.": "Tizimga kirish talab qilinadi.",
    "Incorrect authentication credentials.": "Login yoki parol noto‘g‘ri",
    "You do not have permission to perform this action.": "Sizda bu amal uchun ruxsat yo‘q.",  # = MSG_NO_PERMISSION (V3 §1.2)
    "Given token not valid for any token type": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Token is invalid or expired": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Token is invalid": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Token is expired": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Token is blacklisted": "Sessiya muddati tugagan. Qaytadan kiring.",
    "User not found": "Foydalanuvchi topilmadi.",
    "User is inactive": "Hisobingiz bloklangan. Administratorga murojaat qiling.",
    "No active account found with the given credentials": "Login yoki parol noto‘g‘ri",
    # SimpleJWT refresh of a token whose user has been blocked.
    "No active account found for the given token.": "Hisobingiz bloklangan. Administratorga murojaat qiling.",
    "Token has no type": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Token has wrong type": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Token has no id": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Authorization header must contain two space-delimited values": "Avtorizatsiya sarlavhasi noto‘g‘ri.",
    "Token contained no recognizable user identification": "Sessiya muddati tugagan. Qaytadan kiring.",
    "Date has wrong format. Use one of these formats instead: YYYY-MM-DD.": "Sana noto‘g‘ri formatda (YYYY-MM-DD).",
    "Expected a date but got a datetime.": "Sana noto‘g‘ri formatda (YYYY-MM-DD).",
    "Enter a valid date.": "Sana noto‘g‘ri formatda (YYYY-MM-DD).",
}

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"No .+ matches the given query\."), "Topilmadi."),
    (re.compile(r"Token has no '.+' claim|Token '.+' claim has expired"), "Sessiya muddati tugagan. Qaytadan kiring."),
    (re.compile(r"Method \"(.+)\" not allowed\."), "“\\1” so‘rov usuli ruxsat etilmagan."),
    (re.compile(r"Unsupported media type .*"), "Faqat JSON (application/json) qabul qilinadi."),
    (re.compile(r"JSON parse error.*", re.S), "JSON formati noto‘g‘ri."),
    (re.compile(r"Invalid data\. Expected a dictionary, but got .+\."), "Noto‘g‘ri ma’lumot formati."),
    (re.compile(r"Request was throttled\. Expected available in (\d+) seconds?\."),
     "Juda ko‘p urinish. \\1 soniyadan keyin qayta urinib ko‘ring."),
    (re.compile(r"Request was throttled\."), "Juda ko‘p urinish. Birozdan keyin qayta urinib ko‘ring."),
    (re.compile(r"Invalid pk \".*\" - object does not exist\."), "Tanlangan obyekt topilmadi."),
    (re.compile(r"Incorrect type\. Expected pk value, received .+\."), "Noto‘g‘ri identifikator."),
    (re.compile(r"\"(.*)\" is not a valid choice\."), "“\\1” ruxsat etilgan qiymat emas."),
    (re.compile(r"Ensure this value is greater than or equal to (.+)\."), "Qiymat \\1 dan kichik bo‘lmasligi kerak."),
    (re.compile(r"Ensure this value is less than or equal to (.+)\."), "Qiymat \\1 dan katta bo‘lmasligi kerak."),
    (re.compile(r"Ensure this field has no more than (\d+) characters\."), "Ko‘pi bilan \\1 ta belgi kiriting."),
    (re.compile(r"Ensure this field has at least (\d+) characters\."), "Kamida \\1 ta belgi kiriting."),
    (re.compile(r"Ensure that there are no more than (\d+) digits in total\."), "Ko‘pi bilan \\1 ta raqam kiriting."),
    (re.compile(r"Ensure that there are no more than (\d+) decimal places\."), "Verguldan keyin ko‘pi bilan \\1 ta raqam."),
    (re.compile(r"Ensure that there are no more than (\d+) digits before the decimal point\."),
     "Butun qismda ko‘pi bilan \\1 ta raqam."),
    (re.compile(r".+ with this .+ already exists\."), "Bu qiymat allaqachon mavjud."),
    (re.compile(r"The fields .+ must make a unique set\."), "Bunday yozuv allaqachon mavjud."),
]


def translate_message(message: str) -> str:
    if message in _EXACT:
        return _EXACT[message]
    for pattern, replacement in _PATTERNS:
        if pattern.fullmatch(message):
            return pattern.sub(replacement, message)
    return message


def _translate(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: _translate(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_translate(item) for item in data]
    if isinstance(data, str):
        return translate_message(data)
    return data


def exception_handler(exc: Exception, context: dict[str, Any]):
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = _translate(response.data)
    return response


class UnauthorizedError(APIException):
    """Plain ``401`` (DRF downgrades ``AuthenticationFailed`` to 403 on views without authenticators)."""

    status_code = 401
    default_detail = "Login yoki parol noto‘g‘ri"
    default_code = "unauthorized"

