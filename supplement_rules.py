"""漏项补赔业务规则：纯函数，不做持久化与网络访问。

金额判断与时效判断集中在本模块，便于单独测试；服务层负责把判断结果
转换成案件状态（待主管确认 / 退回补件）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

SUBMISSION_WINDOW_DAYS = 30

PENDING = "pending"        # 待主管确认
RETURNED = "returned"      # 超出累计上限，退回补件
CONFIRMED = "confirmed"    # 主管已确认并生成补赔付款
REJECTED = "rejected"      # 主管拒认
STATUSES = {PENDING, RETURNED, CONFIRMED, REJECTED}

SUBMITTER_ROLES = {"adjuster", "surveyor"}
SUPERVISOR_ROLES = {"supervisor"}
VIEWER_ROLES = {"intake", "supervisor", "adjuster", "surveyor", "auditor"}

CLOSED_STATUSES = {"approved", "closed"}

# 金额以元计，允许分级（分）以下的浮点误差
EPS = 1e-6


def parse_timestamp(value: str | datetime) -> datetime:
    """解析 ISO 时间；朴素时间按 UTC 处理。"""
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def within_submission_window(closed_at: str | datetime,
                             submitted_at: str | datetime | None = None,
                             days: int = SUBMISSION_WINDOW_DAYS) -> bool:
    """结案后 days 天内（含第 30 天当天）可以补登漏项。"""
    closed = parse_timestamp(closed_at)
    submitted = parse_timestamp(submitted_at) if submitted_at is not None else datetime.now(timezone.utc)
    delta = submitted - closed
    return timedelta(0) <= delta <= timedelta(days=days)


def parse_amount(value: Any, label: str = "金额") -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s必须是数值" % label) from exc
    return amount


def remaining_room(original_estimate: float, confirmed_total: float) -> float:
    """原估损下还剩多少补赔额度。"""
    return max(0.0, original_estimate - confirmed_total)


def cumulative_within_cap(original_estimate: float, confirmed_total: float, amount: float) -> bool:
    """已确认补赔累计 + 本次补赔不得超过原估损。"""
    return confirmed_total + amount <= original_estimate + EPS


def evaluate_supplement_amount(original_estimate: float, confirmed_total: float,
                               estimated_amount: float) -> dict[str, float | bool]:
    """返回补赔金额判断结果：剩余额度、超出金额、是否在上限内。"""
    within = cumulative_within_cap(original_estimate, confirmed_total, estimated_amount)
    overflow = max(0.0, confirmed_total + estimated_amount - original_estimate)
    return {
        "within_cap": within,
        "room": round(remaining_room(original_estimate, confirmed_total), 2),
        "overflow": round(overflow, 2),
    }
