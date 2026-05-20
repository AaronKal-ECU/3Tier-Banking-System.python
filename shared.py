"""
shared.py - Shared constants, fee logic, and utilities used by BAS and BDB server
==========================================================================
All monetary values stored as integer CENTS to avoid floating point.
Phone numbers are normalised to 10-digit Australian mobile format.
===========================================================================
INSTRUCTIONS
to run the program, place all 6 python files in one location, then open 4 terminal
windows at the location. paste each command in each terminal in the following order:
- python -m Pyro5.nameserver
- python bdb_server.py
- python bas_server.py
once all three are running in separate terminals, execute the final one for the UI:
python bas_server.py
===========================================================================
Aaron Kalaji 10670705, CSI3344 Assignment 2
"""

import math
import re

# Pyro5 service names
BDB_SERVICE_NAME = "banking.bdb"
BAS_SERVICE_NAME = "banking.bas"

# Transfer statuses
STATUS_PENDING   = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED    = "FAILED"

# Fee table: (min_cents_exclusive, max_cents_inclusive, rate, cap_cents)
FEE_TIERS = [
    (        0,    200000, 0.0000,  0),
    (   200000,  1000000, 0.0025, 2000),
    (  1000000,  2000000, 0.0020, 2500),
    (  2000000,  5000000, 0.00125, 4000),
    (  5000000, 10000000, 0.0008, 6000),
    ( 10000000,     None, 0.0006, 20000),
]


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def calculate_fee_cents(amount_cents: int) -> int:
    if amount_cents < 0:
        raise ValueError("Amount cannot be negative")
    for (lo, hi, rate, cap) in FEE_TIERS:
        in_tier = (amount_cents > lo) if lo > 0 else True
        under   = (amount_cents <= hi) if hi is not None else True
        if in_tier and under:
            return min(round_half_up(amount_cents * rate), cap)
    return 0


def cents_to_str(cents: int) -> str:
    dollars   = cents // 100
    remainder = cents % 100
    return f"${dollars:,}.{remainder:02d}"


def parse_amount(amount_str: str) -> int:
    cleaned = amount_str.replace(",", "").strip().lstrip("$")
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"Invalid amount: '{amount_str}'")
    if value < 0:
        raise ValueError("Amount cannot be negative")
    return round_half_up(value * 100)


def normalise_phone(raw: str) -> str:
    """
    Normalise a phone number to 10-digit Australian mobile format.
    Accepts:  0412345678  |  04 1234 5678  |  +61412345678  |  61412345678
    Returns:  '0412345678'
    Raises ValueError if not a valid Australian mobile.
    """
    digits = re.sub(r"\D", "", raw)          # strip everything non-digit
    if digits.startswith("61"):
        digits = "0" + digits[2:]            # +61 4xx -> 04xx
    if not re.fullmatch(r"04\d{8}", digits):
        raise ValueError(
            f"'{raw}' is not a valid Australian mobile number. "
            "Use format: 0412 345 678"
        )
    return digits


def format_phone_display(phone: str) -> str:
    """Format 0412345678 -> 0412 345 678 for display."""
    if len(phone) == 10:
        return f"{phone[:4]} {phone[4:7]} {phone[7:]}"
    return phone