"""Fail-closed normalization for Codex permission prompts."""
from __future__ import annotations

from .base import HarnessError


SUPPORTED = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}


def permission_question(method: str, params: dict) -> list[dict]:
    if method not in SUPPORTED:
        raise HarnessError("不支持的 Codex 客户端权限请求")
    detail = params.get("command") or params.get("reason") or params.get("itemId", "")
    return [{
        "questionId": "permission",
        "header": "执行授权",
        "question": f"Codex 请求额外权限（仅本次）：{str(detail)[:3000]}",
        "options": [{"label": "允许本次"}, {"label": "拒绝"}],
    }]


def permission_decision(answers: dict[str, list[str]]) -> dict:
    return {"decision": "accept" if answers.get("permission") == ["允许本次"] else "decline"}
