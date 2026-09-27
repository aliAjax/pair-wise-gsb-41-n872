"""补赔漏项复核的接口层。

编排角色校验、结案期限与补赔金额判断（supplement_rules）、
漏项申报与补赔版本的保存（supplement_store），供 HTTP 路由调用。
"""
from __future__ import annotations

from typing import Any

from supplement_rules import SupplementError, check_window, judge_amount, utcnow_str

SUBMIT_ROLES = {"surveyor", "adjuster"}  # 查勘人员
REVIEW_ROLES = {"supervisor"}            # 主管确认/退回
MANAGER_ROLES = {"intake", "supervisor", "auditor"}
FIELD_ROLES = {"surveyor", "adjuster"}


def _require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise SupplementError("角色无权执行：%s" % action, 403)


def _actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise SupplementError("缺少操作人")
    return actor


def _text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SupplementError("%s不能为空" % label)
    return text


def _amount(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise SupplementError("补赔估损金额必须是数值") from exc
    if amount <= 0:
        raise SupplementError("补赔估损金额必须大于 0")
    return amount


def _as_id(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SupplementError("%s无效" % label) from exc


class SupplementService:
    """漏项申报、主管确认、退回补件与查询的编排。"""

    def __init__(self, claims: Any, store: Any):
        self.claims = claims  # 主理赔服务，用于写时间线审计
        self.store = store

    def submit(self, actor: str, role: str, claim_id: Any, items: Any, amount: Any,
               reason: Any) -> dict:
        """查勘员在结案 30 天内提交漏项；同一案件仅留一项待主管确认。

        若本次补赔会使累计超过原估损，申报直接标记为退回补件，
        不占用该案件唯一的待确认名额。
        """
        actor = _actor(actor)
        _require_role(role, SUBMIT_ROLES, "提交漏项补赔")
        items = _text(items, "漏项内容")
        reason = _text(reason, "漏登原因")
        amount = _amount(amount)
        claim_id = _as_id(claim_id, "案件编号")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self.store.get_claim(conn, claim_id)
            if not claim:
                raise SupplementError("理赔案件不存在", 404)
            if claim["status"] != "approved":
                raise SupplementError("只有已结案赔付的案件才能提交漏项补赔", 409)
            check_window(claim["closed_at"] or claim["updated_at"])
            if self.store.pending_request(conn, claim_id):
                raise SupplementError("同一案件已有一项待主管确认的漏项，请先处理", 409)
            confirmed = self.store.confirmed_total(conn, claim_id)
            ok, remaining = judge_amount(claim["estimated_loss"], confirmed, amount)
            now = utcnow_str()
            if ok:
                status, note = "pending", None
            else:
                status = "returned"
                note = "补赔累计将超过原估损 %.2f（剩余额度 %.2f），退回补件" % (
                    claim["estimated_loss"], remaining)
            request_id = self.store.insert_request(
                conn, claim_id, items, amount, reason, status, actor, now, note)
            self.claims._audit(conn, claim_id, actor, "supplement.submitted",
                               {"request_id": request_id, "amount": amount, "status": status})
            return dict(self.store.get_request(conn, request_id))

    def confirm(self, actor: str, role: str, request_id: Any) -> dict:
        """主管确认：另存一条带原因的补赔版本，不改写原赔付记录。"""
        actor = _actor(actor)
        _require_role(role, REVIEW_ROLES, "确认漏项补赔")
        request_id = _as_id(request_id, "漏项申报编号")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._pending_request(conn, request_id, "确认")
            claim = self.store.get_claim(conn, req["claim_id"])
            now = utcnow_str()
            confirmed = self.store.confirmed_total(conn, req["claim_id"])
            ok, remaining = judge_amount(claim["estimated_loss"], confirmed, req["amount"])
            if not ok:
                note = "确认时补赔累计将超过原估损 %.2f（剩余额度 %.2f），退回补件" % (
                    claim["estimated_loss"], remaining)
                self.store.mark_request(conn, req["id"], "returned", note, actor, now)
                self.claims._audit(conn, req["claim_id"], actor, "supplement.returned",
                                   {"request_id": req["id"], "note": note})
                return {"request": dict(self.store.get_request(conn, req["id"])), "payment": None}
            version = self.store.next_version(conn, req["claim_id"])
            payment_id = self.store.insert_payment(
                conn, req["claim_id"], req["id"], version, req["amount"],
                req["items"], req["reason"], actor, now)
            self.store.mark_request(conn, req["id"], "confirmed", None, actor, now)
            self.claims._audit(conn, req["claim_id"], actor, "supplement.confirmed",
                               {"request_id": req["id"], "version": version, "amount": req["amount"]})
            return {"request": dict(self.store.get_request(conn, req["id"])),
                    "payment": dict(self.store.get_payment(conn, payment_id))}

    def send_back(self, actor: str, role: str, request_id: Any, note: Any) -> dict:
        """主管把待确认的漏项退回补件。"""
        actor = _actor(actor)
        _require_role(role, REVIEW_ROLES, "退回漏项补件")
        note = _text(note, "退回说明")
        request_id = _as_id(request_id, "漏项申报编号")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            req = self._pending_request(conn, request_id, "退回")
            self.store.mark_request(conn, req["id"], "returned", note, actor, utcnow_str())
            self.claims._audit(conn, req["claim_id"], actor, "supplement.returned",
                               {"request_id": req["id"], "note": note})
            return dict(self.store.get_request(conn, req["id"]))

    def _pending_request(self, conn, request_id: int, action: str):
        req = self.store.get_request(conn, request_id)
        if not req:
            raise SupplementError("漏项申报不存在", 404)
        if req["status"] != "pending":
            raise SupplementError("只有待主管确认的漏项可以%s" % action, 409)
        return req

    # ---- 查询 ----

    def _visible_claim_ids(self, conn, actor: str, role: str) -> list[int] | None:
        """返回可见案件 id 列表；None 表示全部可见。"""
        if role in MANAGER_ROLES:
            return None
        if role in FIELD_ROLES:
            rows = conn.execute(
                "SELECT id FROM claims WHERE assignee=? OR surveyor=?", (actor or "", actor or "")
            ).fetchall()
            return [r["id"] for r in rows]
        raise SupplementError("角色无权查看补赔信息", 403)

    def overview(self, actor: str, role: str, claim_id: Any = None) -> dict:
        with self.store.connect() as conn:
            visible = self._visible_claim_ids(conn, actor, role)
            if claim_id in (None, ""):
                ids = visible
            else:
                ids = [_as_id(claim_id, "案件编号")]
                if visible is not None and ids[0] not in visible:
                    raise SupplementError("角色无权查看该案件的补赔信息", 403)
            return {"supplements": self.store.list_requests(ids),
                    "supplement_payments": self.store.list_payments(ids)}

    def todos(self, actor: str, role: str, claim_ids: list[int] | None = None) -> dict:
        """待办：主管看待确认项，查勘员看自己待确认与被退回补件的项。"""
        result = {"pending": [], "returned": []}
        if role in MANAGER_ROLES:
            result["pending"] = self.store.list_requests(claim_ids, status="pending")
        elif role in FIELD_ROLES:
            mine = self.store.list_requests(claim_ids, surveyor=(actor or "").strip())
            result["pending"] = [r for r in mine if r["status"] == "pending"]
            result["returned"] = [r for r in mine if r["status"] == "returned"]
        else:
            raise SupplementError("角色无权查看补赔待办", 403)
        return result

    def state_extras(self, actor: str, role: str, claim_ids: list[int]) -> dict:
        """附加到 GET /api/state 的漏项、补赔版本与待办数据。"""
        return {
            "supplements": self.store.list_requests(claim_ids),
            "supplement_payments": self.store.list_payments(claim_ids),
            "supplement_todos": self.todos(actor, role, claim_ids),
        }


def seed_demo(claims: Any, supplements: SupplementService) -> dict:
    """演示数据：把 CLM-DEMO-001 走完赔付流程结案，再提交一条漏项申报。"""
    rows = [c for c in claims.state("seed", "supervisor")["claims"]
            if c["claim_no"] == "CLM-DEMO-001"]
    if not rows or rows[0]["status"] != "received":
        return {"supplement_seeded": False}
    c = rows[0]
    c = claims.triage_claim("sup-demo", "supervisor", c["id"], c["version"], 0.1, True)
    c = claims.assign_claim("sup-demo", "supervisor", c["id"], "adjuster-demo", c["version"], "surveyor-demo")
    c = claims.record_survey("surveyor-demo", "surveyor", c["id"], 0.6,
                             "房屋进水，家电与设备受损", "按核定赔付", c["version"])
    c = claims.submit_review("surveyor-demo", "surveyor", c["id"], c["version"])
    c = claims.finalize_claim("sup-demo", "supervisor", c["id"], "approve", 320000, c["version"])
    req = supplements.submit("surveyor-demo", "surveyor", c["id"],
                             "地下室抽水机与备用发电机", 18000, "查勘时地下室积水未退，漏登受损设备")
    return {"supplement_seeded": True, "request_id": req["id"], "claim_id": c["id"]}
