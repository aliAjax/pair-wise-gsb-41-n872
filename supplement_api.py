"""漏项补赔的 HTTP 接口层：只做请求解析、参数转发和响应封装。

业务规则在 supplement_service，保存逻辑在 supplement_store，金额/时效判断
在 supplement_rules；本文件不含任何业务判断。由 app.py 挂载以下路由：

- GET  /api/supplements            补赔列表（?claim_id=、?status=）
- GET  /api/supplements/todo       按角色返回待办
- GET  /api/supplements/<id>       单条补赔版本（含原案快照字段）
- POST /api/supplements            查勘员提交漏项
- POST /api/supplements/<id>/confirm  主管确认补赔
- POST /api/supplements/<id>/reject   主管拒认
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from supplement_service import DomainError, SupplementService

WRITE_POST_PATHS = {"/api/supplements/confirm", "/api/supplements/reject"}


def route_get(service: SupplementService, path: str, query: str,
              headers_get) -> tuple[int, Any] | None:
    """返回 (status, payload)；路由不匹配时返回 None 交给上层。"""
    if path == "/api/supplements":
        actor, role = headers_get("X-User", ""), headers_get("X-Role", "viewer")
        params = parse_qs(query)
        claim_id = params.get("claim_id", [None])[0]
        status = params.get("status", [None])[0]
        if claim_id is not None:
            try:
                claim_id = int(claim_id)
            except ValueError as exc:
                raise DomainError("claim_id 必须是整数") from exc
        if status:
            from supplement_rules import STATUSES
            if status not in STATUSES:
                raise DomainError("status 取值无效")
        return 200, {"supplements": service.list_supplements(actor, role, claim_id, status)}
    if path == "/api/supplements/todo":
        actor, role = headers_get("X-User", ""), headers_get("X-Role", "viewer")
        return 200, {"todo": service.todo(actor, role)}
    if path.startswith("/api/supplements/"):
        actor, role = headers_get("X-User", ""), headers_get("X-Role", "viewer")
        tail = path[len("/api/supplements/"):]
        if tail.isdigit():
            return 200, {"supplement": service.get_supplement(int(tail), role)}
        raise DomainError("接口不存在", 404)
    return None


def route_post(service: SupplementService, path: str, data: dict[str, Any],
               actor: str, role: str) -> tuple[int, Any] | None:
    """返回 (status, payload)；路由不匹配时返回 None。"""
    if path == "/api/supplements":
        return 201, service.submit_missing_item(
            actor, role,
            claim_id=data["claim_id"],
            item_description=data["item_description"],
            estimated_amount=data["estimated_amount"],
            reason=data["reason"],
        )
    if path.startswith("/api/supplements/"):
        tail = path[len("/api/supplements/"):]
        if tail.endswith("/confirm") and tail[:-len("/confirm")].isdigit():
            supplement_id = int(tail[:-len("/confirm")])
            return 200, service.confirm_supplement(
                actor, role, supplement_id,
                confirmed_amount=data.get("confirmed_amount"),
                review_note=data.get("review_note", ""),
            )
        if tail.endswith("/reject") and tail[:-len("/reject")].isdigit():
            return 200, service.reject_supplement(
                actor, role, int(tail[:-len("/reject")]),
                review_note=data.get("review_note", ""),
            )
        raise DomainError("接口不存在", 404)
    return None


def is_supplement_path(path: str) -> bool:
    return path == "/api/supplements" or path.startswith("/api/supplements/")


def parse(raw_path: str) -> tuple[str, str]:
    parts = urlsplit(raw_path)
    return parts.path, parts.query
