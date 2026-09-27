import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService  # noqa: E402
from supplement_api import SupplementService  # noqa: E402
from supplement_rules import SupplementError, check_window, judge_amount  # noqa: E402
from supplement_store import SupplementStore  # noqa: E402


def settled_claim(claims, number, loss=300000, payout=200000):
    c = claims.create_claim(
        "intake1", "intake", number, "TY-2026", "A区", "flood",
        "P-" + number, "R-" + number, 30.1, 121.1, loss, False, False)
    c = claims.triage_claim("sup1", "supervisor", c["id"], c["version"], 0.1, False)
    c = claims.assign_claim("sup1", "supervisor", c["id"], "adj1", c["version"], "surv1")
    c = claims.record_survey("surv1", "surveyor", c["id"], 0.6, "结构受损", "赔付", c["version"])
    c = claims.submit_review("surv1", "surveyor", c["id"], c["version"])
    return claims.finalize_claim("sup1", "supervisor", c["id"], "approve", payout, c["version"])


class SupplementRulesTest(unittest.TestCase):
    def test_judge_amount(self):
        ok, remaining = judge_amount(300000, 100000, 200000)  # 正好到顶
        self.assertTrue(ok)
        self.assertEqual(200000, remaining)
        ok, _ = judge_amount(300000, 100000, 200001)  # 超出 1 元
        self.assertFalse(ok)
        ok, _ = judge_amount(300000, 300000, 1)  # 已无剩余额度
        self.assertFalse(ok)

    def test_check_window(self):
        closed = "2026-09-01T08:00:00+00:00"
        check_window(closed, "2026-09-30T08:00:00+00:00")  # 29 天，通过
        check_window(closed, "2026-10-01T08:00:00+00:00")  # 正好 30 天，通过
        with self.assertRaises(SupplementError) as ctx:
            check_window(closed, "2026-10-01T08:00:01+00:00")  # 超过 30 天
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(SupplementError):
            check_window(None)


class SupplementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = Path(self.tmp.name) / "test.db"
        self.claims = CatastropheClaimService(db)
        self.store = SupplementStore(db)
        self.svc = SupplementService(self.claims, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_confirm_creates_version_and_keeps_original_payment(self):
        claim = settled_claim(self.claims, "S-001")
        req = self.svc.submit("surv1", "surveyor", claim["id"], "地下室抽水机", 18000, "积水未退漏登")
        self.assertEqual("pending", req["status"])
        result = self.svc.confirm("sup1", "supervisor", req["id"])
        self.assertEqual("confirmed", result["request"]["status"])
        payment = result["payment"]
        self.assertEqual(1, payment["version"])
        self.assertEqual(18000, payment["amount"])
        self.assertEqual("积水未退漏登", payment["reason"])
        # 原赔付记录与最终核定金额不改写
        with self.claims.connect() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"])
            row = conn.execute("SELECT final_payout FROM claims WHERE id=?", (claim["id"],)).fetchone()
        self.assertEqual(200000, row["final_payout"])

    def test_one_pending_per_claim(self):
        claim = settled_claim(self.claims, "S-002")
        self.svc.submit("surv1", "surveyor", claim["id"], "漏项A", 1000, "原因A")
        with self.assertRaises(SupplementError) as ctx:
            self.svc.submit("surv1", "surveyor", claim["id"], "漏项B", 2000, "原因B")
        self.assertEqual(409, ctx.exception.status)
        # 确认后才能提交下一项
        first = self.store.list_requests([claim["id"]], status="pending")[0]
        self.svc.confirm("sup1", "supervisor", first["id"])
        second = self.svc.submit("surv1", "surveyor", claim["id"], "漏项B", 2000, "原因B")
        self.assertEqual("pending", second["status"])

    def test_window_30_days(self):
        claim = settled_claim(self.claims, "S-003")
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
        with self.claims.connect() as conn:
            conn.execute("UPDATE claims SET closed_at=? WHERE id=?", (old, claim["id"]))
        with self.assertRaises(SupplementError) as ctx:
            self.svc.submit("surv1", "surveyor", claim["id"], "漏项", 1000, "原因")
        self.assertEqual(409, ctx.exception.status)

    def test_unsettled_claim_rejected(self):
        c = self.claims.create_claim(
            "intake1", "intake", "S-004", "TY-2026", "A区", "flood",
            "P-S-004", "R-S-004", 30.1, 121.1, 100000, False, False)
        with self.assertRaises(SupplementError) as ctx:
            self.svc.submit("surv1", "surveyor", c["id"], "漏项", 1000, "原因")
        self.assertEqual(409, ctx.exception.status)

    def test_excess_returned_and_cumulative_capped(self):
        claim = settled_claim(self.claims, "S-005", loss=300000)
        # 超过原估损直接退回补件，不占用待确认名额
        req = self.svc.submit("surv1", "surveyor", claim["id"], "全部重报", 300001, "客户重报")
        self.assertEqual("returned", req["status"])
        self.assertIn("退回补件", req["review_note"])
        # 退回后可重新申报
        req = self.svc.submit("surv1", "surveyor", claim["id"], "抽水机", 100000, "漏登原因")
        self.assertEqual("pending", req["status"])
        self.svc.confirm("sup1", "supervisor", req["id"])
        # 累计 100000，再报 200001 超额退回
        req2 = self.svc.submit("surv1", "surveyor", claim["id"], "发电机", 200001, "漏登原因")
        self.assertEqual("returned", req2["status"])
        # 正好累计到原估损可以通过
        req3 = self.svc.submit("surv1", "surveyor", claim["id"], "发电机", 200000, "漏登原因")
        result = self.svc.confirm("sup1", "supervisor", req3["id"])
        self.assertEqual(2, result["payment"]["version"])
        with self.store.connect() as conn:
            self.assertEqual(300000, self.store.confirmed_total(conn, claim["id"]))
        # 已到原估损，再申报任何正数都退回
        req4 = self.svc.submit("surv1", "surveyor", claim["id"], "小零件", 1, "漏登原因")
        self.assertEqual("returned", req4["status"])

    def test_supervisor_send_back_and_resubmit(self):
        claim = settled_claim(self.claims, "S-006")
        req = self.svc.submit("surv1", "surveyor", claim["id"], "漏项", 5000, "原因")
        back = self.svc.send_back("sup1", "supervisor", req["id"], "缺少购置发票")
        self.assertEqual("returned", back["status"])
        self.assertEqual("缺少购置发票", back["review_note"])
        req2 = self.svc.submit("surv1", "surveyor", claim["id"], "漏项", 5000, "原因补充")
        self.assertEqual("pending", req2["status"])

    def test_permissions(self):
        claim = settled_claim(self.claims, "S-007")
        with self.assertRaises(SupplementError) as ctx:
            self.svc.submit("intake1", "intake", claim["id"], "漏项", 1000, "原因")
        self.assertEqual(403, ctx.exception.status)
        req = self.svc.submit("surv1", "surveyor", claim["id"], "漏项", 1000, "原因")
        with self.assertRaises(SupplementError) as ctx2:
            self.svc.confirm("surv1", "surveyor", req["id"])
        self.assertEqual(403, ctx2.exception.status)

    def test_todos_and_state_extras(self):
        claim = settled_claim(self.claims, "S-008")
        req = self.svc.submit("surv1", "surveyor", claim["id"], "漏项", 8000, "原因")
        todos = self.svc.todos("sup1", "supervisor")
        self.assertEqual([req["id"]], [r["id"] for r in todos["pending"]])
        self.assertEqual([req["id"]], [r["id"] for r in self.svc.todos("surv1", "surveyor")["pending"]])
        extras = self.svc.state_extras("sup1", "supervisor", [claim["id"]])
        self.assertEqual(1, len(extras["supplements"]))
        self.assertEqual([], extras["supplement_payments"])
        self.svc.confirm("sup1", "supervisor", req["id"])
        extras = self.svc.state_extras("sup1", "supervisor", [claim["id"]])
        self.assertEqual(1, len(extras["supplement_payments"]))
        self.assertEqual([], extras["supplement_todos"]["pending"])


if __name__ == "__main__":
    unittest.main()
