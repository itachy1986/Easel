from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from easel.harnesses import store
from easel.harnesses.base import HarnessError, RunRequest
from easel.harnesses.codex import CodexAdapter
from easel.harnesses.commands import base_command


FIXTURE = Path(__file__).parent / "fixtures" / "native_codex_app_server.py"


def _request(tmp_path: Path, mode: str = "normal", native_id: str = "") -> RunRequest:
    workspace = tmp_path / "openclaw" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("# test instructions\n", encoding="utf-8")
    env = {
        "PATH": "",
        "FAKE_CODEX_AUDIT": str(tmp_path / "audit.jsonl"),
        "FAKE_CODEX_MODE": mode,
    }
    return RunRequest(
        "test prompt", tmp_path, env, native_id=native_id, model="test-model",
        sandbox="workspace-write", command=(sys.executable, str(FIXTURE)),
        session_key="browser-session",
    )


def _audit(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_native_codex_streams_events_and_resumes_owned_thread(tmp_path):
    events: list[tuple[str, str]] = []
    bound: list[str] = []

    async def reject(_questions):
        raise AssertionError("unexpected approval")

    asyncio.run(CodexAdapter().run(
        _request(tmp_path, native_id="owned-thread"),
        lambda kind, text: events.append((kind, text)), reject, bound.append,
    ))
    assert bound == ["owned-thread"]
    assert ("thinking", "思考") in events
    assert ("activity", "工具：commandExecution") in events
    assert ("token", "完成") in events
    audit = _audit(tmp_path)
    resumed = next(item for item in audit if item.get("method") == "thread/resume")
    assert resumed["params"]["threadId"] == "owned-thread"
    turn = next(item for item in audit if item.get("method") == "turn/start")
    assert turn["params"]["approvalPolicy"] == "on-request"
    assert turn["params"]["sandboxPolicy"]["networkAccess"] is False


def test_native_codex_rejects_thread_replacement(tmp_path):
    async def reject(_questions):
        raise AssertionError

    with pytest.raises(HarnessError, match="不同的原生会话"):
        asyncio.run(CodexAdapter().run(
            _request(tmp_path, mode="wrong-resume", native_id="owned-thread"),
            lambda *_args: None, reject, lambda _value: None,
        ))


@pytest.mark.parametrize("allow, expected", [(True, "accept"), (False, "decline")])
def test_native_codex_permission_is_explicit_and_once_only(tmp_path, allow, expected):
    async def answer(questions):
        assert [item["label"] for item in questions[0]["options"]] == ["允许本次", "拒绝"]
        return {"permission": ["允许本次" if allow else "拒绝"]}

    asyncio.run(CodexAdapter().run(
        _request(tmp_path, mode="permission"), lambda *_args: None, answer, lambda _value: None,
    ))
    response = next(item for item in _audit(tmp_path) if item.get("id") == "permission-1")
    assert response["result"] == {"decision": expected}


def test_native_codex_secret_input_fails_closed(tmp_path):
    asked = []

    async def answer(questions):
        asked.append(questions)
        return {}

    async def run():
        with pytest.raises(HarnessError, match="不支持的交互或工具"):
            await asyncio.wait_for(CodexAdapter().run(
                _request(tmp_path, mode="secret"), lambda *_args: None, answer, lambda _value: None,
            ), timeout=2)

    asyncio.run(run())
    assert asked == []


def test_native_codex_cancel_interrupts_turn_and_cleans_up(tmp_path):
    async def reject(_questions):
        raise AssertionError("unexpected approval")

    async def run():
        events = []
        task = asyncio.create_task(CodexAdapter().run(
            _request(tmp_path, mode="hang"),
            lambda *args: events.append(args), reject, lambda _value: None,
        ))
        for _ in range(200):
            if ("activity", "工具：commandExecution") in events:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("fake app-server never received turn/start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert any(item.get("method") == "turn/interrupt" for item in _audit(tmp_path))


def test_session_binding_is_runtime_owned_and_hashed(tmp_path):
    store.bind(tmp_path, "../unsafe/session", "codex", "native-thread")
    path = store.binding_path(tmp_path, "../unsafe/session")
    assert path.parent == tmp_path / "harness"
    assert store.binding(tmp_path, "../unsafe/session") == {
        "runtime": "codex", "native_id": "native-thread",
    }
    with pytest.raises(HarnessError):
        store.bind(tmp_path, "../unsafe/session", "openclaw", "")


def test_settings_default_openclaw_and_only_support_opt_in(tmp_path):
    assert store.settings(tmp_path)["runtime"] == "openclaw"
    store.write_json(tmp_path / "_harness" / "config.json", {
        "runtime": "codex", "models": {"codex": "gpt-test"}, "sandbox": "read-only",
    })
    assert store.settings(tmp_path) == {
        "runtime": "codex", "models": {"codex": "gpt-test"}, "sandbox": "read-only",
    }
    store.write_json(tmp_path / "_harness" / "config.json", {"runtime": "acp"})
    with pytest.raises(HarnessError, match="未知"):
        store.settings(tmp_path)


def test_windows_codex_npm_launcher_uses_node_argv_without_shell(tmp_path, monkeypatch):
    launcher = tmp_path / "codex.cmd"
    launcher.touch()
    node = tmp_path / "node.exe"
    node.touch()
    entry = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.setattr("easel.harnesses.commands.shutil.which", lambda name: str(launcher) if name == "codex" else None)
    assert base_command("codex") == [str(node), str(entry)]
