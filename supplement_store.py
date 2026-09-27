"""补赔漏项申报与补赔版本的保存层（SQLite）。

supplement_requests 保存查勘员提交的漏项申报；
supplement_payments 保存主管确认后的补赔版本（另存新记录，
原 payments 赔付记录只读不改写）。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS supplement_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    items TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    review_note TEXT,
    surveyor TEXT NOT NULL,
    reviewed_by TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_supplement_requests_claim ON supplement_requests(claim_id, status);
CREATE TABLE IF NOT EXISTS supplement_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    request_id INTEGER NOT NULL REFERENCES supplement_requests(id),
    version INTEGER NOT NULL,
    amount REAL NOT NULL,
    items TEXT NOT NULL,
    reason TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(claim_id, version)
);
"""


class SupplementStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # ---- 案件只读 ----

    def get_claim(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()

    def pending_request(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM supplement_requests WHERE claim_id=? AND status='pending'", (claim_id,)
        ).fetchone()

    def confirmed_total(self, conn: sqlite3.Connection, claim_id: int) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM supplement_payments WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return float(row["total"])

    def next_version(self, conn: sqlite3.Connection, claim_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM supplement_payments WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return int(row["v"])

    # ---- 写入 ----

    def insert_request(self, conn: sqlite3.Connection, claim_id: int, items: str, amount: float,
                       reason: str, status: str, surveyor: str, now: str,
                       review_note: str | None = None) -> int:
        cur = conn.execute(
            """INSERT INTO supplement_requests(claim_id,items,amount,reason,status,review_note,surveyor,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (claim_id, items, amount, reason, status, review_note, surveyor, now),
        )
        return cur.lastrowid

    def mark_request(self, conn: sqlite3.Connection, request_id: int, status: str,
                     note: str | None, reviewer: str, now: str) -> None:
        conn.execute(
            "UPDATE supplement_requests SET status=?,review_note=?,reviewed_by=?,reviewed_at=? WHERE id=?",
            (status, note, reviewer, now, request_id),
        )

    def insert_payment(self, conn: sqlite3.Connection, claim_id: int, request_id: int, version: int,
                       amount: float, items: str, reason: str, approver: str, now: str) -> int:
        cur = conn.execute(
            """INSERT INTO supplement_payments(claim_id,request_id,version,amount,items,reason,approved_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (claim_id, request_id, version, amount, items, reason, approver, now),
        )
        return cur.lastrowid

    # ---- 查询 ----

    def get_request(self, conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM supplement_requests WHERE id=?", (request_id,)).fetchone()

    def get_payment(self, conn: sqlite3.Connection, payment_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM supplement_payments WHERE id=?", (payment_id,)).fetchone()

    def list_requests(self, claim_ids: list[int] | None = None, status: str | None = None,
                      surveyor: str | None = None) -> list[dict]:
        sql = "SELECT * FROM supplement_requests"
        cond, args = [], []
        if claim_ids is not None:
            if not claim_ids:
                return []
            cond.append("claim_id IN (%s)" % ",".join("?" for _ in claim_ids))
            args.extend(claim_ids)
        if status:
            cond.append("status=?")
            args.append(status)
        if surveyor:
            cond.append("surveyor=?")
            args.append(surveyor)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def list_payments(self, claim_ids: list[int] | None = None) -> list[dict]:
        sql = "SELECT * FROM supplement_payments"
        args: list = []
        if claim_ids is not None:
            if not claim_ids:
                return []
            sql += " WHERE claim_id IN (%s)" % ",".join("?" for _ in claim_ids)
            args.extend(claim_ids)
        sql += " ORDER BY claim_id, version"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
