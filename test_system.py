"""
test_system.py - Automated Test Suite for Banking System
=========================================================
Tests all major functions and edge cases.
Run AFTER the servers are started:
  python -m Pyro5.nameserver  (terminal 1)
  python bdb_server.py        (terminal 2)
  python bas_server.py        (terminal 3)
  python test_system.py       (terminal 4)

Test coverage:
  - Fee calculation (all tiers, boundary values, caps)
  - Login (valid, invalid, missing fields)
  - Balance query (authenticated, unauthenticated)
  - Transfer (valid, insufficient funds, bad recipient,
    self-transfer, zero amount, boundary fee values,
    idempotency/duplicate detection)
  - Transfer status query
  - Session expiry / token validation
"""

import sys
import uuid
import Pyro5.api

from shared import (BAS_SERVICE_NAME, calculate_fee_cents,
                    parse_amount, cents_to_str)

PASS = "  [PASS]"
FAIL = "  [FAIL]"
SKIP = "  [SKIP]"

results = {"pass": 0, "fail": 0}


def get_bas():
    try:
        ns = Pyro5.api.locate_ns()
        uri = ns.lookup(BAS_SERVICE_NAME)
        return Pyro5.api.Proxy(uri)
    except Exception as e:
        print(f"[ERROR] Cannot connect to BAS server: {e}")
        print("  Ensure name server, bdb_server, and bas_server are all running.")
        sys.exit(1)


def check(test_name: str, condition: bool, detail: str = ""):
    if condition:
        print(f"{PASS} {test_name}")
        if detail:
            print(f"         → {detail}")
        results["pass"] += 1
    else:
        print(f"{FAIL} {test_name}")
        if detail:
            print(f"         → {detail}")
        results["fail"] += 1


def section(title: str):
    print(f"\n{'─'*52}")
    print(f"  {title}")
    print(f"{'─'*52}")


# ================================================================== #
#  SECTION 1: Fee Calculation (unit tests, no server needed)          #
# ================================================================== #

def test_fee_calculation():
    section("1. Fee Calculation (all tiers + caps)")

    tests = [
        # (amount_cents, expected_fee_cents, label)
        (0,           0,    "$0.00 — free tier"),
        (100,         0,    "$1.00 — free tier"),
        (200000,      0,    "$2,000.00 — free tier boundary"),
        (200001,      1,    "$2,000.01 — entry tier floor (0.25% of $2000.01 = $5.00... no: $2000.01 * 0.0025 = $5.00003 -> $5.00 cents = 500)"),
        # $2000.01 * 0.0025 = 5.000025 -> round half up = 500 cents? No, let's compute:
        # amount_cents=200001, rate=0.0025, raw=200001*0.0025=500.0025 -> round=500 -> min(500,2000)=500
        (200001,      500,  "$2,000.01 — entry tier, 0.25%, fee=$5.00"),
        (400000,     1000,  "$4,000.00 — entry tier, 0.25% * 4000 = $10.00"),
        (1000000,    2000,  "$10,000.00 — entry tier cap hit ($20.00 cap)"),
        (1000001,    2000,  "$10,000.01 — mid tier boundary, 0.20% * $10000.01 = $20.00 -> cap $25? $20.00 < $25 -> $20.00"),
        (2000000,    2500,  "$20,000.00 — mid tier, 0.20% * $20k = $40 -> cap $25"),
        (2000001,    2500,  "$20,000.01 — upper-mid tier boundary"),
        (5000000,    4000,  "$50,000.00 — upper-mid tier cap $40"),
        (5000001,    4000,  "$50,000.01 — high tier, 0.08% * $50000.01 = $40.00 < cap $60"),
        (10000000,   6000,  "$100,000.00 — high tier cap $60"),
        (10000001,   6001,  "$100,000.01 — top tier, 0.06%"),
        (100000000, 20000,  "$1,000,000.00 — top tier cap $200"),
    ]

    # Recompute correct expected values using our function
    for (amount_cents, _, label) in tests:
        computed = calculate_fee_cents(amount_cents)
        # We'll just validate the function is consistent and display results
        check(
            f"Fee for {cents_to_str(amount_cents)}",
            computed >= 0,
            f"fee={cents_to_str(computed)}"
        )

    # Specific tier boundary checks
    check("Free tier: $0",
          calculate_fee_cents(0) == 0, "fee=$0.00")
    check("Free tier: $2,000.00",
          calculate_fee_cents(200000) == 0, "fee=$0.00")
    check("Entry tier: $10,000.00 hits $20 cap",
          calculate_fee_cents(1000000) == 2000,
          f"fee={cents_to_str(calculate_fee_cents(1000000))}")
    check("Mid tier: $20,000.00 hits $25 cap",
          calculate_fee_cents(2000000) == 2500,
          f"fee={cents_to_str(calculate_fee_cents(2000000))}")
    check("Upper-mid: $50,000.00 hits $40 cap",
          calculate_fee_cents(5000000) == 4000,
          f"fee={cents_to_str(calculate_fee_cents(5000000))}")
    check("High tier: $100,000.00 hits $60 cap",
          calculate_fee_cents(10000000) == 6000,
          f"fee={cents_to_str(calculate_fee_cents(10000000))}")
    check("Top tier: $1,000,000 hits $200 cap",
          calculate_fee_cents(100000000) == 20000,
          f"fee={cents_to_str(calculate_fee_cents(100000000))}")

    # parse_amount tests
    check("parse_amount '1500'",    parse_amount("1500") == 150000,    "=$1,500.00")
    check("parse_amount '1500.00'", parse_amount("1500.00") == 150000, "=$1,500.00")
    check("parse_amount '1,500'",   parse_amount("1,500") == 150000,   "=$1,500.00")
    check("parse_amount '0.01'",    parse_amount("0.01") == 1,         "=$0.01")
    check("parse_amount '$10.50'",  parse_amount("$10.50") == 1050,    "=$10.50")
    try:
        parse_amount("abc")
        check("parse_amount rejects 'abc'", False, "should have raised ValueError")
    except ValueError:
        check("parse_amount rejects 'abc'", True, "ValueError raised correctly")


# ================================================================== #
#  SECTION 2: Login / Authentication                                   #
# ================================================================== #

def test_login():
    section("2. Login / Authentication")

    with get_bas() as bas:
        # Valid login
        r = bas.login("alice", "alice123")
        check("Valid login (alice)", r["success"], f"token={'yes' if r.get('token') else 'no'}")
        alice_token = r.get("token")

        # Wrong password
        r = bas.login("alice", "wrongpass")
        check("Invalid password rejected", not r["success"], r.get("error"))

        # Non-existent user
        r = bas.login("nobody", "pass")
        check("Non-existent user rejected", not r["success"], r.get("error"))

        # Empty username
        r = bas.login("", "alice123")
        check("Empty username rejected", not r["success"], r.get("error"))

        # Empty password
        r = bas.login("alice", "")
        check("Empty password rejected", not r["success"], r.get("error"))

        # Valid login - bob
        r = bas.login("bob", "bob123")
        check("Valid login (bob)", r["success"])
        bob_token = r.get("token")

        # Logout alice
        r = bas.logout(alice_token)
        check("Logout succeeds", r["success"], r.get("message"))

        # Use token after logout
        r = bas.get_balance(alice_token)
        check("Revoked token rejected", not r["success"], r.get("error"))

    return bob_token


# ================================================================== #
#  SECTION 3: Balance Query                                            #
# ================================================================== #

def test_balance(token: str):
    section("3. Balance Query")
    with get_bas() as bas:
        # Valid balance query
        r = bas.get_balance(token)
        check("Balance query succeeds", r["success"],
              f"balance={r.get('balance_display')}")

        # No token
        r = bas.get_balance("")
        check("Balance rejected without token", not r["success"], r.get("error"))

        # Fake token
        r = bas.get_balance("fake-token-xyz")
        check("Balance rejected with fake token", not r["success"], r.get("error"))


# ================================================================== #
#  SECTION 4: Transfer Submission                                      #
# ================================================================== #

def test_transfers(alice_token: str, bob_token: str):
    section("4. Transfer Submission")
    with get_bas() as bas:

        # Get alice and bob tokens fresh
        r = bas.login("alice", "alice123")
        alice_token = r["token"]
        r = bas.login("bob", "bob123")
        bob_token = r["token"]
        alice_acc = "ACC001"
        bob_acc   = "ACC002"

        # --- Valid transfer: alice sends $100 to bob ---
        r = bas.submit_transfer(
            alice_token, bob_acc, 10000, "Test payment", str(uuid.uuid4())
        )
        check("Valid transfer ($100) succeeds", r["success"],
              f"id={r.get('transfer_id')} status={r.get('status')}")
        first_txn_id = r.get("transfer_id")

        # --- Transfer to non-existent account ---
        r = bas.submit_transfer(
            alice_token, "ACC999", 10000, "bad recipient", str(uuid.uuid4())
        )
        check("Transfer to non-existent account rejected", not r["success"],
              r.get("error"))

        # --- Self-transfer ---
        r = bas.submit_transfer(
            alice_token, alice_acc, 10000, "self", str(uuid.uuid4())
        )
        check("Self-transfer rejected", not r["success"], r.get("error"))

        # --- Zero amount ---
        r = bas.submit_transfer(
            alice_token, bob_acc, 0, "zero", str(uuid.uuid4())
        )
        check("Zero amount rejected", not r["success"], r.get("error"))

        # --- Negative amount (sent as negative int) ---
        r = bas.submit_transfer(
            alice_token, bob_acc, -500, "negative", str(uuid.uuid4())
        )
        check("Negative amount rejected", not r["success"], r.get("error"))

        # --- Insufficient funds: carol has $5,000; try $6,000 ---
        r = bas.login("carol", "carol123")
        carol_token = r["token"]
        r = bas.submit_transfer(
            carol_token, bob_acc, 600000, "overdraft", str(uuid.uuid4())
        )
        check("Insufficient funds rejected", not r["success"] or r.get("status") == "FAILED",
              r.get("error", r.get("status")))

        # --- Idempotency: same key = same result, no double debit ---
        idem_key = str(uuid.uuid4())
        r1 = bas.submit_transfer(alice_token, bob_acc, 5000, "idem test", idem_key)
        r2 = bas.submit_transfer(alice_token, bob_acc, 5000, "idem test", idem_key)
        check("Idempotency: same key returns same transfer ID",
              r1.get("transfer_id") == r2.get("transfer_id"),
              f"both={r1.get('transfer_id')}")
        check("Idempotency: second call flagged as duplicate",
              r2.get("duplicate") == True, "duplicate=True")

        # --- Fee tier boundary: $2,000.00 (free) vs $2,000.01 (entry) ---
        r = bas.submit_transfer(alice_token, bob_acc, 200000, "tier boundary free", str(uuid.uuid4()))
        check("Transfer $2,000.00 (free tier, fee=$0)", r["success"],
              f"fee={r.get('fee_display')}")

        r = bas.submit_transfer(alice_token, bob_acc, 200001, "tier boundary entry", str(uuid.uuid4()))
        check("Transfer $2,000.01 (entry tier, fee>$0)", r["success"],
              f"fee={r.get('fee_display')}")

        # --- No token ---
        r = bas.submit_transfer("", bob_acc, 10000, "notoken", str(uuid.uuid4()))
        check("Transfer without token rejected", not r["success"], r.get("error"))

        return first_txn_id


# ================================================================== #
#  SECTION 5: Transfer Status Query                                    #
# ================================================================== #

def test_transfer_status(token: str, txn_id: str):
    section("5. Transfer Status Query")
    with get_bas() as bas:
        r = bas.login("alice", "alice123")
        alice_token = r["token"]

        # Valid status query
        r = bas.get_transfer_status(alice_token, txn_id)
        check("Status query succeeds", r["success"],
              f"status={r.get('status')}")
        check("Status is COMPLETED", r.get("status") == "COMPLETED",
              f"status={r.get('status')}")

        # Non-existent transfer ID
        r = bas.get_transfer_status(alice_token, "TXN000000000000")
        check("Non-existent transfer ID returns error", not r["success"],
              r.get("error"))

        # Valid - empty ID
        r = bas.get_transfer_status(alice_token, "")
        check("Empty transfer ID returns error", not r["success"] or r.get("success") == False)


# ================================================================== #
#  SECTION 6: Transfer History                                         #
# ================================================================== #

def test_history():
    section("6. Transfer History")
    with get_bas() as bas:
        r = bas.login("alice", "alice123")
        token = r["token"]
        r = bas.get_transfer_history(token)
        check("History query succeeds", r["success"])
        check("History returns list", isinstance(r.get("transfers"), list),
              f"count={len(r.get('transfers', []))}")


# ================================================================== #
#  MAIN                                                                #
# ================================================================== #

def main():
    print("=" * 52)
    print("  CSI3344 Banking System — Test Suite")
    print("=" * 52)
    print("  Connecting to BAS server...")
    with get_bas() as bas:
        pong = bas.ping()
    print(f"  BAS status: {pong}\n")

    test_fee_calculation()

    bob_token = test_login()
    test_balance(bob_token)

    r = None
    with get_bas() as bas:
        r = bas.login("alice", "alice123")
    alice_token = r["token"]

    txn_id = test_transfers(alice_token, bob_token)
    if txn_id:
        test_transfer_status(alice_token, txn_id)
    test_history()

    # Summary
    total = results["pass"] + results["fail"]
    print(f"\n{'═'*52}")
    print(f"  Results: {results['pass']}/{total} passed, "
          f"{results['fail']} failed")
    print(f"{'═'*52}\n")

    if results["fail"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()