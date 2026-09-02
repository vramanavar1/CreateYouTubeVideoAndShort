"""PII detectors, kept separate from the policy that acts on them.

The split matters. These functions only answer "what is this?" -- they never
decide what to do about it. ``PiiStage`` owns that decision, driven by
``YTSHORT_PII_POLICY``. Mixing the two is how detectors end up quietly tuned to
whatever the current policy happens to want.

Every detector is checksum-validated where the format allows it (Luhn for cards,
Verhoeff for Aadhaar). Pattern-only matching on 12- and 16-digit runs produces so
many false positives that a reviewer learns to ignore the findings, which is
worse than not having them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
# International-ish: optional +CC, then 9-14 digits with common separators.
PHONE_RE = re.compile(r"(?<![\w])(?:\+\d{1,3}[\s.-]?)?(?:\d[\s.-]?){9,14}\d(?![\w])")
CARD_RE = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
# Indian PAN: five letters, four digits, one letter.
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
AADHAAR_RE = re.compile(r"(?<![\d])\d{4}[\s-]?\d{4}[\s-]?\d{4}(?![\d])")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

#: Words that make a nearby number far more likely to be a real postal address.
ADDRESS_HINTS = re.compile(
    r"\b(street|st\.|road|rd\.|avenue|ave\.|lane|apartment|apt\.|flat|suite|"
    r"block|sector|pincode|postcode|zip\s?code)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Detection:
    kind: str
    value: str
    redacted: str
    confidence: str  # "high" when checksum-validated, "medium" otherwise


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def mask(value: str, keep: int = 2) -> str:
    """Mask a value for display. Findings are read by humans; never echo the PII."""
    digits_only = _digits(value)
    if len(digits_only) >= 4 and digits_only == value.replace(" ", "").replace("-", ""):
        return f"{'*' * max(0, len(digits_only) - keep)}{digits_only[-keep:]}"
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:1]
        return f"{head}{'*' * max(1, len(local) - 1)}@{domain}"
    return value[:keep] + "*" * max(0, len(value) - keep)


def luhn_valid(number: str) -> bool:
    digits = [int(d) for d in _digits(number)]
    if len(digits) < 13:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


# Verhoeff tables -- the checksum Aadhaar numbers actually use.
_D_TABLE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P_TABLE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(number: str) -> bool:
    digits = _digits(number)
    if len(digits) != 12:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        checksum = _D_TABLE[checksum][_P_TABLE[index % 8][int(digit)]]
    return checksum == 0


def detect(text: str) -> list[Detection]:
    """Find candidate PII in a block of text (OCR output, subject, or body)."""
    if not text or not text.strip():
        return []

    found: list[Detection] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, confidence: str) -> None:
        key = (kind, _digits(value) or value)
        if key in seen:
            return
        seen.add(key)
        found.append(
            Detection(kind=kind, value=value, redacted=mask(value), confidence=confidence)
        )

    for match in EMAIL_RE.finditer(text):
        add("email", match.group(), "high")

    for match in PAN_RE.finditer(text):
        add("pan", match.group(), "high")

    for match in IBAN_RE.finditer(text):
        add("iban", match.group(), "medium")

    # Cards before phones: a 16-digit run matches both patterns, and a
    # Luhn-valid one is a card, not a phone number.
    card_digits: set[str] = set()
    for match in CARD_RE.finditer(text):
        candidate = match.group()
        if luhn_valid(candidate):
            card_digits.add(_digits(candidate))
            add("payment_card", candidate, "high")

    for match in AADHAAR_RE.finditer(text):
        candidate = match.group()
        if _digits(candidate) in card_digits:
            continue
        if verhoeff_valid(candidate):
            add("aadhaar", candidate, "high")

    for match in PHONE_RE.finditer(text):
        candidate = match.group()
        digits = _digits(candidate)
        if digits in card_digits or len(digits) > 15:
            continue
        add("phone", candidate, "medium")

    if ADDRESS_HINTS.search(text):
        hint = ADDRESS_HINTS.search(text)
        assert hint is not None
        add("postal_address_hint", hint.group(), "medium")

    return found
