"""补赔漏项复核的金额与期限判断。

只包含纯函数：不读写数据库，也不依赖 HTTP 层，方便单独测试和复用。
"""
from __future__ import annotations

from datetime import datetime, timezone

WINDOW_DAYS = 30  # 结案后允许提交漏项申报的天数
EPSILON = 1e-6    # 金额比较的浮点容差


class SupplementError(Exception):
    """补赔业务校验失败，status 为对应的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 86400.0


def check_window(closed_at: str | None, now: str | None = None, days: int = WINDOW_DAYS) -> None:
    """结案 30 天内才允许提交漏项，尚未结案或逾期都拒绝。"""
    if not closed_at:
        raise SupplementError("案件尚未结案，不能提交漏项补赔", 409)
    if days_between(closed_at, now or utcnow_str()) > days:
        raise SupplementError("已超过结案 %d 天的漏项申报期限" % days, 409)


def judge_amount(estimated_loss: float, confirmed_total: float, new_amount: float) -> tuple[bool, float]:
    """判断本次补赔是否使累计超过原估损。

    返回 (是否通过, 剩余额度)。补赔累计达到原估损后剩余额度为 0，
    超出部分应退回补件。
    """
    remaining = round(float(estimated_loss) - float(confirmed_total), 2)
    return float(new_amount) <= remaining + EPSILON, max(remaining, 0.0)
