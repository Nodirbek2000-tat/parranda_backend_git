"""DRF exception handler: keeps DRF's error shape ({"detail": ...} or field errors) in Uzbek."""

import re

from rest_framework.views import exception_handler as drf_exception_handler

SESSION_EXPIRED = "Sessiya muddati tugagan. Qaytadan kiring."

_EXACT = {
    "This field is required.": "Bu maydon to‘ldirilishi shart.",
    "This field may not be blank.": "Bu maydon bo‘sh bo‘lishi mumkin emas.",
    "This field may not be null.": "Bu maydon bo‘sh bo‘lishi mumkin emas.",
    "This list may not be empty.": "Ro‘yxat bo‘sh bo‘lishi mumkin emas.",
    "A valid integer is required.": "Butun son kiriting.",
    "A valid number is required.": "Son kiriting.",
    "Must be a valid boolean.": "Qiymat true yoki false bo‘lishi kerak.",
    "Not a valid string.": "Matn kiriting.",
    "Not found.": "Topilmadi.",
    "No Case matches the given query.": "Topilmadi.",
    "Authentication credentials were not provided.": "Tizimga kirish talab qilinadi.",
    "Incorrect authentication credentials.": "Login yoki parol noto‘g‘ri",
    "You do not have permission to perform this action.": "Sizda bu amal uchun ruxsat yo‘q.",
    "Given token not valid for any token type": SESSION_EXPIRED,
    "Token is invalid or expired": SESSION_EXPIRED,
    "Token is invalid": SESSION_EXPIRED,
    "Token is expired": SESSION_EXPIRED,
    "Token is blacklisted": SESSION_EXPIRED,
    "User not found": "Foydalanuvchi topilmadi.",
    "User is inactive": "Hisobingiz bloklangan. Administratorga murojaat qiling.",
    "Upload a valid image. The file you uploaded was either not an image or a corrupted image.":
        "Rasm fayli noto‘g‘ri yoki buzilgan.",
    "No file was submitted.": "Fayl yuborilmadi.",
    "The submitted file is empty.": "Fayl bo‘sh.",
    "Datetime has wrong format. Use one of these formats instead: "
    "YYYY-MM-DDThh:mm[:ss[.uuuuuu]][+HH:MM|-HH:MM|Z].": "Vaqt noto‘g‘ri formatda (ISO 8601).",
}

_PATTERNS = [
    (re.compile(r"No .+ matches the given query\."), "Topilmadi."),
    (re.compile(r'Method "(.+)" not allowed\.'), "“\\1” so‘rov usuli ruxsat etilmagan."),
    (re.compile(r"Unsupported media type .*"), "So‘rov formati qo‘llab-quvvatlanmaydi."),
    (re.compile(r"JSON parse error.*", re.S), "JSON formati noto‘g‘ri."),
    (re.compile(r"Request was throttled.*"), "Juda ko‘p urinish. Birozdan keyin qayta urinib ko‘ring."),
    (re.compile(r'"(.*)" is not a valid choice\.'), "“\\1” ruxsat etilgan qiymat emas."),
    (re.compile(r"Ensure this value is greater than or equal to (.+)\."), "Qiymat \\1 dan kichik bo‘lmasligi kerak."),
    (re.compile(r"Ensure this value is less than or equal to (.+)\."), "Qiymat \\1 dan katta bo‘lmasligi kerak."),
    (re.compile(r"Ensure this field has no more than (\d+) characters\."), "Ko‘pi bilan \\1 ta belgi kiriting."),
    (re.compile(r"Ensure that there are no more than .+"), "Son juda katta yoki noto‘g‘ri."),
    (re.compile(r'Expected a list of items but got type ".+"\.'), "Ro‘yxat kutilgan."),
    (re.compile(r".+ with this .+ already exists\."), "Bu qiymat allaqachon mavjud."),
    (re.compile(r"Token .*"), SESSION_EXPIRED),
]


def translate(message: str) -> str:
    if message in _EXACT:
        return _EXACT[message]
    for pattern, replacement in _PATTERNS:
        if pattern.fullmatch(message):
            return pattern.sub(replacement, message)
    return message


def _walk(data):
    if isinstance(data, dict):
        return {key: _walk(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_walk(item) for item in data]
    if isinstance(data, str):
        return translate(data)
    return data


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        data = response.data
        # SimpleJWT adds "code"/"messages" noise to token errors: keep only the detail.
        if isinstance(data, dict) and "messages" in data:
            data = {"detail": data.get("detail", SESSION_EXPIRED)}
        response.data = _walk(data)
    return response
