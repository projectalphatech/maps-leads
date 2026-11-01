import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


@dataclass
class PhoneResult:
    raw: str
    normalized: str | None
    is_valid: bool
    display: str | None = None


def _clean_raw(value: str) -> str:
    text = value.translate(ARABIC_DIGITS)
    text = re.sub(r"[^\d+]", "", text)
    return text.strip()


def normalize_egypt_phone(raw: str | None) -> PhoneResult:
    if not raw or not raw.strip():
        return PhoneResult(raw=raw or "", normalized=None, is_valid=False)

    cleaned = _clean_raw(raw)
    if not cleaned:
        return PhoneResult(raw=raw, normalized=None, is_valid=False)

    candidates = [cleaned]
    if cleaned.startswith("0"):
        candidates.append("+20" + cleaned[1:])
    if not cleaned.startswith("+"):
        candidates.append("+20" + cleaned.lstrip("0"))

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, "EG")
            if phonenumbers.is_valid_number(parsed):
                e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
                display = phonenumbers.format_number(parsed, PhoneNumberFormat.INTERNATIONAL)
                return PhoneResult(raw=raw, normalized=e164, is_valid=True, display=display)
        except NumberParseException:
            continue

    return PhoneResult(raw=raw, normalized=None, is_valid=False)
