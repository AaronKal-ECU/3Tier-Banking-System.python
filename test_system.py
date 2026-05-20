"""
test_system.py - banking system testing environment
===========================================================================
This file features 49 test cases that highlight the edge-case and
error handling of our proposed banking app.
to run, start the program and run the following in a new terminal:
python test_system.py
===========================================================================
Aaron Kalaji 10670705, CSI3344 Assignment 2
"""

import sys, uuid
import Pyro5.api
from shared import BAS_SERVICE_NAME, calculate_fee_cents, parse_amount, cents_to_str, normalise_phone

results = {"pass": 0, "fail": 0}

def get_bas():
    try:
        ns = Pyro5.api.locate_ns()
        uri = ns.lookup(BAS_SERVICE_NAME)
        return Pyro5.api.Proxy(uri)
    except Exception as e:
        print(f"[ERROR] Cannot connect: {e}")
        sys.exit(1)

def check(name, condition, detail=""):
    tag = "  [PASS]" if condition else "  [FAIL]"
    results["pass" if condition else "fail"] += 1
    print(f"{tag} {name}" + (f"\n         → {detail}" if detail else ""))

def section(title):
    print(f"\n{'─'*52}\n  {title}\n{'─'*52}")


# edge values for fee calculation
def test_fee():
    section("1. Fee Calculation")
    check("$0 free tier",           calculate_fee_cents(0) == 0)
    check("$2,000 free tier",       calculate_fee_cents(200000) == 0)
    check("$2,000.01 entry 0.25%",  calculate_fee_cents(200001) == 500,
          f"got {cents_to_str(calculate_fee_cents(200001))}")
    check("$10,000 entry cap $20",  calculate_fee_cents(1000000) == 2000,
          f"got {cents_to_str(calculate_fee_cents(1000000))}")
    check("$20,000 mid cap $25",    calculate_fee_cents(2000000) == 2500)
    check("$50,000 upper-mid cap $40", calculate_fee_cents(5000000) == 4000)
    check("$100,000 high cap $60",  calculate_fee_cents(10000000) == 6000)
    check("$1,000,000 top cap $200",calculate_fee_cents(100000000) == 20000)

    check("parse '1500'",    parse_amount("1500") == 150000)
    check("parse '$10.50'",  parse_amount("$10.50") == 1050)
    check("parse '1,500.00'",parse_amount("1,500.00") == 150000)
    try:
        parse_amount("abc")
        check("parse 'abc' raises", False)
    except ValueError:
        check("parse 'abc' raises ValueError", True)

    check("normalise 0412345678",    normalise_phone("0412345678") == "0412345678")
    check("normalise +61412345678",  normalise_phone("+61412345678") == "0412345678")
    check("normalise 0412 345 678",  normalise_phone("0412 345 678") == "0412345678")
    try:
        normalise_phone("0312345678")
        check("landline rejected", False)
    except ValueError:
        check("landline rejected", True)


# login edge cases

def test_login():
    section("2. Login / Authentication")
    with get_bas() as bas:
        r = bas.login("alice", "alice123")
        check("Valid login alice", r["success"], r.get("phone_number"))
        alice_token = r.get("token")

        check("Phone returned on login", "phone_number" in r)

        r = bas.login("alice", "wrong")
        check("Wrong password rejected", not r["success"])

        r = bas.login("nobody", "x")
        check("Unknown user rejected", not r["success"])

        r = bas.login("", "alice123")
        check("Empty username rejected", not r["success"])

        bas.logout(alice_token)
        r = bas.get_balance(alice_token)
        check("Token invalid after logout", not r["success"])

        r = bas.login("bob", "bob123")
        check("Valid login bob", r["success"])
        return r.get("token")


# balance

def test_balance(token):
    section("3. Balance Query")
    with get_bas() as bas:
        r = bas.get_balance(token)
        check("Balance query succeeds", r["success"], r.get("balance_display"))
        check("Balance has phone", "phone_number" in r)
        check("No token rejected", not bas.get_balance("")["success"])
        check("Fake token rejected", not bas.get_balance("fake-xxx")["success"])


# PayID

def test_payid(alice_token):
    section("4. PayID Phone Lookup")
    with get_bas() as bas:
        # Valid lookup
        r = bas.lookup_payid(alice_token, "0422222222")   # bob's number
        check("Lookup bob by phone", r["success"], r.get("masked_name"))
        check("Masked name format correct",
              r.get("masked_name","").endswith("."),
              r.get("masked_name"))

        # Non-existent phone
        r = bas.lookup_payid(alice_token, "0499999999")
        check("Unknown phone rejected", not r["success"])

        # Own phone
        r = bas.lookup_payid(alice_token, "0411111111")
        check("Self-lookup rejected", not r["success"])

        # Invalid format (landline)
        r = bas.lookup_payid(alice_token, "0812345678")
        check("Landline number rejected", not r["success"])

        # Invalid non-numeric
        r = bas.lookup_payid(alice_token, "not-a-number")
        check("Non-numeric phone rejected", not r["success"])


# transfers

def test_transfers():
    section("5. Transfer Submission")
    with get_bas() as bas:
        alice = bas.login("alice", "alice123")
        carol = bas.login("carol", "carol123")
        alice_token = alice["token"]
        carol_token = carol["token"]

        # Valid transfer alice -> bob ($100)
        r = bas.submit_transfer(alice_token, "0422222222", 10000,
                                "Lunch", str(uuid.uuid4()))
        check("Valid $100 transfer", r["success"],
              f"{r.get('transfer_id')} {r.get('status')}")
        txn_id = r.get("transfer_id")

        # Non-existent phone
        r = bas.submit_transfer(alice_token, "0499999999", 10000,
                                "", str(uuid.uuid4()))
        check("Transfer to unknown phone rejected", not r["success"])

        # Self-transfer
        r = bas.submit_transfer(alice_token, "0411111111", 10000,
                                "", str(uuid.uuid4()))
        check("Self-transfer rejected", not r["success"])

        # Zero amount
        r = bas.submit_transfer(alice_token, "0422222222", 0,
                                "", str(uuid.uuid4()))
        check("Zero amount rejected", not r["success"])

        # Negative amount
        r = bas.submit_transfer(alice_token, "0422222222", -100,
                                "", str(uuid.uuid4()))
        check("Negative amount rejected", not r["success"])

        # Insufficient funds (carol has $5,000 → try $6,000)
        r = bas.submit_transfer(carol_token, "0422222222", 600000,
                                "", str(uuid.uuid4()))
        check("Insufficient funds rejected",
              not r["success"] or r.get("status") == "FAILED",
              r.get("error", r.get("status")))

        # Idempotency: same key → same result, no double debit
        key = str(uuid.uuid4())
        r1 = bas.submit_transfer(alice_token, "0422222222", 5000, "idem", key)
        r2 = bas.submit_transfer(alice_token, "0422222222", 5000, "idem", key)
        check("Idempotency: same transfer_id returned",
              r1.get("transfer_id") == r2.get("transfer_id"))
        check("Idempotency: duplicate flagged", r2.get("duplicate") == True)

        # Fee boundary: $2,000.00 (free) vs $2,000.01 (entry)
        r = bas.submit_transfer(alice_token, "0422222222", 200000,
                                "tier boundary", str(uuid.uuid4()))
        check("$2,000 transfer (free tier fee=$0)",
              r["success"] and r.get("fee_display") == "$0.00",
              r.get("fee_display"))

        r = bas.submit_transfer(alice_token, "0422222222", 200001,
                                "entry tier", str(uuid.uuid4()))
        check("$2,000.01 transfer (entry tier fee>$0)",
              r["success"] and r.get("fee_display") != "$0.00",
              r.get("fee_display"))

        return txn_id


# ── 6. Transfer Status ───────────────────────────────────────────────

def test_status(txn_id):
    section("6. Transfer Status")
    with get_bas() as bas:
        alice = bas.login("alice", "alice123")
        token = alice["token"]

        r = bas.get_transfer_status(token, txn_id)
        check("Status query succeeds", r["success"], r.get("status"))
        check("Status is COMPLETED", r.get("status") == "COMPLETED")
        check("Phone displayed in status", "receiver_phone_display" in r)

        r = bas.get_transfer_status(token, "TXN000000000000")
        check("Unknown transfer ID returns error", not r["success"])


# ── 7. History ───────────────────────────────────────────────────────

def test_history():
    section("7. Transfer History")
    with get_bas() as bas:
        token = bas.login("alice", "alice123")["token"]
        r = bas.get_transfer_history(token)
        check("History succeeds", r["success"])
        check("History is a list", isinstance(r.get("transfers"), list),
              f"count={len(r.get('transfers',[]))}")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  CSI3344 Banking System — Test Suite")
    print("=" * 52)
    with get_bas() as bas:
        print(f"  BAS: {bas.ping()}\n")

    test_fee()
    bob_token   = test_login()
    test_balance(bob_token)

    with get_bas() as bas:
        alice_token = bas.login("alice", "alice123")["token"]

    test_payid(alice_token)
    txn_id = test_transfers()
    if txn_id:
        test_status(txn_id)
    test_history()

    total = results["pass"] + results["fail"]
    print(f"\n{'═'*52}")
    print(f"  Results: {results['pass']}/{total} passed, {results['fail']} failed")
    print(f"{'═'*52}\n")
    if results["fail"]:
        sys.exit(1)

if __name__ == "__main__":
    main()