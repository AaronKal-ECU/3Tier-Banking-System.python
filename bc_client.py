"""
bc_client.py, Banking Client, client tier of banking system
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

import sys
import uuid
import Pyro5.api

from shared import BAS_SERVICE_NAME, parse_amount, cents_to_str, format_phone_display

DIVIDER = "─" * 52
HEADER  = "═" * 52

def get_bas():
    try:
        ns  = Pyro5.api.locate_ns()
        uri = ns.lookup(BAS_SERVICE_NAME)
        return Pyro5.api.Proxy(uri)
    except Exception as e:
        print(f"\n[ERROR] Cannot connect to BAS server: {e}")
        print("  Start in order:")
        print("    1. python -m Pyro5.nameserver")
        print("    2. python bdb_server.py")
        print("    3. python bas_server.py")
        sys.exit(1)

def print_header(title):
    print(f"\n{HEADER}\n  {title}\n{HEADER}")

def print_section(title):
    print(f"\n{DIVIDER}\n  {title}\n{DIVIDER}")

def print_error(msg):
    print(f"\n  [!] {msg}")

def print_ok(msg):
    print(f"\n  [✓] {msg}")

def press_enter():
    input("\n  Press Enter to continue...")


# login
def screen_login():
    print_header("Banking System: Sign In")
    print("  testing accounts:  alice/alice123  ·  bob/bob123")
    print("                     carol/carol123  ·  dave/dave123\n")
    username = input("  Username: ").strip()
    if not username:
        return None, None, None
    password = input("  Password: ").strip()

    with get_bas() as bas:
        r = bas.login(username, password)

    if r["success"]:
        print_ok(r["message"])
        print(f"  Account ID : {r['account_id']}")
        print(f"  Your PayID : {format_phone_display(r['phone_number'])}")
        print(f"  Expires    : {r['expires_at']}")
        press_enter()
        return r["token"], r["account_id"], r["phone_number"]
    else:
        print_error(r["error"])
        press_enter()
        return None, None, None


# menu
def main_menu(token, account_id, phone_number):
    while True:
        print_header(f"Main Menu  [{format_phone_display(phone_number)}]")
        print("  1. View my Balance")
        print("  2. Transfer via PayID")
        print("  3. Check Transfer Status")
        print("  4. Transfer History")
        print("  5. Fee Calculator")
        print("  6. Logout")
        choice = input("\n  Select (1-6): ").strip()

        if   choice == "1": do_balance(token)
        elif choice == "2": do_transfer(token)
        elif choice == "3": do_status(token)
        elif choice == "4": do_history(token)
        elif choice == "5": do_fee_preview(token)
        elif choice == "6":
            do_logout(token)
            return
        else:
            print_error("Please enter 1–6.")
            press_enter()


# ── Balance ──────────────────────────────────────────────────────────

def do_balance(token):
    print_section("My Account Balance")
    with get_bas() as bas:
        r = bas.get_balance(token)
    if r["success"]:
        print(f"\n  Account  : {r['account_id']}")
        print(f"  PayID    : {format_phone_display(r['phone_number'])}")
        print(f"  Balance  : {r['balance_display']}")
    else:
        print_error(r["error, request timed out"])
    press_enter()


# ── Transfer (2-step PayID flow) ─────────────────────────────────────

def do_transfer(token):
    print_section("New Transfer with PayID")
    print("  Enter the recipient's mobile number.")
    print("  Example: 0423 456 789\n")

    # Step 1: resolve phone → masked name
    raw_phone = input("  Recipient phone: ").strip()
    if not raw_phone:
        print_error("Cancelled. timed out")
        press_enter()
        return

    with get_bas() as bas:
        lookup = bas.lookup_payid(token, raw_phone)

    if not lookup["success"]:
        print_error(lookup["error, could not find PAYID"])
        press_enter()
        return

    print(f"\n  Recipient found!:")
    print(f"  PayID  : {lookup['phone_display']}")
    print(f"  Name   : {lookup['masked_name']}")
    confirm_recipient = input("\n  Is this the right person? (yes/no): ").strip().lower()
    if confirm_recipient not in ("yes", "y"):
        print_error("Transfer cancelled by user")
        press_enter()
        return

    # Step 2: enter amount
    amount_str = input("\n  Amount ($): ").strip()
    try:
        amount_cents = parse_amount(amount_str)
    except ValueError as e:
        print_error(str(e))
        press_enter()
        return

    if amount_cents == 0:
        print_error("Amount must be greater than $0.00")
        press_enter()
        return

    reference = input("  Reference (optional): ").strip()

    # Preview fee
    with get_bas() as bas:
        preview = bas.calculate_fee_preview(token, amount_cents)

    if not preview["success"]:
        print_error(preview["error"])
        press_enter()
        return

    print(f"\n  ┌─ Transfer Summary ─────────────────────┐")
    print(f"  │  To      : {lookup['masked_name']:<28}│")
    print(f"  │  PayID   : {lookup['phone_display']:<28}│")
    print(f"  │  Amount  : {preview['amount_display']:<28}│")
    print(f"  │  Fee     : {preview['fee_display']:<28}│")
    print(f"  │  Total   : {preview['total_display']:<28}│")
    print(f"  └────────────────────────────────────────┘")

    confirm = input("\n  Confirm transfer? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print_error("Transfer cancelled.")
        press_enter()
        return

    idem_key = str(uuid.uuid4())
    with get_bas() as bas:
        r = bas.submit_transfer(
            token,
            lookup["phone_number"],
            amount_cents,
            reference,
            idem_key
        )

    if r["success"]:
        print_ok(r["message"])
        print(f"  Transfer ID : {r['transfer_id']}")
        print(f"  Status      : {r['status']}")
        print(f"  To          : {r.get('receiver_name')} ({r.get('receiver_phone')})")
        print(f"  Amount      : {r['amount_display']}")
        print(f"  Fee         : {r['fee_display']}")
        print(f"  Total debit : {r['total_debit_display']}")
    else:
        print_error(r.get("error", "Transfer failed"))
        if r.get("transfer_id"):
            print(f"  Transfer ID : {r['transfer_id']}")
            print(f"  Status      : {r.get('status','FAILED')}")

    press_enter()

# status of transfer
def do_status(token):
    print_section("Query Transfer Status")
    txn_id = input("  Transfer ID: ").strip()
    if not txn_id:
        print_error("Cancelled.")
        press_enter()
        return
    with get_bas() as bas:
        r = bas.get_transfer_status(token, txn_id)
    if r["success"]:
        icon = {"COMPLETED":"✓","PENDING":"⏳","FAILED":"✗"}.get(r["status"],"?")
        print(f"\n  Transfer ID  : {r['transfer_id']}")
        print(f"  Status       : [{icon}] {r['status']}")
        print(f"  From (PayID) : {format_phone_display(r.get('sender_phone',''))}")
        print(f"  To   (PayID) : {r.get('receiver_phone_display','')}")
        print(f"  Amount       : {r['amount_display']}")
        print(f"  Fee          : {r['fee_display']}")
        if r.get("reference"):
            print(f"  Reference    : {r['reference']}")
        print(f"  Created      : {r['created_at']}")
        print(f"  Updated      : {r['updated_at']}")
    else:
        print_error(r["error"])
    press_enter()


# transfer history
def do_history(token):
    print_section("Transfer History (last 10)")
    with get_bas() as bas:
        r = bas.get_transfer_history(token)
    if not r["success"]:
        print_error(r["error"])
        press_enter()
        return
    transfers = r["transfers"]
    if not transfers:
        print("\n  No transfers found.")
    else:
        for i, t in enumerate(transfers, 1):
            icon = {"COMPLETED":"✓","PENDING":"⏳","FAILED":"✗"}.get(t["status"],"?")
            print(f"\n  {i}. [{icon}] {t['transfer_id']}")
            print(f"     {format_phone_display(t.get('sender_phone','?'))} → "
                  f"{t.get('receiver_phone_display','?')}")
            print(f"     {t['amount_display']}  fee {t['fee_display']}  {t['created_at']}")
            if t.get("reference"):
                print(f"     Ref: {t['reference']}")
    press_enter()


# fee calculator
def do_fee_preview(token):
    print_section("Fee Calculator")
    amount_str = input("  Amount ($): ").strip()
    try:
        amount_cents = parse_amount(amount_str)
    except ValueError as e:
        print_error(str(e))
        press_enter()
        return
    with get_bas() as bas:
        r = bas.calculate_fee_preview(token, amount_cents)
    if r["success"]:
        print(f"\n  Amount  : {r['amount_display']}")
        print(f"  Fee     : {r['fee_display']}")
        print(f"  Total   : {r['total_display']}")
    else:
        print_error(r["error"])
    press_enter()


# logout
def do_logout(token):
    with get_bas() as bas:
        r = bas.logout(token)
    print_ok(r.get("message","Logged out."))
    press_enter()

def main():
    print_header("Banking Client (BC)")
    print("  Connecting to BAS server...")
    try:
        with get_bas() as bas:
            print(f"  Server: {bas.ping()}")
    except Exception as e:
        print_error(f"Cannot reach BAS server: {e}")
        sys.exit(1)

    while True:
        token, account_id, phone = screen_login()
        if token:
            main_menu(token, account_id, phone)
        else:
            again = input("\n  Try again? (yes/no): ").strip().lower()
            if again not in ("yes","y"):
                print("\n  Goodbye.\n")
                break

if __name__ == "__main__":
    main()