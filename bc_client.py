"""
bc_client.py - Banking Client (BC) — Client Tier
=================================================
Customer-facing terminal application.
Communicates ONLY with BAS server via Pyro5 RPC.
Never contacts BDB server directly.

Usage: python bc_client.py
Requires: name server + bdb_server + bas_server all running.
"""

import sys
import uuid
import Pyro5.api

from shared import BAS_SERVICE_NAME, parse_amount, cents_to_str


# ------------------------------------------------------------------ #
#  Connection                                                          #
# ------------------------------------------------------------------ #

def get_bas() -> Pyro5.api.Proxy:
    """Connect to BAS server via Pyro5 name server."""
    try:
        ns = Pyro5.api.locate_ns()
        uri = ns.lookup(BAS_SERVICE_NAME)
        return Pyro5.api.Proxy(uri)
    except Exception as e:
        print(f"\n[ERROR] Cannot connect to BAS server: {e}")
        print("  Make sure these are running in order:")
        print("  1. python -m Pyro5.nameserver")
        print("  2. python bdb_server.py")
        print("  3. python bas_server.py")
        sys.exit(1)


# ------------------------------------------------------------------ #
#  Display helpers                                                     #
# ------------------------------------------------------------------ #

DIVIDER = "─" * 52
HEADER  = "═" * 52

def print_header(title: str):
    print(f"\n{HEADER}")
    print(f"  {title}")
    print(HEADER)

def print_section(title: str):
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)

def print_error(msg: str):
    print(f"\n  [!] {msg}")

def print_ok(msg: str):
    print(f"\n  [✓] {msg}")

def press_enter():
    input("\n  Press Enter to continue...")


# ------------------------------------------------------------------ #
#  Login screen                                                        #
# ------------------------------------------------------------------ #

def screen_login() -> tuple:
    """
    Returns (token, account_id) on success or (None, None).
    """
    print_header("ECU Banking System — Sign In")
    print("  Mock accounts: alice/alice123  bob/bob123")
    print("                 carol/carol123  dave/dave123")
    print()
    username = input("  Username: ").strip()
    if not username:
        return None, None
    password = input("  Password: ").strip()

    with get_bas() as bas:
        result = bas.login(username, password)

    if result["success"]:
        print_ok(result["message"])
        print(f"  Account: {result['account_id']}")
        print(f"  Session expires: {result['expires_at']}")
        press_enter()
        return result["token"], result["account_id"]
    else:
        print_error(result["error"])
        press_enter()
        return None, None


# ------------------------------------------------------------------ #
#  Main menu                                                           #
# ------------------------------------------------------------------ #

def main_menu(token: str, account_id: str):
    """Main authenticated menu loop."""
    while True:
        print_header(f"Main Menu  [{account_id}]")
        print("  1. View Balance")
        print("  2. Submit Transfer")
        print("  3. Query Transfer Status")
        print("  4. Transfer History")
        print("  5. Fee Calculator")
        print("  6. Logout")
        print()
        choice = input("  Select option (1-6): ").strip()

        if choice == "1":
            do_balance(token)
        elif choice == "2":
            do_transfer(token, account_id)
        elif choice == "3":
            do_status(token)
        elif choice == "4":
            do_history(token)
        elif choice == "5":
            do_fee_preview(token)
        elif choice == "6":
            do_logout(token)
            return
        else:
            print_error("Invalid option. Please enter 1-6.")
            press_enter()


# ------------------------------------------------------------------ #
#  (B) Balance                                                         #
# ------------------------------------------------------------------ #

def do_balance(token: str):
    print_section("Account Balance")
    with get_bas() as bas:
        result = bas.get_balance(token)
    if result["success"]:
        print(f"\n  Account:   {result['account_id']}")
        print(f"  Balance:   {result['balance_display']}")
    else:
        print_error(result["error"])
    press_enter()


# ------------------------------------------------------------------ #
#  (C) Transfer                                                        #
# ------------------------------------------------------------------ #

def do_transfer(token: str, account_id: str):
    print_section("Submit Transfer")
    print(f"  Sending from: {account_id}")
    print()

    # Get recipient
    receiver = input("  Recipient Account ID (e.g. ACC002): ").strip()
    if not receiver:
        print_error("Cancelled.")
        press_enter()
        return

    # Get amount
    amount_str = input("  Amount ($): ").strip()
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

    # Optional reference
    reference = input("  Reference (optional): ").strip()

    # Preview fee before confirming
    with get_bas() as bas:
        preview = bas.calculate_fee_preview(token, amount_cents)

    if not preview["success"]:
        print_error(preview["error"])
        press_enter()
        return

    print()
    print(f"  ┌─ Transfer Summary ─────────────────┐")
    print(f"  │  To:          {receiver:<22}│")
    print(f"  │  Amount:      {preview['amount_display']:<22}│")
    print(f"  │  Fee:         {preview['fee_display']:<22}│")
    print(f"  │  Total debit: {preview['total_display']:<22}│")
    print(f"  └────────────────────────────────────┘")
    print()

    confirm = input("  Confirm transfer? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print_error("Transfer cancelled.")
        press_enter()
        return

    # Submit with a fresh idempotency key
    idempotency_key = str(uuid.uuid4())
    with get_bas() as bas:
        result = bas.submit_transfer(
            token, receiver, amount_cents, reference, idempotency_key
        )

    if result["success"]:
        print_ok(result["message"])
        print(f"  Transfer ID: {result['transfer_id']}")
        print(f"  Status:      {result['status']}")
        print(f"  Amount:      {result['amount_display']}")
        print(f"  Fee:         {result['fee_display']}")
        print(f"  Total:       {result['total_debit_display']}")
    else:
        print_error(result.get("error", "Transfer failed"))
        if "transfer_id" in result:
            print(f"  Transfer ID: {result['transfer_id']}")
            print(f"  Status:      {result.get('status', 'FAILED')}")

    press_enter()


# ------------------------------------------------------------------ #
#  (D) Transfer Status                                                 #
# ------------------------------------------------------------------ #

def do_status(token: str):
    print_section("Query Transfer Status")
    txn_id = input("  Transfer ID (e.g. TXNABC123...): ").strip()
    if not txn_id:
        print_error("Cancelled.")
        press_enter()
        return

    with get_bas() as bas:
        result = bas.get_transfer_status(token, txn_id)

    if result["success"]:
        status = result["status"]
        status_icon = {"COMPLETED": "✓", "PENDING": "⏳", "FAILED": "✗"}.get(status, "?")
        print(f"\n  Transfer ID:  {result['transfer_id']}")
        print(f"  Status:       [{status_icon}] {status}")
        print(f"  From:         {result['sender_account']}")
        print(f"  To:           {result['receiver_account']}")
        print(f"  Amount:       {result['amount_display']}")
        print(f"  Fee:          {result['fee_display']}")
        if result.get("reference"):
            print(f"  Reference:    {result['reference']}")
        print(f"  Created:      {result['created_at']}")
        print(f"  Updated:      {result['updated_at']}")
    else:
        print_error(result["error"])

    press_enter()


# ------------------------------------------------------------------ #
#  Transfer History                                                    #
# ------------------------------------------------------------------ #

def do_history(token: str):
    print_section("Transfer History (last 10)")
    with get_bas() as bas:
        result = bas.get_transfer_history(token)

    if not result["success"]:
        print_error(result["error"])
        press_enter()
        return

    transfers = result["transfers"]
    if not transfers:
        print("\n  No transfers found.")
    else:
        for i, t in enumerate(transfers, 1):
            status_icon = {"COMPLETED": "✓", "PENDING": "⏳", "FAILED": "✗"}.get(
                t["status"], "?")
            direction = "SENT" if t.get("sender_account") else "RECV"
            print(f"\n  {i}. [{status_icon}] {t['transfer_id']}")
            print(f"     {t['sender_account']} → {t['receiver_account']}")
            print(f"     {t['amount_display']}  fee {t['fee_display']}  {t['created_at']}")
            if t.get("reference"):
                print(f"     Ref: {t['reference']}")

    press_enter()


# ------------------------------------------------------------------ #
#  Fee Calculator                                                      #
# ------------------------------------------------------------------ #

def do_fee_preview(token: str):
    print_section("Fee Calculator")
    print("  Check the fee for any transfer amount without submitting.")
    print()
    amount_str = input("  Amount ($): ").strip()
    try:
        amount_cents = parse_amount(amount_str)
    except ValueError as e:
        print_error(str(e))
        press_enter()
        return

    with get_bas() as bas:
        result = bas.calculate_fee_preview(token, amount_cents)

    if result["success"]:
        print(f"\n  Amount:      {result['amount_display']}")
        print(f"  Fee:         {result['fee_display']}")
        print(f"  Total debit: {result['total_display']}")
    else:
        print_error(result["error"])

    press_enter()


# ------------------------------------------------------------------ #
#  Logout                                                              #
# ------------------------------------------------------------------ #

def do_logout(token: str):
    with get_bas() as bas:
        result = bas.logout(token)
    print_ok(result.get("message", "Logged out."))
    press_enter()


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def main():
    print_header("ECU Banking Client (BC)")
    print("  Connecting to BAS server...")
    try:
        with get_bas() as bas:
            pong = bas.ping()
        print(f"  Server status: {pong}")
    except Exception as e:
        print_error(f"Cannot reach BAS server: {e}")
        sys.exit(1)

    while True:
        token, account_id = screen_login()
        if token:
            main_menu(token, account_id)
        else:
            print()
            again = input("  Try again? (yes/no): ").strip().lower()
            if again not in ("yes", "y"):
                print("\n  Goodbye.\n")
                break


if __name__ == "__main__":
    main()