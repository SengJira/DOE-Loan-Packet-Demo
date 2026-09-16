"""Normalisation helpers shared by the deterministic analysis nodes.

Comparisons must ignore cosmetic differences (case, whitespace, punctuation,
date formatting, currency symbols) so the demo does not raise false mismatches.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACES = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
)

_ADDRESS_ABBREVIATIONS = {
    "rd": "road",
    "st": "street",
    "ave": "avenue",
    "avn": "avenue",
    "soi": "soi",
    "bldg": "building",
    "apt": "apartment",
    "fl": "floor",
    "no": "number",
    "dist": "district",
    "bkk": "bangkok",
}

_COMPANY_SUFFIXES = {
    "co",
    "ltd",
    "limited",
    "plc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "public",
    "pcl",
    "llc",
}

_NAME_TITLES = {"mr", "mrs", "ms", "miss", "dr", "khun", "prof"}


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_text(value: Any) -> str:
    """Lowercase, strip accents/punctuation and collapse whitespace."""
    if value is None:
        return ""
    text = _strip_accents(str(value)).lower()
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalize_name(value: Any) -> str:
    """Normalise a person name: drop titles and sort tokens for order-independence."""
    tokens = [t for t in normalize_text(value).split() if t and t not in _NAME_TITLES]
    return " ".join(sorted(tokens))


def normalize_employer(value: Any) -> str:
    tokens = [t for t in normalize_text(value).split() if t and t not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


def normalize_address(value: Any) -> str:
    tokens = [_ADDRESS_ABBREVIATIONS.get(t, t) for t in normalize_text(value).split()]
    return " ".join(tokens)


def normalize_id_number(value: Any) -> str:
    if value is None:
        return ""
    return _NON_ALNUM.sub("", str(value)).lower()


def parse_date(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    iso_prefix = re.match(r"(\d{4}-\d{2}-\d{2})[T ]", text)
    if iso_prefix:
        text = iso_prefix.group(1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else normalize_text(value)


def to_float(value: Any) -> float | None:
    """Parse an amount written as a number or as a formatted string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return -amount if negative and amount > 0 else amount


def relative_difference(a: float | None, b: float | None) -> float | None:
    """Absolute difference as a percentage of the larger magnitude."""
    if a is None or b is None:
        return None
    scale = max(abs(a), abs(b))
    if scale == 0:
        return 0.0
    return abs(a - b) / scale * 100.0


def coefficient_of_variation(values: list) -> float | None:
    """Standard deviation over mean, expressed as a percentage."""
    numbers = [v for v in (to_float(x) for x in values) if v is not None]
    if len(numbers) < 2:
        return None
    mean = sum(numbers) / len(numbers)
    if mean == 0:
        return None
    variance = sum((n - mean) ** 2 for n in numbers) / len(numbers)
    return (variance ** 0.5) / abs(mean) * 100.0
