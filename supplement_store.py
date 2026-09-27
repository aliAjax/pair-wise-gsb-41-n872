"""漏项补赔的持久化层：只负责 SQLite 的建表与读写，不含业务规则。

原赔付记录（claims.final_payout、既有 payments 行）在本层完全不会被修改；
每次漏项补登都另存一个 supplements 版本，携带原因与原案快照。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catastrophe_claims.db")


class SupplementStore:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS supplements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplement_no TEXT NOT NULL UNIQUE,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    item_description TEXT NOT NULL,
                    estimated_amount REAL NOT NULL,
                    confirmed_amount REAL,
                    reason TEXT NOT NULL,
                    original_estimate REAL NOT NULL,
                    original_payout REAL NOT NULL,
                    confirmed_before REAL NOT NULL DEFAULT 0,
                    submitter TEXT NOT NULL,
                    reviewed_by TEXT,
                    review_note TEXT,
                    payment_id INTEGER REFERENCES payments(id),
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    UNIQUE(claim_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_supplements_claim ON supplements(claim_id, id);
                CREATE INDEX IF NOT EXISTS idx_supplements_status ON supplements(status, created_at);
                -- 同一案件仅允许一项待主管确认（返回/拒绝/确认后可重新提交）
                CREATE UNIQUE INDEX IF NOT EXISTS uq_supplements_one_pending
                    ON supplements(claim_id) WHERE status='pending';
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def pending_for(self, conn: sqlite3.Connection, claim_id: int) -> dict[str, Any] | None:
        return self._row(conn.execute(
            "SELECT * FROM supplements WHERE claim_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (claim_id,),
        ).fetchone())

    def next_seq(self, conn: sqlite3.Connection, claim_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM supplements WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        return int(row["next_seq"])

    def confirmed_total(self, conn: sqlite3.Connection, claim_id: int) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(confirmed_amount),0) AS total FROM supplements WHERE claim_id=? AND status='confirmed'",
            (claim_id,),
        ).fetchone()
        return float(row["total"])

    def insert(self, conn: sqlite3.Connection, *, supplement_no: str, claim_id: int, seq: int,
               status: str, item_description: str, estimated_amount: float, reason: str,
               original_estimate: float, original_payout: float, confirmed_before: float,
               submitter: str, created_at: str) -> dict[str, Any]:
        try:
            cur = conn.execute(
                """INSERT INTO supplements(supplement_no,claim_id,seq,status,item_description,estimated_amount,
                   reason,original_estimate,original_payout,confirmed_before,submitter,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (supplement_no, claim_id, seq, status, item_description, estimated_amount,
                 reason, original_estimate, original_payout, confirmed_before, submitter, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该案件已有待主管确认的漏项，不能重复提交") from exc
        return self.get(conn, cur.lastrowid)  # type: ignore[return-value]

    def get(self, conn: sqlite3.Connection, supplement_id: int) -> dict[str, Any] | None:
        return self._row(conn.execute("SELECT * FROM supplements WHERE id=?", (supplement_id,)).fetchone())

    def get_by_no(self, conn: sqlite3.Connection, supplement_no: str) -> dict[str, Any] | None:
        return self._row(conn.execute("SELECT * FROM supplements WHERE supplement_no=?", (supplement_no.strip(),)).fetchone())

    def list(self, conn: sqlite3.Connection, *, claim_id: int | None = None,
             status: str | None = None, submitter: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM supplements WHERE 1=1", []
        if claim_id is not None:
            sql += " AND claim_id=?"
            params.append(claim_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if submitter:
            sql += " AND submitter=?"
            params.append(submitter)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def mark_confirmed(self, conn: sqlite3.Connection, supplement_id: int, *,
                       confirmed_amount: float, reviewed_by: str, review_note: str,
                       payment_id: int, reviewed_at: str) -> None:
        conn.execute(
            """UPDATE supplements SET status='confirmed',confirmed_amount=?,reviewed_by=?,review_note=?,
               payment_id=?,reviewed_at=? WHERE id=? AND status='pending'""",
            (confirmed_amount, reviewed_by, review_note, payment_id, reviewed_at, supplement_id),
        )

    def mark_rejected(self, conn: sqlite3.Connection, supplement_id: int, *,
                      reviewed_by: str, review_note: str, reviewed_at: str) -> None:
        conn.execute(
            """UPDATE supplements SET status='rejected',reviewed_by=?,review_note=?,reviewed_at=?
               WHERE id=? AND status='pending'""",
            (reviewed_by, review_note, reviewed_at, supplement_id),
        )

    def add_supplement_payment(self, conn: sqlite3.Connection, *, claim_id: int, amount: float,
                               approved_by: str, reference: str, created_at: str) -> int:
        try:
            cur = conn.execute(
                "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, "supplement", amount, approved_by, reference, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("付款参考号已存在") from exc
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def append_timeline(self, conn: sqlite3.Connection, *, claim_id: int, actor: str,
                        action: str, details: dict[str, Any], created_at: str) -> None:
        conn.execute(
            "INSERT INTO timeline(claim_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (claim_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), created_at),
        )

    def finalized_at(self, conn: sqlite3.Connection, claim_id: int) -> str | None:
        """结案时间：取最终核定时间线，缺失时回退到案件 updated_at。"""
        row = conn.execute(
            "SELECT created_at FROM timeline WHERE claim_id=? AND action='claim.finalized' ORDER BY id DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
        if row:
            return row["created_at"]
        claim = conn.execute("SELECT status,updated_at FROM claims WHERE id=?", (claim_id,)).fetchone()
        return claim["updated_at"] if claim else None


class ConflictError(Exception):
    """并发写入或唯一约束冲突（如同一案件出现两项待办）。"""
