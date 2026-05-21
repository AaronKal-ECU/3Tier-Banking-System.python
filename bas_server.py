"""
bas_server.py, Bank Application Server, application tier
===========================================================================
INSTRUCTIONS
to run the program, place all 5 python files in one location, then open 4 terminal
windows at the location. paste each command in each terminal in the following order:
- python -m Pyro5.nameserver
- python bdb_server.py
- python bas_server.py
once all three are running in separate terminals, execute the final one for the UI:
python bas_server.py
===========================================================================
Aaron Kalaji 10670705, CSI3344 Assignment 2
"""

import uuid
import Pyro5.api
import Pyro5.server

from shared import (BAS_SERVICE_NAME, BDB_SERVICE_NAME,
                    calculate_fee_cents, cents_to_str,
                    normalise_phone, format_phone_display,
                    STATUS_PENDING, STATUS_COMPLETED, STATUS_FAILED)

def get_bdb():
    ns  = Pyro5.api.locate_ns()
    uri = ns.lookup(BDB_SERVICE_NAME)
    return Pyro5.api.Proxy(uri)
@Pyro5.api.expose
class BASService:

    # login
    def login(self, username: str, password: str) -> dict:
        if not username or not password:
            return {"success": False,
                    "error": "Username and password are required"}
        with get_bdb() as bdb:
            creds = bdb.validate_credentials(username.strip(), password)
            if not creds["success"]:
                return creds
            session = bdb.create_session(creds["user_id"], creds["account_id"])
        return {
            "success":      True,
            "token":        session["token"],
            "username":     creds["username"],
            "full_name":    creds["full_name"],
            "account_id":   creds["account_id"],
            "phone_number": creds["phone_number"],
            "expires_at":   session["expires_at"],
            "message":      f"Welcome, {creds['full_name']}!",
        }

    def logout(self, token: str) -> dict:
        with get_bdb() as bdb:
            bdb.invalidate_session(token)
        return {"success": True, "message": "Logged out successfully"}

    # check balane
    def get_balance(self, token: str) -> dict:
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}
        with get_bdb() as bdb:
            result = bdb.get_balance(session["account_id"])
        if not result["success"]:
            return result
        return {
            "success":         True,
            "account_id":      session["account_id"],
            "phone_number":    session["phone_number"],
            "balance_cents":   result["balance_cents"],
            "balance_display": cents_to_str(result["balance_cents"]),
            "username":        session["username"],
        }

    # phone number link to account before transfer (PAYID)
    def lookup_payid(self, token: str, raw_phone: str) -> dict:
        """
        Step 1 of transfer: resolve a phone number to a masked name.
        Client uses this to confirm 'Sending to Alice J.?' before submitting.
        """
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        try:
            phone = normalise_phone(raw_phone)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        # Can't send to yourself
        if phone == session["phone_number"]:
            return {"success": False,
                    "error": "You cannot transfer to your own account"}

        with get_bdb() as bdb:
            result = bdb.resolve_phone(phone)

        if not result["success"]:
            return result

        return {
            "success":      True,
            "phone_number": phone,
            "phone_display": format_phone_display(phone),
            "account_id":   result["account_id"],
            "masked_name":  result["masked_name"],
        }

    # transfers
    def submit_transfer(self, token: str, receiver_phone: str,
                        amount_cents: int, reference: str,
                        idempotency_key: str = None) -> dict:
        """
        Submit and settle a transfer by recipient phone number (PayID).

        Flow:
          1. Authenticate session
          2. Validate and normalise receiver phone
          3. Resolve phone → account_id via BDB
          4. Validate amount
          5. Calculate fee
          6. Create PENDING transfer record (idempotency check)
          7. Settle atomically in BDB (single transaction)
          8. Return COMPLETED or FAILED with full details
        """
        # 1. Auth
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        sender_account = session["account_id"]
        sender_phone   = session["phone_number"]
        user_id        = session["user_id"]

        # 2. Normalise receiver phone
        try:
            receiver_phone = normalise_phone(receiver_phone)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if receiver_phone == sender_phone:
            return {"success": False,
                    "error": "Cannot transfer to your own account"}

        # 3. Resolve phone → account_id
        with get_bdb() as bdb:
            lookup = bdb.resolve_phone(receiver_phone)
        if not lookup["success"]:
            return {"success": False,
                    "error": f"Recipient not found: {lookup['error']}"}
        receiver_account = lookup["account_id"]
        masked_name      = lookup["masked_name"]

        # 4. Validate amount
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            return {"success": False,
                    "error": "Amount must be a positive value"}

        # 5. Calculate fee
        fee_cents = calculate_fee_cents(amount_cents)

        # 6. Idempotency key
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())

        # 7. Create PENDING record
        with get_bdb() as bdb:
            create = bdb.create_transfer_record(
                sender_account, receiver_account,
                sender_phone, receiver_phone,
                amount_cents, fee_cents,
                reference or "", idempotency_key
            )
        if not create["success"]:
            return {"success": False,
                    "error": "Could not create transfer: " + create.get("error","")}

        transfer_id = create["transfer_id"]
        if create.get("duplicate"):
            return {
                "success":     True,
                "transfer_id": transfer_id,
                "status":      create["status"],
                "message":     "Duplicate request — returning existing transfer",
                "duplicate":   True,
            }

        # 8. Settle
        with get_bdb() as bdb:
            settle = bdb.settle_transfer(
                transfer_id, sender_account, receiver_account,
                amount_cents, fee_cents, user_id
            )

        if settle["success"]:
            return {
                "success":            True,
                "transfer_id":        transfer_id,
                "status":             STATUS_COMPLETED,
                "receiver_phone":     format_phone_display(receiver_phone),
                "receiver_name":      masked_name,
                "amount_display":     cents_to_str(amount_cents),
                "fee_display":        cents_to_str(fee_cents),
                "total_debit_display": cents_to_str(amount_cents + fee_cents),
                "message":            "Transfer completed successfully",
            }
        else:
            return {
                "success":        False,
                "transfer_id":    transfer_id,
                "status":         settle.get("status", STATUS_FAILED),
                "error":          settle.get("error", "Transfer failed"),
                "amount_display": cents_to_str(amount_cents),
                "fee_display":    cents_to_str(fee_cents),
            }

    # check transfers status
    def get_transfer_status(self, token: str, transfer_id: str) -> dict:
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}
        with get_bdb() as bdb:
            result = bdb.get_transfer_status(
                transfer_id.strip().upper(), session["account_id"]
            )
        if result["success"]:
            result["amount_display"] = cents_to_str(result["amount_cents"])
            result["fee_display"]    = cents_to_str(result["fee_cents"])
            result["receiver_phone_display"] = format_phone_display(
                result.get("receiver_phone",""))
        return result

    def get_transfer_history(self, token: str) -> dict:
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}
        with get_bdb() as bdb:
            result = bdb.get_transfer_history(session["account_id"])
        if result["success"]:
            for t in result["transfers"]:
                t["amount_display"] = cents_to_str(t["amount_cents"])
                t["fee_display"]    = cents_to_str(t["fee_cents"])
                t["receiver_phone_display"] = format_phone_display(
                    t.get("receiver_phone",""))
        return result

    # fee preview calculator
    def calculate_fee_preview(self, token: str, amount_cents: int) -> dict:
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            return {"success": False, "error": "Invalid amount"}
        fee = calculate_fee_cents(amount_cents)
        return {
            "success":        True,
            "amount_display": cents_to_str(amount_cents),
            "fee_display":    cents_to_str(fee),
            "total_display":  cents_to_str(amount_cents + fee),
            "fee_cents":      fee,
        }

    # helpers
    def _require_auth(self, token: str) -> dict:
        if not token:
            return {"valid": False,
                    "error": "No session token. Please log in."}
        with get_bdb() as bdb:
            return bdb.validate_session(token)

    def ping(self) -> str:
        return "BAS OK"

def main():
    print("=" * 52)
    print("  Bank Application Server (BAS) starting...")
    print("=" * 52)
    print("[BAS] Connecting to BDB server...")
    try:
        with get_bdb() as bdb:
            print(f"[BAS] BDB ping: {bdb.ping()}")
    except Exception as e:
        print(f"[BAS] ERROR — cannot reach BDB: {e}")
        print("      Start bdb_server.py first.")
        return

    daemon = Pyro5.server.Daemon()
    ns  = Pyro5.api.locate_ns()
    uri = daemon.register(BASService, objectId="banking.bas.service")
    ns.register(BAS_SERVICE_NAME, uri)
    print(f"[BAS] Registered as '{BAS_SERVICE_NAME}' → {uri}")
    print("[BAS] Ready. Waiting for BC client connections...")
    print("      (Ctrl+C to stop)\n")
    daemon.requestLoop()

if __name__ == "__main__":
    main()