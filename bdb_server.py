"""
bdb_server.py - Bank Database Server (BDB) — Data Tier
==========================================================================
Stores all persistent data via SQLite.
Exposed via Pyro5 to BAS server ONLY meaning BC client has no direct access.
Transfers are initiated by phone number (PAYID); BDB connects phone number to account_id.
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
    return hashlib.sha256(password.encode()).hexdigest()

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name     TEXT NOT NULL,
            created_at    TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            account_id    TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            phone_number  TEXT UNIQUE NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            account_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_active  INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            transfer_id       TEXT PRIMARY KEY,
            sender_account    TEXT NOT NULL,
            receiver_account  TEXT NOT NULL,
            sender_phone      TEXT NOT NULL,
            receiver_phone    TEXT NOT NULL,
            amount_cents      INTEGER NOT NULL,
            fee_cents         INTEGER NOT NULL,
            status            TEXT NOT NULL,
            reference         TEXT,
            idempotency_key   TEXT UNIQUE,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            FOREIGN KEY (sender_account)   REFERENCES accounts(account_id),
            FOREIGN KEY (receiver_account) REFERENCES accounts(account_id)
        )
    """)
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
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        _seed_mock_data(conn)
    conn.close()
    print(f"[BDB] Database initialised at '{DB_PATH}'")

def _seed_mock_data(conn):
    """
    these are the example accounts used in testing
    user_id, username, password, full_name, account_id, phone, balance_cents
    """
    now = _now_iso()
    mock = [
        ("USR001", "aaron", "aaron123", "Aaron Kalaji",  "ACC001", "0411111111", 100_000_000),
        ("USR002", "harry", "harry123", "Harry Stubbs",   "ACC002", "0422222222", 200_000_000),
        ("USR003", "kun",   "kun123",   "Kun Hu",     "ACC003", "0433333333",   999_999_999),
    ]
    for (uid, uname, pwd, fname, acc_id, phone, bal) in mock:
        conn.execute(
            "INSERT INTO users VALUES (?,?,?,?,?)",
            (uid, uname, _hash_password(pwd), fname, now)
        )
        conn.execute(
            "INSERT INTO accounts VALUES (?,?,?,?,?)",
            (acc_id, uid, phone, bal, now)
        )
    conn.commit()
    print("[BDB] data seeded:")
    for (_, uname, pwd, fname, acc_id, phone, bal) in mock:
        print(f"      {uname}/{pwd}  |  {acc_id}  |  {phone}  |  ${bal/100:,.2f}")

@Pyro5.api.expose
class BDBService:
    """
    Pyro5-exposed database service which is called only by BAS server.
    money values in integer cents.
    """

    # authentication of the session
    def validate_credentials(self, username: str, password: str) -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT u.user_id, u.username, u.full_name, "
                "a.account_id, a.phone_number "
                "FROM users u JOIN accounts a ON u.user_id = a.user_id "
                "WHERE u.username=? AND u.password_hash=?",
                (username, _hash_password(password))
            )
            row = cur.fetchone()
            if row is None:
                self._audit(conn, "LOGIN_FAIL", detail=f"username={username}")
                conn.commit()
                return {"success": False, "error": "Invalid username or password"}
            self._audit(conn, "LOGIN_OK", user_id=row["user_id"],
                        account_id=row["account_id"])
            conn.commit()
            return {
                "success":    True,
                "user_id":    row["user_id"],
                "username":   row["username"],
                "full_name":  row["full_name"],
                "account_id": row["account_id"],
                "phone_number": row["phone_number"],
            }
        finally:
            conn.close()

    def create_session(self, user_id: str, account_id: str) -> dict:
        token   = str(uuid.uuid4())
        now     = datetime.datetime.now()
        expires = (now + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        conn    = get_connection()
        try:
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,1)",
                (token, user_id, account_id,
                 now.isoformat(timespec="seconds"), expires)
            )
            conn.commit()
            return {"token": token, "expires_at": expires}
        finally:
            conn.close()

    def validate_session(self, token: str) -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.user_id, s.account_id, s.expires_at, s.is_active, "
                "u.username, u.full_name, a.phone_number "
                "FROM sessions s "
                "JOIN users    u ON s.user_id    = u.user_id "
                "JOIN accounts a ON s.account_id = a.account_id "
                "WHERE s.token=?",
                (token,)
            )
            row = cur.fetchone()
            if row is None:
                return {"valid": False, "error": "Token not found"}
            if not row["is_active"]:
                return {"valid": False, "error": "Session has been revoked"}
            if row["expires_at"] < _now_iso():
                return {"valid": False, "error": "Session expired"}
            return {
                "valid":        True,
                "user_id":      row["user_id"],
                "account_id":   row["account_id"],
                "username":     row["username"],
                "full_name":    row["full_name"],
                "phone_number": row["phone_number"],
            }
        finally:
            conn.close()

    def invalidate_session(self, token: str) -> bool:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE sessions SET is_active=0 WHERE token=?", (token,)
            )
            conn.commit()
            return True
        finally:
            conn.close()

    # balance check

    def get_balance(self, account_id: str) -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT balance_cents, phone_number "
                "FROM accounts WHERE account_id=?",
                (account_id,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False, "error": "Account not found"}
            return {
                "success":       True,
                "balance_cents": row["balance_cents"],
                "account_id":    account_id,
                "phone_number":  row["phone_number"],
            }
        finally:
            conn.close()

    # resolve phone number

    def resolve_phone(self, phone_number: str) -> dict:
        """
        Look up account by phone number then returns account_id
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT a.account_id, u.full_name "
                "FROM accounts a JOIN users u ON a.user_id = u.user_id "
                "WHERE a.phone_number=?",
                (phone_number,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False,
                        "error": f"No account registered to {phone_number}"}
            # Return not full name for privacy
            name  = row["full_name"]
            parts = name.split()
            masked = f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name
            return {
                "success":    True,
                "account_id": row["account_id"],
                "masked_name": masked,
            }
        finally:
            conn.close()

    def account_exists(self, account_id: str) -> bool:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM accounts WHERE account_id=?", (account_id,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    # transfers
    def create_transfer_record(self, sender_account: str, receiver_account: str,
                                sender_phone: str, receiver_phone: str,
                                amount_cents: int, fee_cents: int,
                                reference: str, idempotency_key: str) -> dict:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT transfer_id, status FROM transfers "
                "WHERE idempotency_key=?",
                (idempotency_key,)
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "success":     True,
                    "transfer_id": existing["transfer_id"],
                    "status":      existing["status"],
                    "duplicate":   True,
                }
            transfer_id = "TXN" + str(uuid.uuid4()).replace("-","")[:12].upper()
            now = _now_iso()
            conn.execute(
                "INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (transfer_id, sender_account, receiver_account,
                 sender_phone, receiver_phone,
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
        settle a transfer in a single SQLite transaction.
        debit sender (amount + fee), credit receiver (amount),
        update transfer status and rolls back entirely on any failure.
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status FROM transfers WHERE transfer_id=?",
                (transfer_id,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False, "error": "Transfer not found"}
            if row["status"] != STATUS_PENDING:
                return {"success": False,
                        "error": f"Transfer already {row['status']}"}

            cur.execute(
                "SELECT balance_cents FROM accounts WHERE account_id=?",
                (sender_account,)
            )
            sender_row = cur.fetchone()
            if sender_row is None:
                return {"success": False, "error": "Sender account not found"}

            total_debit = amount_cents + fee_cents
            if sender_row["balance_cents"] < total_debit:
                conn.execute(
                    "UPDATE transfers SET status=?, updated_at=? "
                    "WHERE transfer_id=?",
                    (STATUS_FAILED, _now_iso(), transfer_id)
                )
                self._audit(conn, "TRANSFER_FAILED", user_id=user_id,
                            account_id=sender_account,
                            transfer_id=transfer_id,
                            detail="Insufficient funds")
                conn.commit()
                return {"success": False, "error": "Insufficient funds",
                        "transfer_id": transfer_id, "status": STATUS_FAILED}

            now = _now_iso()
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents - ? "
                "WHERE account_id=?", (total_debit, sender_account)
            )
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ? "
                "WHERE account_id=?", (amount_cents, receiver_account)
            )
            conn.execute(
                "UPDATE transfers SET status=?, updated_at=? "
                "WHERE transfer_id=?",
                (STATUS_COMPLETED, now, transfer_id)
            )
            self._audit(conn, "TRANSFER_COMPLETED", user_id=user_id,
                        account_id=sender_account, transfer_id=transfer_id,
                        detail=f"amt={amount_cents}c fee={fee_cents}c to={receiver_account}")
            conn.commit()
            return {"success": True, "transfer_id": transfer_id,
                    "status": STATUS_COMPLETED}

        except Exception as e:
            conn.rollback()
            try:
                conn.execute(
                    "UPDATE transfers SET status=?, updated_at=? "
                    "WHERE transfer_id=?",
                    (STATUS_FAILED, _now_iso(), transfer_id)
                )
                conn.commit()
            except Exception:
                pass
            return {"success": False,
                    "error": f"Settlement error: {e}",
                    "transfer_id": transfer_id, "status": STATUS_FAILED}
        finally:
            conn.close()

    def get_transfer_status(self, transfer_id: str,
                             requesting_account: str) -> dict:
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
            if (row["sender_account"] != requesting_account and
                    row["receiver_account"] != requesting_account):
                return {"success": False,
                        "error": "Not authorised to view this transfer"}
            return {
                "success":          True,
                "transfer_id":      row["transfer_id"],
                "sender_account":   row["sender_account"],
                "receiver_account": row["receiver_account"],
                "sender_phone":     row["sender_phone"],
                "receiver_phone":   row["receiver_phone"],
                "amount_cents":     row["amount_cents"],
                "fee_cents":        row["fee_cents"],
                "status":           row["status"],
                "reference":        row["reference"],
                "created_at":       row["created_at"],
                "updated_at":       row["updated_at"],
            }
        finally:
            conn.close()

    def get_transfer_history(self, account_id: str, limit: int = 10) -> dict:
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
            return {
                "success": True,
                "transfers": [dict(r) for r in rows]
            }
        finally:
            conn.close()

    def _audit(self, conn, event_type, user_id=None,
               account_id=None, transfer_id=None, detail=None):
        conn.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), event_type, user_id,
             account_id, transfer_id, detail, _now_iso())
        )
    def ping(self) -> str:
        return "BDB OK"

def main():
    print("=" * 52)
    print("  Bank Database Server (BDB) starting...")
    print("=" * 52)
    init_db()
    daemon = Pyro5.server.Daemon()
    ns  = Pyro5.api.locate_ns()
    uri = daemon.register(BDBService, objectId="banking.bdb.service")
    ns.register(BDB_SERVICE_NAME, uri)
    print(f"[BDB] Registered as '{BDB_SERVICE_NAME}' → {uri}")
    print("[BDB] Ready. Waiting for BAS server requests...")
    print("      (Ctrl+C to stop)\n")
    daemon.requestLoop()

if __name__ == "__main__":
    main()