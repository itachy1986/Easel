"""Optional native agent harnesses; OpenClaw keeps its existing runtime path."""
from __future__ import annotations

from dataclasses import asdict

from .base import Descriptor, HarnessError
from .codex import CodexAdapter
from .commands import base_command


OPENCLAW = Descriptor("openclaw", "OpenClaw", "Gateway / CLI", "使用 Easel 现有 OpenClaw 配置")
ADAPTERS = {"codex": CodexAdapter()}


def adapter(runtime: str):
    try:
        return ADAPTERS[runtime]
    except KeyError as error:
        raise HarnessError("此执行引擎不使用原生 Codex harness") from error


def describe(runtime: str) -> dict:
    descriptor = OPENCLAW if runtime == "openclaw" else adapter(runtime).descriptor
    result = asdict(descriptor)
    if runtime == "openclaw":
        result["installed"] = True
        return result
    try:
        base_command("codex")
        result["installed"] = True
    except (HarnessError, OSError):
        result["installed"] = False
    return result
