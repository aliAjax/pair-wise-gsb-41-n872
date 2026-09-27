"""漏项补赔业务编排：组合 supplement_rules（判断）与 supplement_store（保存）。

规则要点：
- 查勘员在结案 30 天内提交漏项描述、估损金额、漏登原因；
- 同一案件仅允许一项待主管确认的补赔版本；
- 补赔累计不得超过原估损，超出则本次补登退回补件（returned），可修改后重新提交；
- 原赔付记录不改写，确认时另写 payments(kind='supplement') 与带原因的补赔版本。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from supplement_rules import (
    CLOSED_STATUSES,
    PENDING,
    RETURNED,
    SUBMITTER_ROLES,
    SUPERVISOR_ROLES,
    evaluate_supplement_amount,
    parse_amount,
    within_submission_window,
)
from supplement_store import ConflictError, SupplementStore

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catastrophe_claims.db")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def actor_id(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def utcnow() -> str:
    from app import utcnow as _utcnow  # 延迟导入，避免与 app 形成循环依赖
    return _utcnow()


class SupplementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.store = SupplementStore(db_path)
        self.db_path = str(db_path)

    # -- 内部工具 ----------------------------------------------------------

    def _claim(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("理赔案件不存在", 404)
        return row

    @staticmethod
    def _audit(conn: sqlite3.Connection, store: SupplementStore, claim_id: int, actor: str,
               action: str, details: dict[str, Any]) -> None:
        store.append_timeline(conn, claim_id=claim_id, actor=actor, action=action,
                              details=details, created_at=utcnow())

    # -- 查勘员：提交漏项 ---------------------------------------------------

    def submit_missing_item(self, actor: str, role: str, claim_id: int,
                            item_description: str, estimated_amount: Any,
                            reason: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, SUBMITTER_ROLES, "补登漏项")
        item_description = (item_description or "").strip()
        reason = (reason or "").strip()
        if not item_description:
            raise DomainError("漏登物品描述不能为空")
        if not reason:
            raise DomainError("漏登原因不能为空")
        try:
            estimated_amount = parse_amount(estimated_amount, "漏项估损")
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        if estimated_amount <= 0:
            raise DomainError("漏项估损必须大于0")

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, int(claim_id))
            if claim["status"] not in CLOSED_STATUSES:
                raise DomainError("只有已结案（approved/closed）案件可以补登漏项", 409)
            if self.store.pending_for(conn, claim["id"]):
                raise DomainError("同一案件已有一项待主管确认的漏项", 409)

            closed_at = self.store.finalized_at(conn, claim["id"])
            if closed_at is None or not within_submission_window(closed_at):
                raise DomainError("已超过结案后30天补登期限", 409)

            confirmed_before = self.store.confirmed_total(conn, claim["id"])
            verdict = evaluate_supplement_amount(
                float(claim["estimated_loss"]), confirmed_before, estimated_amount,
            )
            status = PENDING if verdict["within_cap"] else RETURNED
            seq = self.store.next_seq(conn, claim["id"])
            supplement_no = "SUP-%s-%03d" % (claim["claim_no"], seq)
            try:
                record = self.store.insert(
                    conn,
                    supplement_no=supplement_no,
                    claim_id=claim["id"],
                    seq=seq,
                    status=status,
                    item_description=item_description,
                    estimated_amount=estimated_amount,
                    reason=reason,
                    original_estimate=float(claim["estimated_loss"]),
                    original_payout=float(claim["final_payout"] or 0),
                    confirmed_before=confirmed_before,
                    submitter=actor,
                    created_at=utcnow(),
                )
            except ConflictError as exc:
                raise DomainError("同一案件已有一项待主管确认的漏项", 409) from exc
            action = "supplement.submitted" if status == PENDING else "supplement.returned"
            self._audit(conn, self.store, claim["id"], actor, action, {
                "supplement_no": supplement_no,
                "seq": seq,
                "status": status,
                "estimated_amount": estimated_amount,
                "room": verdict["room"],
                "overflow": verdict["overflow"],
                "reason": reason,
            })
            return {
                "supplement": record,
                "verdict": verdict,
                "message": ("已提交，待主管确认" if status == PENDING
                            else "累计补赔将超过原估损 %s 元，退回补件" % verdict["overflow"]),
            }

    # -- 主管：确认 / 拒认 ---------------------------------------------------

    def _pending(self, conn: sqlite3.Connection, supplement_id: int) -> dict[str, Any]:
        record = self.store.get(conn, int(supplement_id))
        if not record:
            raise DomainError("补赔记录不存在", 404)
        if record["status"] != PENDING:
            raise DomainError("该漏项不是待确认状态（当前：%s）" % record["status"], 409)
        return record

    def confirm_supplement(self, actor: str, role: str, supplement_id: int,
                           confirmed_amount: Any | None = None, review_note: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, SUPERVISOR_ROLES, "确认漏项补赔")
        review_note = (review_note or "").strip()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._pending(conn, supplement_id)
            if confirmed_amount is None:
                confirmed_amount = record["estimated_amount"]
            try:
                confirmed_amount = parse_amount(confirmed_amount, "补赔金额")
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
            if confirmed_amount <= 0:
                raise DomainError("补赔金额必须大于0")

            # 主管确认时再次核对累计上限（可小于等于估损金额，确认部分补赔）
            confirmed_before = self.store.confirmed_total(conn, record["claim_id"])
            verdict = evaluate_supplement_amount(
                record["original_estimate"], confirmed_before, confirmed_amount,
            )
            if not verdict["within_cap"]:
                raise DomainError(
                    "补赔累计不能超过原估损：剩余额度 %s 元，本次申请 %s 元，请退回补件"
                    % (verdict["room"], confirmed_amount),
                    409,
                )

            reference = "SUP-PAY-%d" % record["id"]
            try:
                payment_id = self.store.add_supplement_payment(
                    conn,
                    claim_id=record["claim_id"],
                    amount=confirmed_amount,
                    approved_by=actor,
                    reference=reference,
                    created_at=utcnow(),
                )
            except ConflictError as exc:
                raise DomainError("补赔付款参考号冲突，请重试", 409) from exc
            self.store.mark_confirmed(
                conn, record["id"], confirmed_amount=confirmed_amount, reviewed_by=actor,
                review_note=review_note, payment_id=payment_id, reviewed_at=utcnow(),
            )
            updated = self.store.get(conn, record["id"])
            self._audit(conn, self.store, record["claim_id"], actor, "supplement.confirmed", {
                "supplement_no": record["supplement_no"],
                "confirmed_amount": confirmed_amount,
                "payment_reference": reference,
                "note": review_note,
            })
            return {"supplement": updated, "payment_id": payment_id, "payment_reference": reference}

    def reject_supplement(self, actor: str, role: str, supplement_id: int,
                          review_note: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, SUPERVISOR_ROLES, "拒认漏项补赔")
        review_note = (review_note or "").strip()
        if not review_note:
            raise DomainError("拒认必须填写理由")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._pending(conn, supplement_id)
            self.store.mark_rejected(conn, record["id"], reviewed_by=actor,
                                     review_note=review_note, reviewed_at=utcnow())
            updated = self.store.get(conn, record["id"])
            self._audit(conn, self.store, record["claim_id"], actor, "supplement.rejected", {
                "supplement_no": record["supplement_no"],
                "note": review_note,
            })
            return {"supplement": updated}

    # -- 查询：调度页/接口 ---------------------------------------------------

    def list_supplements(self, actor: str = "", role: str = "viewer",
                         claim_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看补赔记录", 403)
        with self.store.connect() as conn:
            submitter = actor if role in {"adjuster", "surveyor"} else None
            records = self.store.list(conn, claim_id=claim_id, status=status, submitter=submitter)
            if not records:
                return []
            # 附带原案关键信息，便于调度页同时看到原案、漏项、补赔金额、待办
            ids = sorted({r["claim_id"] for r in records})
            marks = ",".join("?" for _ in ids)
            claims = {
                row["id"]: dict(row)
                for row in conn.execute(
                    "SELECT id,claim_no,region,peril_type,policy_no,status,estimated_loss,final_payout FROM claims WHERE id IN (%s)" % marks,
                    ids,
                ).fetchall()
            }
        for record in records:
            record["claim"] = claims.get(record["claim_id"])
            record["pending"] = record["status"] == PENDING
        return records

    def get_supplement(self, supplement_id: int, role: str = "viewer") -> dict[str, Any]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看补赔记录", 403)
        with self.store.connect() as conn:
            record = self.store.get(conn, int(supplement_id))
            if not record:
                raise DomainError("补赔记录不存在", 404)
            claim = conn.execute(
                "SELECT id,claim_no,region,peril_type,policy_no,status,estimated_loss,final_payout FROM claims WHERE id=?",
                (record["claim_id"],),
            ).fetchone()
        record["claim"] = dict(claim) if claim else None
        record["pending"] = record["status"] == PENDING
        return record

    def todo(self, actor: str = "", role: str = "supervisor") -> list[dict[str, Any]]:
        """主管待办：所有待确认漏项；查勘员看待自己名下待办/退回件。"""
        if role in SUPERVISOR_ROLES:
            return self.list_supplements(actor, role, status=PENDING)
        if role in SUBMITTER_ROLES:
            return [r for r in self.list_supplements(actor, role) if r["status"] in {PENDING, RETURNED}]
        raise DomainError("角色无权查看补赔待办", 403)
