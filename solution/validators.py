import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def validate_phone(raw: str) -> str | None:
    """
    Indian mobile number: strip non-digits; if 12 digits starting with 91 drop 91;
    if 11 digits starting with 0 drop 0. Valid iff 10 digits starting with 6,7,8,9.
    Returns '+91XXXXXXXXXX' or None.
    """
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in {"6", "7", "8", "9"}:
        return f"+91{digits}"
    return None


def validate_date(raw: str) -> str | None:
    """
    Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, and DD-Mon-YYYY (day-first).
    Rejects impossible dates like 31/02. Returns 'YYYY-MM-DD' or None.
    """
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def validate_decimal(raw: str) -> str | None:
    """
    Optionally prefixed by ₹, Rs, Rs., or INR (case-insensitive, optional space).
    Commas are thousands separators. Must be >= 0. Rounded to 2 decimal places.
    """
    s = raw.strip()
    s = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE)
    s = s.replace(",", "").strip()
    if not s:
        return None
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    if not val.is_finite() or val < Decimal("0"):
        return None
    rounded = val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(rounded)


def validate_enum(raw: str, allowed_values: list[str]) -> str | None:
    """Compared case-insensitively against allowed values; stored lower-case."""
    low = raw.strip().lower()
    allowed_lower = {v.lower() for v in allowed_values}
    return low if low in allowed_lower else None


def parse_field_value(raw: str, field_spec: dict[str, Any]) -> tuple[bool, Any]:
    """Returns (is_valid, normalized_value)."""
    ftype = field_spec["type"]
    if ftype == "string":
        return True, raw.strip()
    if ftype == "phone":
        res = validate_phone(raw)
        return (res is not None), res
    if ftype == "date":
        res = validate_date(raw)
        return (res is not None), res
    if ftype == "decimal":
        res = validate_decimal(raw)
        return (res is not None), res
    if ftype == "enum":
        res = validate_enum(raw, field_spec.get("values", []))
        return (res is not None), res
    return True, raw.strip()