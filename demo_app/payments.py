"""
A small, CLEAN payments module. It exists only so the code reviewer has something to review.

On the branch demo/add-refund, risky code is added to this file (hardcoded key, SQL injection,
no timeout, TLS certificate checks turned off, shell injection). Run the reviewer there:
    git switch demo/add-refund && python code_review/review.py --diff main && git switch main

This clean version shows the SAFE way to do the same things, so you can compare:
  - parameterised SQL (the ? placeholders) instead of pasting values into the SQL text
  - one atomic UPDATE instead of "read the balance, check it, then write it" (a race condition)
  - logging with fields instead of print()
"""
import logging
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)


def get_balance(db: sqlite3.Connection, user_id: str) -> Optional[float]:
    # The ? is a placeholder. The database driver sends user_id separately from the SQL text,
    # so a malicious user_id like  "x' OR '1'='1"  can't change the query (no SQL injection).
    row = db.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:                   # user has no wallet: return None instead of crashing
        return None
    return row[0]


def charge(db: sqlite3.Connection, user_id: str, amount: float) -> bool:
    """
    Charge only if the balance is enough, in ONE statement.
    Unsafe version: read balance -> if balance >= amount -> update. Two requests at the same time
    can both pass the check and overdraw the wallet. Doing the check inside the UPDATE avoids that.
    """
    cursor = db.execute(
        "UPDATE wallets SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    db.commit()
    charged = cursor.rowcount == 1    # 1 row changed = charged; 0 rows = not enough balance
    log.info("charge", extra={"user_id": user_id, "amount": amount, "ok": charged})
    return charged
