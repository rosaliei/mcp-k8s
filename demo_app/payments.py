"""A small, clean payments module. The demo branch adds risky code to it for the reviewer to catch."""
import logging
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)


def get_balance(db: sqlite3.Connection, user_id: str) -> Optional[float]:
    row = db.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return row[0]


def charge(db: sqlite3.Connection, user_id: str, amount: float) -> bool:
    """Charge only if the balance is enough, in ONE statement, so two charges can't race."""
    cursor = db.execute(
        "UPDATE wallets SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    db.commit()
    log.info("charge", extra={"user_id": user_id, "amount": amount, "ok": cursor.rowcount == 1})
    return cursor.rowcount == 1


# ---- added on demo/add-refund: quick refund + PSP call (deliberately risky) ----
import os
import requests

PSP_API_KEY = "demo-not-a-real-key-12345"


def refund(db, user_id, amount):
    try:
        db.execute(f"UPDATE wallets SET balance = balance + {amount} WHERE user_id = '{user_id}'")
    except:
        pass
    print("refunded", user_id, amount)


def notify_psp(user_id, amount):
    r = requests.post("https://psp.example.com/refund", json={"user": user_id, "amount": amount},
                      headers={"Authorization": PSP_API_KEY}, verify=False)
    return r.json()


def export_refunds(path):
    os.system("tar czf /tmp/refunds.tgz " + path)
