"""
bas_server.py - Bank Application Server (BAS) — Application Tier
=================================================================
Responsibilities:
  - Exposes a Pyro5 RPC interface to BC clients
  - Enforces all business rules: authentication, validation, fee calc
  - Communicates with BDB server for all persistence
  - BC client never talks to BDB directly

Communication design:
  - Client <-> BAS: Synchronous RPC (Pyro5)
    Justified: login, balance, transfer all require immediate responses.
    The user must wait for confirmation before taking further action.
  - BAS <-> BDB: Synchronous RPC (Pyro5)
    Justified: BAS must know the outcome of every DB operation before
    responding to the client. Atomicity requires sequential confirmation.

Consistency:
  - Transfers use idempotency keys (client-generated UUID) to prevent
    duplicate processing on retry.
  - BDB settle_transfer() uses a single SQLite transaction: if anything
    fails mid-way, the entire operation rolls back. No partial updates.
  - PENDING status ensures a transfer is visible even before settlement,
    allowing status queries during processing.

Run: python bas_server.py
Requires: python -m Pyro5.nameserver AND python bdb_server.py first
"""

import uuid
import Pyro5.api
import Pyro5.server

from shared import (BAS_SERVICE_NAME, BDB_SERVICE_NAME,
                    calculate_fee_cents, cents_to_str,
                    STATUS_PENDING, STATUS_COMPLETED, STATUS_FAILED)


def get_bdb() -> Pyro5.api.Proxy:
    """Get a proxy to the BDB server via Pyro5 name server."""
    ns = Pyro5.api.locate_ns()
    uri = ns.lookup(BDB_SERVICE_NAME)
    return Pyro5.api.Proxy(uri)


@Pyro5.api.expose
class BASService:
    """
    Application server service exposed to BC clients.
    All operations validated and authorized here before touching BDB.
    """

    # ------------------------------------------------------------------ #
    #  (A) LOGIN / AUTHENTICATION                                          #
    # ------------------------------------------------------------------ #

    def login(self, username: str, password: str) -> dict:
        """
        Authenticate user. Returns session token on success.
        Token must be passed to all subsequent API calls.
        Design: BAS delegates credential check to BDB, then asks BDB
        to create the session record. Token is returned to client.
        """
        if not username or not password:
            return {"success": False, "error": "Username and password required"}

        with get_bdb() as bdb:
            result = bdb.validate_credentials(username.strip(), password)
            if not result["success"]:
                return result

            session = bdb.create_session(result["user_id"], result["account_id"])
            return {
                "success": True,
                "token": session["token"],
                "username": result["username"],
                "full_name": result["full_name"],
                "account_id": result["account_id"],
                "expires_at": session["expires_at"],
                "message": f"Welcome, {result['full_name']}!",
            }

    def logout(self, token: str) -> dict:
        """Invalidate session token."""
        with get_bdb() as bdb:
            bdb.invalidate_session(token)
        return {"success": True, "message": "Logged out successfully"}

    # ------------------------------------------------------------------ #
    #  (B) BALANCE QUERY                                                   #
    # ------------------------------------------------------------------ #

    def get_balance(self, token: str) -> dict:
        """
        Return authenticated user's balance.
        Synchronous: user expects immediate, accurate balance.
        BAS validates token first, then fetches balance from BDB.
        """
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        with get_bdb() as bdb:
            result = bdb.get_balance(session["account_id"])
        if not result["success"]:
            return result

        return {
            "success": True,
            "account_id": session["account_id"],
            "balance_cents": result["balance_cents"],
            "balance_display": cents_to_str(result["balance_cents"]),
            "username": session["username"],
        }

    # ------------------------------------------------------------------ #
    #  (C) TRANSFER REQUEST                                                #
    # ------------------------------------------------------------------ #

    def submit_transfer(self, token: str, receiver_account: str,
                        amount_cents: int, reference: str,
                        idempotency_key: str = None) -> dict:
        """
        Submit a transfer request.

        Processing is SYNCHRONOUS: we complete the full debit/credit in one
        RPC call and return a COMPLETED or FAILED result. This is appropriate
        for this teaching system where simplicity and immediate confirmation
        are priorities. In a real system, large transfers might be queued.

        Idempotency: if the client retries with the same idempotency_key,
        the existing transfer record is returned without re-processing.
        This handles network failures and duplicate submissions.

        Consistency: BDB's settle_transfer() uses a single SQLite transaction.
        The sequence is: create PENDING record -> validate funds -> debit/credit
        -> mark COMPLETED. Any failure at any step rolls back completely.
        """
        # 1. Authenticate
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        sender_account = session["account_id"]
        user_id = session["user_id"]

        # 2. Validate inputs
        if not receiver_account or not receiver_account.strip():
            return {"success": False, "error": "Recipient account ID is required"}

        receiver_account = receiver_account.strip().upper()

        if receiver_account == sender_account:
            return {"success": False, "error": "Cannot transfer to your own account"}

        if not isinstance(amount_cents, int) or amount_cents <= 0:
            return {"success": False, "error": "Amount must be a positive value"}

        if amount_cents < 1:  # less than $0.01
            return {"success": False, "error": "Minimum transfer amount is $0.01"}

        # 3. Check receiver exists
        with get_bdb() as bdb:
            if not bdb.account_exists(receiver_account):
                return {"success": False,
                        "error": f"Recipient account '{receiver_account}' not found"}

        # 4. Calculate fee
        fee_cents = calculate_fee_cents(amount_cents)

        # 5. Generate idempotency key if not provided
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())

        # 6. Create PENDING transfer record (idempotency check inside)
        with get_bdb() as bdb:
            create_result = bdb.create_transfer_record(
                sender_account, receiver_account,
                amount_cents, fee_cents,
                reference or "", idempotency_key
            )

        if not create_result["success"]:
            return {"success": False,
                    "error": "Failed to create transfer record: " + create_result.get("error", "")}

        transfer_id = create_result["transfer_id"]

        # If duplicate key, return existing transfer info
        if create_result.get("duplicate"):
            return {
                "success": True,
                "transfer_id": transfer_id,
                "status": create_result["status"],
                "message": "Duplicate request — returning existing transfer",
                "duplicate": True,
            }

        # 7. Settle: atomic debit/credit in BDB
        with get_bdb() as bdb:
            settle_result = bdb.settle_transfer(
                transfer_id, sender_account, receiver_account,
                amount_cents, fee_cents, user_id
            )

        if settle_result["success"]:
            return {
                "success": True,
                "transfer_id": transfer_id,
                "status": STATUS_COMPLETED,
                "amount_display": cents_to_str(amount_cents),
                "fee_display": cents_to_str(fee_cents),
                "total_debit_display": cents_to_str(amount_cents + fee_cents),
                "receiver_account": receiver_account,
                "message": "Transfer completed successfully",
            }
        else:
            return {
                "success": False,
                "transfer_id": transfer_id,
                "status": settle_result.get("status", STATUS_FAILED),
                "error": settle_result.get("error", "Transfer failed"),
                "amount_display": cents_to_str(amount_cents),
                "fee_display": cents_to_str(fee_cents),
            }

    # ------------------------------------------------------------------ #
    #  (D) TRANSFER STATUS QUERY                                           #
    # ------------------------------------------------------------------ #

    def get_transfer_status(self, token: str, transfer_id: str) -> dict:
        """Query status of a specific transfer by ID."""
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        with get_bdb() as bdb:
            result = bdb.get_transfer_status(transfer_id.strip().upper(),
                                              session["account_id"])
        if not result["success"]:
            return result

        result["amount_display"] = cents_to_str(result["amount_cents"])
        result["fee_display"] = cents_to_str(result["fee_cents"])
        return result

    def get_transfer_history(self, token: str) -> dict:
        """Return recent transfer history for authenticated user."""
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        with get_bdb() as bdb:
            result = bdb.get_transfer_history(session["account_id"])

        if result["success"]:
            for t in result["transfers"]:
                t["amount_display"] = cents_to_str(t["amount_cents"])
                t["fee_display"] = cents_to_str(t["fee_cents"])
        return result

    # ------------------------------------------------------------------ #
    #  HELPERS                                                             #
    # ------------------------------------------------------------------ #

    def _require_auth(self, token: str) -> dict:
        """Validate token and return session info."""
        if not token:
            return {"valid": False, "error": "No session token provided. Please login."}
        with get_bdb() as bdb:
            return bdb.validate_session(token)

    def ping(self) -> str:
        """Health check."""
        return "BAS OK"

    def calculate_fee_preview(self, token: str, amount_cents: int) -> dict:
        """
        Preview fee for a given amount without submitting.
        Useful for client-side display before user confirms.
        """
        session = self._require_auth(token)
        if not session["valid"]:
            return {"success": False, "error": session["error"]}

        if not isinstance(amount_cents, int) or amount_cents <= 0:
            return {"success": False, "error": "Invalid amount"}

        fee_cents = calculate_fee_cents(amount_cents)
        return {
            "success": True,
            "amount_display": cents_to_str(amount_cents),
            "fee_display": cents_to_str(fee_cents),
            "total_display": cents_to_str(amount_cents + fee_cents),
            "fee_cents": fee_cents,
        }


def main():
    print("=" * 50)
    print("  Bank Application Server (BAS) starting...")
    print("=" * 50)

    # Verify BDB is reachable before starting
    print("[BAS] Connecting to BDB server...")
    try:
        with get_bdb() as bdb:
            pong = bdb.ping()
        print(f"[BAS] BDB connection OK: {pong}")
    except Exception as e:
        print(f"[BAS] ERROR: Cannot reach BDB server: {e}")
        print("      Make sure bdb_server.py is running first.")
        return

    daemon = Pyro5.server.Daemon()
    ns = Pyro5.api.locate_ns()
    uri = daemon.register(BASService, objectId="banking.bas.service")
    ns.register(BAS_SERVICE_NAME, uri)
    print(f"[BAS] Registered as '{BAS_SERVICE_NAME}' -> {uri}")
    print("[BAS] Ready. Waiting for BC client connections...")
    print("      (Press Ctrl+C to stop)\n")
    daemon.requestLoop()


if __name__ == "__main__":
    main()