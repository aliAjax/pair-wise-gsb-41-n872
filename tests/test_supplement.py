import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402
from supplement_rules import (  # noqa: E402
    evaluate_supplement_amount,
    parse_amount,
    within_submission_window,
)
from supplement_service import DomainError as SupplementError  # noqa: E402
from supplement_service import SupplementService  # noqa: E402


class SupplementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "test.db"
        self.service = CatastropheClaimService(db)
        self.supplements = SupplementService(db)

    def tearDown(self):
        self.tmp.cleanup()

    def approved_claim(self, number="SC-001", loss=500000, payout=280000):
        c = self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", "P-" + number,
            "R-" + number, 30.1, 121.1, loss, False, True,
        )
        c = self.service.triage_claim("sup1", "supervisor", c["id"], c["version"], 0.1)
        c = self.service.assign_claim("sup1", "supervisor", c["id"], "adjuster1", c["version"], "surveyor1")
        c = self.service.record_survey("adjuster1", "adjuster", c["id"], 0.6, "结构受损", "部分赔付", c["version"])
        c = self.service.submit_review("adjuster1", "adjuster", c["id"], c["version"])
        c = self.service.finalize_claim("sup1", "supervisor", c["id"], "approve", payout, c["version"])
        return c

    def test_submit_confirm_full_flow_keeps_original_records(self):
        c = self.approved_claim()
        result = self.supplements.submit_missing_item(
            "surveyor1", "surveyor", c["id"], "冷库压缩机", 50000, "清点时被废墟覆盖",
        )
        self.assertEqual("pending", result["supplement"]["status"])
        self.assertTrue(result["verdict"]["within_cap"])
        # 版本号自增、快照带原案金额与原因
        self.assertEqual(1, result["supplement"]["seq"])
        self.assertEqual(500000, result["supplement"]["original_estimate"])
        self.assertEqual(280000, result["supplement"]["original_payout"])

        sup_id = result["supplement"]["id"]
        confirmed = self.supplements.confirm_supplement("sup1", "supervisor", sup_id)
        self.assertEqual("confirmed", confirmed["supplement"]["status"])
        self.assertEqual(50000, confirmed["supplement"]["confirmed_amount"])

        # 原赔付记录不改写：final_payout 与案件状态保持结案值
        fresh = self.service.state("sup1", "supervisor")
        claim = next(x for x in fresh["claims"] if x["id"] == c["id"])
        self.assertEqual(280000, claim["final_payout"])
        self.assertEqual("approved", claim["status"])
        # 补赔另存为独立付款，原付款记录仍在
        kinds = sorted(p["kind"] for p in fresh["payments"] if p["claim_id"] == c["id"])
        self.assertEqual(["supplement"], kinds)
        payment = next(p for p in fresh["payments"] if p["claim_id"] == c["id"])
        self.assertEqual(50000, payment["amount"])
        self.assertEqual(confirmed["payment_id"], payment["id"])

    def test_only_one_pending_per_claim(self):
        c = self.approved_claim()
        self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "漏项A", 10000, "原因A")
        with self.assertRaises(SupplementError) as ctx:
            self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "漏项B", 20000, "原因B")
        self.assertEqual(409, ctx.exception.status)
        # 主管确认后可再提交下一项
        pending = self.supplements.list_supplements("sup1", "supervisor", c["id"], "pending")[0]
        self.supplements.confirm_supplement("sup1", "supervisor", pending["id"])
        second = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "漏项B", 20000, "原因B")
        self.assertEqual("pending", second["supplement"]["status"])
        self.assertEqual(2, second["supplement"]["seq"])

    def test_cumulative_above_original_estimate_is_returned_and_can_resubmit(self):
        c = self.approved_claim(loss=500000, payout=450000)
        # 先确认补 480000（累计在原估损 500000 内）
        first = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "发电机", 480000, "漏登")
        self.assertEqual("pending", first["supplement"]["status"])
        self.supplements.confirm_supplement("sup1", "supervisor", first["supplement"]["id"])
        # 再补 30000：累计 510000 > 原估损 500000，超出 10000 -> 退回补件
        over = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "压缩机", 30000, "漏登")
        self.assertEqual("returned", over["supplement"]["status"])
        self.assertFalse(over["verdict"]["within_cap"])
        self.assertEqual(10000, over["verdict"]["overflow"])
        self.assertEqual(20000, over["verdict"]["room"])
        # 退回件不占待办名额，可按剩余额度重新提交
        again = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "压缩机（核减）", 20000, "补件后重报")
        self.assertEqual("pending", again["supplement"]["status"])
        self.supplements.confirm_supplement("sup1", "supervisor", again["supplement"]["id"])
        # 已确认满 500000，再补 1 元都不行；主管确认环节同样拦截
        third = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "小物件", 1, "漏登")
        self.assertEqual("returned", third["supplement"]["status"])

    def test_window_and_claim_status(self):
        c = self.approved_claim()
        # 未结案案件不能补登
        other = self.service.create_claim(
            "intake1", "intake", "SC-099", "TY-2026", "A区", "flood", "P-SC-099",
            "R-SC-099", 30.3, 121.1, 100000,
        )
        with self.assertRaises(SupplementError) as ctx:
            self.supplements.submit_missing_item("surveyor1", "surveyor", other["id"], "x", 100, "r")
        self.assertEqual(409, ctx.exception.status)
        # 把结案时间改到 31 天前 -> 超期
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
        with self.supplements.store.connect() as conn:
            conn.execute("UPDATE timeline SET created_at=? WHERE claim_id=? AND action='claim.finalized'", (old, c["id"]))
            conn.commit()
        with self.assertRaises(SupplementError) as ctx2:
            self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "x", 100, "r")
        self.assertEqual(409, ctx2.exception.status)

    def test_roles_and_required_fields(self):
        c = self.approved_claim()
        with self.assertRaises(SupplementError) as ctx:
            self.supplements.submit_missing_item("intake1", "intake", c["id"], "x", 100, "r")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(SupplementError):
            self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "  ", 100, "r")
        with self.assertRaises(SupplementError):
            self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "x", -1, "r")
        pending = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "x", 100, "r")
        with self.assertRaises(SupplementError):
            self.supplements.reject_supplement("sup1", "supervisor", pending["supplement"]["id"], "  ")
        # 查勘员不能确认
        with self.assertRaises(SupplementError) as ctx2:
            self.supplements.confirm_supplement("surveyor1", "surveyor", pending["supplement"]["id"])
        self.assertEqual(403, ctx2.exception.status)

    def test_reject_frees_pending_slot_and_visibility(self):
        c = self.approved_claim()
        first = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "x", 100, "r")
        self.supplements.reject_supplement("sup1", "supervisor", first["supplement"]["id"], "证据不足")
        # 拒绝后可重新提交
        again = self.supplements.submit_missing_item("surveyor1", "surveyor", c["id"], "x2", 100, "r2")
        self.assertEqual("pending", again["supplement"]["status"])
        # 主管待办只有这一项；查勘员只看到自己提交的
        todo = self.supplements.todo("sup1", "supervisor")
        self.assertEqual([again["supplement"]["id"]], [t["id"] for t in todo])
        self.assertTrue(all(t["pending"] for t in todo))
        # 无角色看不到
        with self.assertRaises(SupplementError) as ctx:
            self.supplements.list_supplements("", "viewer")
        self.assertEqual(403, ctx.exception.status)


class RuleUnitTest(unittest.TestCase):
    def test_window_boundary(self):
        closed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(within_submission_window(closed, closed + timedelta(days=30)))
        self.assertFalse(within_submission_window(closed, closed + timedelta(days=30, seconds=1)))
        self.assertFalse(within_submission_window(closed, closed - timedelta(seconds=1)))

    def test_amount_verdict(self):
        verdict = evaluate_supplement_amount(1000, 600, 400)
        self.assertTrue(verdict["within_cap"])
        self.assertEqual(0, verdict["overflow"])
        over = evaluate_supplement_amount(1000, 800, 300)
        self.assertFalse(over["within_cap"])
        self.assertEqual(100, over["overflow"])
        self.assertEqual(200, over["room"])
        self.assertEqual(100, parse_amount("100"))


if __name__ == "__main__":
    unittest.main()
