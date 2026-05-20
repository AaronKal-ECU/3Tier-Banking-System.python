"""
bdb_server.py - Bank Database Server (BDB) — Data Tier
=======================================================
Responsibilities:
  - Manages all persistent data via SQLite
  - Exposes a Pyro5 RPC interface to BAS server ONLY
  - BC client has NO direct access to this server
  - Handles: users, accounts, sessions, transfers, audit logs

Run:  python bdb_server.py
Requires Pyro5 name server to be running first:
  python -m Pyro5.nameserver
"""

import sqlite3
import hashlib
import uuid
import datetime
import os
import Pyro5.api
import Pyro5.server

from shared import BDB_SERVICE_NAME, STATUS_PENDING, STATUS_COMPLETED, STATUS_FAILED

DB_PATH = "bank.db"


def _hash_password(password: str) -> str:
    """SHA-256 hash of password"""
    return hashlib.sha256(password.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent read/write
    conn.execute("PRAGMA journal_mode=WAL")
    # Enforce foreign key constraints
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create database and placeholder data if not already present."""
    conn = get_connection()
    cur = conn.cursor()

    # users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     TEXT PRIMARY KEY,
            username    TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name   TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)

    # accounts table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            account_id  TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # sessions table (for auth tokens each login)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            account_id  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # transfers table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            transfer_id     TEXT PRIMARY KEY,
            sender_account  TEXT NOT NULL,
            receiver_account TEXT NOT NULL,
            amount_cents    INTEGER NOT NULL,
            fee_cents       INTEGER NOT NULL,
            status          TEXT NOT NULL,
            reference       TEXT,
            idempotency_key TEXT UNIQUE,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (sender_account)   REFERENCES accounts(account_id),
            FOREIGN KEY (receiver_account) REFERENCES accounts(account_id)
        )
    """)

    # audit log table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id      TEXT PRIMARY KEY,
            event_type  TEXT NOT NULL,
            user_id     TEXT,
            account_id  TEXT,
            transfer_id TEXT,
            detail      TEXT,
            created_at  TEXT NOT NULL
        )
    """)

    conn.commit()

    # insert our placeholder data to show project functionality
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        _seed_mock_data(conn)

    conn.close()
    print(f"[BDB] Database initialized at '{DB_PATH}'")


def _seed_mock_data(conn: sqlite3.Connection):
    """Insert users and accounts for testing."""
    now = _now_iso()
    mock_users = [
        ("USR001", "alice",  "alice123",  "Alice Johnson",   "ACC001", 5_000_000),   # $50,000.00
        ("USR002", "bob",    "bob123",    "Bob Smith",        "ACC002", 2_000_000),   # $20,000.00
        ("USR003", "carol",  "carol123",  "Carol White",      "ACC003",   500_000),   # $5,000.00
        ("USR004", "dave",   "dave123",   "Dave Martinez",    "ACC004", 15_000_000),  # $150,000.00
    ]
    for (uid, uname, pwd, fname, acc_id, balance) in mock_users:
        conn.execute(
            "INSERT INTO users VALUES (?,?,?,?,?)",
            (uid, uname, _hash_password(pwd), fname, now)
        )
        conn.execute(
            "INSERT INTO accounts VALUES (?,?,?,?)",
            (acc_id, uid, balance, now)
        )
    conn.commit()
    print("[BDB] Mock data seeded: alice, bob, carol, dave")


@Pyro5.api.expose
class BDBService:
    """
    Pyro5-exposed database service.
    All methods are called only by BAS server — never directly by BC client.
    All money values are in integer cents throughout this interface.
    """

    # ------------------------------------------------------------------ #
    #  AUTH / SESSION                                                      #
    # ------------------------------------------------------------------ #

    def validate_credentials(self, username: str, password: str) -> dict:
        """
        Check username/password. Returns user+account info or error.
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT u.user_id, u.username, u.full_name, a.account_id "
                "FROM users u JOIN accounts a ON u.user_id = a.user_id "
                "WHERE u.username = ? AND u.password_hash = ?",
                (username, _hash_password(password))
            )
            row = cur.fetchone()
            if row is None:
                self._audit(conn, "LOGIN_FAIL", detail=f"username={username}")
                conn.commit()
                return {"success": False, "error": "Invalid username or password"}
            result = {
                "success": True,
                "user_id": row["user_id"],
                "username": row["username"],
                "full_name": row["full_name"],
                "account_id": row["account_id"],
            }
            self._audit(conn, "LOGIN_OK", user_id=row["user_id"],
                        account_id=row["account_id"])
            conn.commit()
            return result
        finally:
            conn.close()

    def create_session(self, user_id: str, account_id: str) -> dict:
        """Create and store a session token. Expires in 1 hour."""
        token = str(uuid.uuid4())
        now = datetime.datetime.now()
        expires = (now + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,1)",
                (token, user_id, account_id, now.isoformat(timespec="seconds"), expires)
            )
            conn.commit()
            return {"token": token, "expires_at": expires}
        finally:
            conn.close()

    def validate_session(self, token: str) -> dict:
        """Check a session token. Returns user/account info or error."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.user_id, s.account_id, s.expires_at, s.is_active, "
                "u.username, u.full_name "
                "FROM sessions s JOIN users u ON s.user_id = u.user_id "
                "WHERE s.token = ?",
                (token,)
            )
            row = cur.fetchone()
            if row is None:
                return {"valid": False, "error": "Token not found"}
            if not row["is_active"]:
                return {"valid": False, "error": "Session has been revoked"}
            now = datetime.datetime.now().isoformat(timespec="seconds")
            if row["expires_at"] < now:
                return {"valid": False, "error": "Session expired"}
            return {
                "valid": True,
                "user_id": row["user_id"],
                "account_id": row["account_id"],
                "username": row["username"],
                "full_name": row["full_name"],
            }
        finally:
            conn.close()

    def invalidate_session(self, token: str) -> bool:
        """Logout - deactivate the session token."""
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE sessions SET is_active=0 WHERE token=?", (token,)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  BALANCE                                                             #
    # ------------------------------------------------------------------ #

    def get_balance(self, account_id: str) -> dict:
        """Return current balance in cents for account_id."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT balance_cents FROM accounts WHERE account_id=?",
                (account_id,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False, "error": "Account not found"}
            return {"success": True, "balance_cents": row["balance_cents"],
                    "account_id": account_id}
        finally:
            conn.close()

    def account_exists(self, account_id: str) -> bool:
        """Check if account_id exists."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM accounts WHERE account_id=?", (account_id,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  TRANSFER                                                            #
    # ------------------------------------------------------------------ #

    def create_transfer_record(self, sender_account: str, receiver_account: str,
                                amount_cents: int, fee_cents: int,
                                reference: str, idempotency_key: str) -> dict:
        """
        Insert a PENDING transfer record atomically.
        Idempotency key prevents duplicate submissions.
        Returns transfer_id or error if duplicate key found.
        """
        conn = get_connection()
        try:
            # Check for duplicate idempotency key
            cur = conn.cursor()
            cur.execute(
                "SELECT transfer_id, status FROM transfers WHERE idempotency_key=?",
                (idempotency_key,)
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "success": True,
                    "transfer_id": existing["transfer_id"],
                    "status": existing["status"],
                    "duplicate": True,
                }
            transfer_id = "TXN" + str(uuid.uuid4()).replace("-", "")[:12].upper()
            now = _now_iso()
            conn.execute(
                "INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?,?)",
                (transfer_id, sender_account, receiver_account,
                 amount_cents, fee_cents, STATUS_PENDING,
                 reference, idempotency_key, now, now)
            )
            conn.commit()
            return {"success": True, "transfer_id": transfer_id,
                    "status": STATUS_PENDING, "duplicate": False}
        except Exception as e:
            conn.rollback()
            return {"success": False, "error": str(e)}
        finally:
            conn.close()

    def settle_transfer(self, transfer_id: str, sender_account: str,
                        receiver_account: str, amount_cents: int,
                        fee_cents: int, user_id: str) -> dict:
        """
        Atomically settle a transfer:
          - Debit sender by (amount + fee)
          - Credit receiver by amount
          - Update transfer status to COMPLETED
          - Write audit log entry
        Uses a single SQLite transaction for atomicity.
        If any step fails, the whole transaction rolls back (FAILED status set).
        """
        conn = get_connection()
        try:
            cur = conn.cursor()

            # Lock check: verify transfer is still PENDING
            cur.execute(
                "SELECT status FROM transfers WHERE transfer_id=?",
                (transfer_id,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False, "error": "Transfer not found"}
            if row["status"] != STATUS_PENDING:
                return {"success": False,
                        "error": f"Transfer already in state: {row['status']}"}

            # Check sender has sufficient funds
            cur.execute(
                "SELECT balance_cents FROM accounts WHERE account_id=?",
                (sender_account,)
            )
            sender_row = cur.fetchone()
            if sender_row is None:
                return {"success": False, "error": "Sender account not found"}

            total_debit = amount_cents + fee_cents
            if sender_row["balance_cents"] < total_debit:
                # Mark transfer FAILED
                conn.execute(
                    "UPDATE transfers SET status=?, updated_at=? WHERE transfer_id=?",
                    (STATUS_FAILED, _now_iso(), transfer_id)
                )
                self._audit(conn, "TRANSFER_FAILED", user_id=user_id,
                            account_id=sender_account, transfer_id=transfer_id,
                            detail="Insufficient funds")
                conn.commit()
                return {"success": False, "error": "Insufficient funds",
                        "transfer_id": transfer_id, "status": STATUS_FAILED}

            now = _now_iso()
            # Atomic: debit sender, credit receiver, update transfer
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents - ? "
                "WHERE account_id=?",
                (total_debit, sender_account)
            )
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ? "
                "WHERE account_id=?",
                (amount_cents, receiver_account)
            )
            conn.execute(
                "UPDATE transfers SET status=?, updated_at=? WHERE transfer_id=?",
                (STATUS_COMPLETED, now, transfer_id)
            )
            self._audit(conn, "TRANSFER_COMPLETED", user_id=user_id,
                        account_id=sender_account, transfer_id=transfer_id,
                        detail=f"amount={amount_cents}c fee={fee_cents}c to={receiver_account}")
            conn.commit()
            return {"success": True, "transfer_id": transfer_id,
                    "status": STATUS_COMPLETED}

        except Exception as e:
            conn.rollback()
            # Attempt to mark transfer as FAILED
            try:
                conn.execute(
                    "UPDATE transfers SET status=?, updated_at=? WHERE transfer_id=?",
                    (STATUS_FAILED, _now_iso(), transfer_id)
                )
                conn.commit()
            except Exception:
                pass
            return {"success": False, "error": f"Settlement error: {str(e)}",
                    "transfer_id": transfer_id, "status": STATUS_FAILED}
        finally:
            conn.close()

    def get_transfer_status(self, transfer_id: str, requesting_account: str) -> dict:
        """
        Return status of a transfer.
        requesting_account must be sender or receiver for authorization.
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM transfers WHERE transfer_id=?",
                (transfer_id,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False, "error": "Transfer not found"}
            # Authorization check
            if (row["sender_account"] != requesting_account and
                    row["receiver_account"] != requesting_account):
                return {"success": False, "error": "Not authorized to view this transfer"}
            return {
                "success": True,
                "transfer_id": row["transfer_id"],
                "sender_account": row["sender_account"],
                "receiver_account": row["receiver_account"],
                "amount_cents": row["amount_cents"],
                "fee_cents": row["fee_cents"],
                "status": row["status"],
                "reference": row["reference"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()

    def get_transfer_history(self, account_id: str, limit: int = 10) -> dict:
        """Return recent transfers for an account (sent or received)."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM transfers "
                "WHERE sender_account=? OR receiver_account=? "
                "ORDER BY created_at DESC LIMIT ?",
                (account_id, account_id, limit)
            )
            rows = cur.fetchall()
            transfers = []
            for row in rows:
                transfers.append({
                    "transfer_id": row["transfer_id"],
                    "sender_account": row["sender_account"],
                    "receiver_account": row["receiver_account"],
                    "amount_cents": row["amount_cents"],
                    "fee_cents": row["fee_cents"],
                    "status": row["status"],
                    "reference": row["reference"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })
            return {"success": True, "transfers": transfers}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  INTERNAL HELPERS                                                    #
    # ------------------------------------------------------------------ #

    def _audit(self, conn, event_type: str, user_id: str = None,
               account_id: str = None, transfer_id: str = None,
               detail: str = None):
        """Write an audit log entry (called within existing connection)."""
        conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), event_type, user_id, account_id,
             transfer_id, detail, _now_iso())
        )

    def ping(self) -> str:
        """Health check."""
        return "BDB OK"


def main():
    print("=" * 50)
    print("  Bank Database Server (BDB) starting...")
    print("=" * 50)
    init_db()

    daemon = Pyro5.server.Daemon()
    ns = Pyro5.api.locate_ns()
    uri = daemon.register(BDBService, objectId="banking.bdb.service")
    ns.register(BDB_SERVICE_NAME, uri)
    print(f"[BDB] Registered as '{BDB_SERVICE_NAME}' -> {uri}")
    print("[BDB] Ready. Waiting for requests from BAS server...")
    print("      (Press Ctrl+C to stop)\n")
    daemon.requestLoop()


if __name__ == "__main__":
    main()