"""
shared.py - Shared constants, fee logic, and utilities used by BAS and BDB server
==========================================================================
this file is used to keep logic consistent across the system.
All monetary values stored as integer CENTS to avoid floating point.
Phone numbers are normalised to 10-digit Australian mobile format regardless of input
===========================================================================
Aaron Kalaji 10670705, CSI3344 Assignment 2
"""

import math
import re

# Pyro5 service names
BDB_SERVICE_NAME = "banking.bdb"
BAS_SERVICE_NAME = "banking.bas"
# statuses
STATUS_PENDING   = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED    = "FAILED"
# min_cents_exclusive, max_cents_inclusive, rate, cap_cents)
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
    convert input from user to 10-digit  mobile format.
    Accepts: 0412345678, 04 1234 5678, +61412345678, 61412345678, returns standard int
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
    """format numbers for display"""
    if len(phone) == 10:
        return f"{phone[:4]} {phone[4:7]} {phone[7:]}"
    return phone