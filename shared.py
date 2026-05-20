"""
shared.py - Shared constants, fee logic, and utilities
Used by BAS server and BDB server.

Fee table (per assignment spec):
  $0        - $2,000.00   : 0%     (free tier)
  $2,000.01 - $10,000.00  : 0.25%, cap $20.00
  $10,000.01- $20,000.00  : 0.20%, cap $25.00
  $20,000.01- $50,000.00  : 0.125%, cap $40.00
  $50,000.01- $100,000.00 : 0.08%, cap $60.00
  $100,000.01+            : 0.06%, cap $200.00

All monetary values stored as integer CENTS to avoid floating point.
Rounding policy: round half-up to 2 decimal places.
"""

import math

# Pyro5 service names (used for name server registration)
BDB_SERVICE_NAME = "banking.bdb"
BAS_SERVICE_NAME = "banking.bas"

# Transfer statuses
STATUS_PENDING   = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED    = "FAILED"

# Fee table: (min_cents_exclusive, max_cents_inclusive, rate_pct, cap_cents)
# min=0 means >= 0, use exclusive lower bound for tiers above free
FEE_TIERS = [
    (         0,    200000, 0.0000, 0),       # $0 - $2,000.00     : free
    (    200000,  1000000, 0.0025, 2000),     # $2,000.01-$10k     : 0.25%, cap $20
    (   1000000,  2000000, 0.0020, 2500),     # $10k.01-$20k       : 0.20%, cap $25
    (   2000000,  5000000, 0.00125, 4000),    # $20k.01-$50k       : 0.125%, cap $40
    (   5000000, 10000000, 0.0008, 6000),     # $50k.01-$100k      : 0.08%, cap $60
    (  10000000,  None,    0.0006, 20000),    # $100k.01+          : 0.06%, cap $200
]


def round_half_up(value: float) -> int:
    """
    Round half-up to nearest cent.
    Returns integer cents.
    Input: float representing cents (e.g. 123.5 cents)
    """
    return math.floor(value + 0.5)


def calculate_fee_cents(amount_cents: int) -> int:
    """
    Calculate transfer fee in cents for a given amount in cents.
    Steps (per spec):
      1. Find tier by amount
      2. Compute raw fee (amount * rate)
      3. Round half-up
      4. Apply per-transfer cap
    Returns fee in cents (integer).
    """
    if amount_cents < 0:
        raise ValueError("Amount cannot be negative")

    for (lo, hi, rate, cap) in FEE_TIERS:
        if hi is None or amount_cents <= hi:
            if amount_cents > lo:
                raw_fee = amount_cents * rate          # float cents
                rounded_fee = round_half_up(raw_fee)   # integer cents
                return min(rounded_fee, cap)
            # Falls in free tier (amount_cents <= 200000 and lo==0)
            if lo == 0:
                raw_fee = amount_cents * rate
                rounded_fee = round_half_up(raw_fee)
                return min(rounded_fee, cap)

    # Fallback (should never reach here)
    return 0


def cents_to_str(cents: int) -> str:
    """Convert integer cents to dollar string e.g. 150025 -> '$1,500.25'"""
    dollars = cents // 100
    remainder = cents % 100
    return f"${dollars:,}.{remainder:02d}"


def parse_amount(amount_str: str) -> int:
    """
    Parse a dollar amount string entered by user into integer cents.
    Accepts: '1500', '1500.00', '1500.5', '1,500.00'
    Returns cents as int.
    Raises ValueError on invalid input.
    """
    cleaned = amount_str.replace(",", "").strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"Invalid amount: '{amount_str}'")
    if value < 0:
        raise ValueError("Amount cannot be negative")
    # Convert to cents using round-half-up
    return round_half_up(value * 100)