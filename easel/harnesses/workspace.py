"""Build native harness instructions from Easel's existing shared workspace."""
from __future__ import annotations

from pathlib import Path

from .base import HarnessError, RunRequest


def workspace_documents(root: Path) -> list[Path]:
    source = root / "openclaw" / "workspace"
    if not source.is_dir():
        source = Path(__file__).resolve().parents[2] / "openclaw" / "workspace"
    documents = sorted(source.glob("*.md"), key=lambda item: (item.name != "SOUL.md", item.name != "AGENTS.md", item.name))
    if not documents or not (source / "AGENTS.md").is_file():
        raise HarnessError("缺少 openclaw/workspace/AGENTS.md，请检查 Easel 安装")
    return documents


def harness_instructions(request: RunRequest) -> str:
    root = request.cwd.resolve()
    documents = "\n\n".join(
        f"## 共享工作区：{document.name}\n\n{document.read_text(encoding='utf-8')}"
        for document in workspace_documents(root)
    )
    return (
        documents
        + "\n\n## Native Codex 运行边界\n\n"
        + f"项目根绝对路径：{root}；权限范围：{request.sandbox}。"
        + "使用 Codex CLI 自己的登录与账户额度；不得读取、复制或迁移 OpenClaw 凭据。"
        + f"产物保存到 {root / 'outputs'}。"
        + "公开发布、账号状态修改和其它不可逆操作必须获得用户当次明确授权。"
    )
