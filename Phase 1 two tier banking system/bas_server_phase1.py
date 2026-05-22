import Pyro5.api
import Pyro5.server
import hashlib
import uuid
import json
import os
import math
import re
from datetime import datetime, timedelta

# Fee tier logic (same as shared.py in Phase 2)
FEE_TIERS = [
    (0,         200_000,    0.0000,  0),
    (200_000,   1_000_000,  0.0025,  2_000),
    (1_000_000, 2_000_000,  0.0020,  2_500),
    (2_000_000, 5_000_000,  0.00125, 4_000),
    (5_000_000, 10_000_000, 0.0008,  6_000),
    (10_000_000, float('inf'), 0.0006, 20_000),
]

def round_half_up(x):
    return math.floor(x + 0.5)

def calculate_fee_cents(amount_cents):
    for min_ex, max_in, rate, cap in FEE_TIERS:
        if min_ex < amount_cents <= max_in:
            raw = round_half_up(amount_cents * rate)
            return min(raw, cap)
    return 0

# placeholder accounts

USERS = {
    "aaron": {"password_hash": hashlib.sha256("aaron123".encode()).hexdigest(), "full_name": "Aaron Kalaji",  "account_id": "ACC001", "phone": "0411111111", "balance_cents": 1_000_000},
    "harry": {"password_hash": hashlib.sha256("harry123".encode()).hexdigest(), "full_name": "Harry Evans",   "account_id": "ACC002", "phone": "0422222222", "balance_cents": 2_000_000},
    "kun":   {"password_hash": hashlib.sha256("kun123".encode()).hexdigest(),   "full_name": "Kun Zhang",     "account_id": "ACC003", "phone": "0433333333", "balance_cents": 9_999_999},
}

PHONE_INDEX = {u["phone"]: uname for uname, u in USERS.items()}
SESSIONS = {}       # token, username, expires_at
TRANSFERS = {}
IDEMPOTENCY = {}

PERSISTENCE_FILE = "phase1_state.json"

# helpers
def save_state():
    state = {
        "balances": {u: USERS[u]["balance_cents"] for u in USERS},
        "transfers": TRANSFERS,
    }
    with open(PERSISTENCE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_state():
    if not os.path.exists(PERSISTENCE_FILE):
        return
    with open(PERSISTENCE_FILE, "r") as f:
        state = json.load(f)
    for uname, bal in state.get("balances", {}).items():
        if uname in USERS:
            USERS[uname]["balance_cents"] = bal
    for tid, tx in state.get("transfers", {}).items():
        TRANSFERS[tid] = tx
        if tx.get("idempotency_key"):
            IDEMPOTENCY[tx["idempotency_key"]] = tid

def mask_name(full_name):
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0]}."
    return full_name

def normalise_phone(phone):
    return re.sub(r"\s+", "", phone)

# BAS Phase 1 remote object
@Pyro5.api.expose
class BankingAppServer:

    def _validate_session(self, token):
        s = SESSIONS.get(token)
        if not s:
            return None
        if datetime.fromisoformat(s["expires_at"]) < datetime.now():
            del SESSIONS[token]
            return None
        return s["username"]

    def login(self, username, password):
        user = USERS.get(username)
        if not user:
            return {"success": False, "error": "Invalid username or password."}
        ph = hashlib.sha256(password.encode()).hexdigest()
        if ph != user["password_hash"]:
            return {"success": False, "error": "Invalid username or password."}
        token = str(uuid.uuid4())
        SESSIONS[token] = {
            "username": username,
            "expires_at": (datetime.now() + timedelta(hours=1)).isoformat(),
        }
        return {"success": True, "token": token, "account_id": user["account_id"]}

    def logout(self, token):
        SESSIONS.pop(token, None)
        return {"success": True}

    def get_balance(self, token):
        username = self._validate_session(token)
        if not username:
            return {"success": False, "error": "Invalid or expired session."}
        user = USERS[username]
        return {
            "success": True,
            "account_id": user["account_id"],
            "phone": user["phone"],
            "balance_cents": user["balance_cents"],
        }

    def resolve_payid(self, token, phone):
        if not self._validate_session(token):
            return {"success": False, "error": "Invalid or expired session."}
        phone = normalise_phone(phone)
        if not re.fullmatch(r"04\d{8}", phone):
            return {"success": False, "error": "Not a valid Australian mobile number."}
        uname = PHONE_INDEX.get(phone)
        if not uname:
            return {"success": False, "error": "No account registered to that number."}
        return {"success": True, "masked_name": mask_name(USERS[uname]["full_name"]), "phone": phone}

    def submit_transfer(self, token, recipient_phone, amount_cents, reference, idempotency_key):
        username = self._validate_session(token)
        if not username:
            return {"success": False, "error": "Invalid or expired session."}

        if idempotency_key in IDEMPOTENCY:
            return {"success": True, "transfer": TRANSFERS[IDEMPOTENCY[idempotency_key]]}

        sender = USERS[username]
        recipient_phone = normalise_phone(recipient_phone)

        if sender["phone"] == recipient_phone:
            return {"success": False, "error": "Cannot transfer to your own account."}

        r_uname = PHONE_INDEX.get(recipient_phone)
        if not r_uname:
            return {"success": False, "error": "Recipient not found."}

        fee = calculate_fee_cents(amount_cents)
        total_debit = amount_cents + fee

        if sender["balance_cents"] < total_debit:
            return {"success": False, "error": "Insufficient funds.", "status": "FAILED"}

        sender["balance_cents"] -= total_debit
        USERS[r_uname]["balance_cents"] += amount_cents

        transfer_id = "TXN" + str(uuid.uuid4()).replace("-", "")[:12].upper()
        tx = {
            "transfer_id": transfer_id,
            "sender": username,
            "receiver": r_uname,
            "amount_cents": amount_cents,
            "fee_cents": fee,
            "status": "COMPLETED",
            "reference": reference,
            "idempotency_key": idempotency_key,
            "created_at": datetime.now().isoformat(),
        }
        TRANSFERS[transfer_id] = tx
        IDEMPOTENCY[idempotency_key] = transfer_id
        save_state()
        return {"success": True, "transfer": tx}

    def get_transfer_status(self, token, transfer_id):
        username = self._validate_session(token)
        if not username:
            return {"success": False, "error": "Invalid or expired session."}
        tx = TRANSFERS.get(transfer_id)
        if not tx:
            return {"success": False, "error": "Transfer not found."}
        if tx["sender"] != username and tx["receiver"] != username:
            return {"success": False, "error": "Not authorised to view this transfer."}
        return {"success": True, "transfer": tx}

    def get_transfer_history(self, token):
        username = self._validate_session(token)
        if not username:
            return {"success": False, "error": "Invalid or expired session."}
        history = [t for t in TRANSFERS.values() if t["sender"] == username or t["receiver"] == username]
        history.sort(key=lambda x: x["created_at"], reverse=True)
        return {"success": True, "transfers": history[:10]}

    def calculate_fee(self, token, amount_cents):
        if not self._validate_session(token):
            return {"success": False, "error": "Invalid or expired session."}
        return {"success": True, "fee_cents": calculate_fee_cents(amount_cents)}

if __name__ == "__main__":
    load_state()
    daemon = Pyro5.server.Daemon()
    ns = Pyro5.api.locate_ns()
    uri = daemon.register(BankingAppServer, "banking.bas")
    ns.register("banking.bas", uri)
    print("BAS Phase 1 ready (in-memory + file persistence).")
    daemon.requestLoop()