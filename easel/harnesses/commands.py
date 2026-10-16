"""Resolve the Codex CLI without a shell or Windows batch argument rewriting."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .base import HarnessError


def base_command(name: str) -> list[str]:
    if name != "codex":
        raise HarnessError("只支持 Codex 原生 CLI")
    executable = shutil.which(name)
    if not executable:
        raise HarnessError("Codex CLI 未安装或不在 PATH；请在终端自行安装并登录")
    path = Path(executable)
    if path.suffix.lower() not in (".cmd", ".bat"):
        node = path.parent / ("node.exe" if os.name == "nt" else "node")
        if node.is_file() and os.access(node, os.X_OK):
            try:
                shebang = path.open("rb").readline(128).strip()
            except OSError:
                shebang = b""
            if shebang.startswith(b"#!") and b"node" in shebang.split():
                return [str(node), str(path.resolve())]
        return [str(path)]
    adjacent_node = path.parent / "node.exe"
    node = str(adjacent_node) if adjacent_node.is_file() else shutil.which("node")
    script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if node and script.is_file():
        return [node, str(script)]
    raise HarnessError("Codex 的 Windows npm 启动入口不存在；请重新安装 CLI")
