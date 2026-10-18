from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import web.app as web
from easel.harnesses import store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    sessions = outputs / "_sessions"
    owner_state = tmp_path / "owner-state"
    outputs.mkdir()
    sessions.mkdir()
    owner_state.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=owner-sentinel\n", encoding="utf-8")
    monkeypatch.setattr(web, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(web, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(web, "OPENCLAW_SESSIONS_DIR", owner_state)
    monkeypatch.setattr(web, "ENV_FILE", env_file)
    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as test_client:
        test_client.outputs = outputs
        test_client.sessions = sessions
        test_client.env_file = env_file
        yield test_client


def test_harness_settings_default_to_openclaw_and_never_return_credentials(client):
    response = client.get("/api/settings/harness")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == "openclaw"
    assert [agent["id"] for agent in body["agents"]] == ["openclaw", "codex"]
    assert "owner-sentinel" not in response.text


def _save_harness_runtime(client, monkeypatch, runtime: str):
    monkeypatch.setattr(web.harnesses, "describe", lambda candidate: {
        "id": candidate, "label": candidate, "protocol": "test", "install_hint": "terminal",
        "installed": True,
    })
    response = client.post("/api/settings/harness", json={
        "runtime": runtime, "model": "gpt-test" if runtime == "codex" else "",
        "sandbox": "read-only",
    })
    assert response.status_code == 200
    return response


@pytest.mark.parametrize("session_id", ["crafted-native", None])
def test_default_openclaw_rejects_crafted_new_codex_before_side_effects(
    client, monkeypatch, session_id,
):
    assert client.get("/api/settings/harness").json()["runtime"] == "openclaw"
    entered = []

    class FakeAdapter:
        async def run(self, *_args):
            entered.append(True)

    monkeypatch.setattr(web.harnesses, "adapter", lambda _runtime: FakeAdapter())
    payload = {"message": "must not run", "turnId": "blocked-turn", "runtime": "codex"}
    if session_id:
        payload["sessionId"] = session_id

    response = client.post("/api/chat/stream", json=payload)

    assert response.status_code == 409
    assert "服务端" in response.json()["detail"]
    assert entered == []
    binding_dir = client.sessions / "harness"
    assert not binding_dir.exists() or not list(binding_dir.glob("*.json"))
    assert not list(client.sessions.glob("web_*.json"))
    assert not web._job_event_file("blocked-turn").exists()


def test_codex_opt_in_requires_cli_and_only_writes_private_harness_config(client, monkeypatch):
    monkeypatch.setattr(web.harnesses, "describe", lambda runtime: {
        "id": runtime, "label": runtime, "protocol": "test", "install_hint": "terminal",
        "installed": runtime == "openclaw",
    })
    before = client.env_file.read_bytes()
    blocked = client.post("/api/settings/harness", json={
        "runtime": "codex", "model": "", "sandbox": "workspace-write",
    })
    assert blocked.status_code == 409
    assert client.env_file.read_bytes() == before
    assert not (client.outputs / "_harness" / "config.json").exists()

    monkeypatch.setattr(web.harnesses, "describe", lambda runtime: {
        "id": runtime, "label": runtime, "protocol": "test", "install_hint": "terminal", "installed": True,
    })
    saved = client.post("/api/settings/harness", json={
        "runtime": "codex", "model": "gpt-test", "sandbox": "read-only",
    })
    assert saved.status_code == 200
    assert store.settings(client.outputs) == {
        "runtime": "codex", "models": {"codex": "gpt-test"}, "sandbox": "read-only",
    }
    assert client.env_file.read_bytes() == before


def test_runtime_is_per_session_and_legacy_sessions_remain_openclaw(client, monkeypatch):
    legacy = web.ChatRequest(message="hello", sessionId="legacy")
    assert web._chat_runtime(legacy) == "openclaw"

    _save_harness_runtime(client, monkeypatch, "codex")
    fresh = web.ChatRequest(message="hello", sessionId="fresh", runtime="codex")
    assert web._chat_runtime(fresh) == "codex"
    store.bind(client.sessions, "fresh", "codex", "native")
    with pytest.raises(HTTPException, match="新建对话"):
        web._chat_runtime(web.ChatRequest(message="hello", sessionId="fresh", runtime="openclaw"))

    store.bind(client.sessions, "owned-openclaw", "openclaw")
    with pytest.raises(HTTPException, match="新建对话"):
        web._chat_runtime(web.ChatRequest(
            message="hello", sessionId="owned-openclaw", runtime="codex",
        ))


def test_existing_openclaw_history_cannot_be_rebound_to_codex(client):
    session_id = "owned-openclaw"
    (web.OPENCLAW_SESSIONS_DIR / f"{web._openclaw_session_id(session_id)}.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(HTTPException, match="OpenClaw"):
        web._chat_runtime(web.ChatRequest(message="hello", sessionId=session_id, runtime="codex"))


def test_native_codex_environment_does_not_inherit_provider_or_openclaw_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "platform-secret")
    monkeypatch.setenv("OPENCLAW_TOKEN", "openclaw-secret")
    monkeypatch.setenv("SOME_PASSWORD", "password-secret")
    monkeypatch.setenv("PATH", "safe-path")
    monkeypatch.setenv("USERPROFILE", "C:/safe-user")
    monkeypatch.setenv("CODEX_HOME", "C:/safe-codex")
    env = web._native_codex_env()
    assert env["PATH"] == "safe-path"
    assert env["USERPROFILE"] == "C:/safe-user"
    assert env["CODEX_HOME"] == "C:/safe-codex"
    assert "OPENAI_API_KEY" not in env
    assert "OPENCLAW_TOKEN" not in env
    assert "SOME_PASSWORD" not in env


def test_native_codex_stream_uses_fake_adapter_and_persists_runtime_binding(client, monkeypatch):
    captured = {}

    class FakeAdapter:
        async def run(self, request, emit, ask, bind):
            captured["request"] = request
            bind("fake-thread")
            emit("thinking", "fake-reasoning")
            emit("activity", "fake-tool")
            emit("token", "fake-answer")

    _save_harness_runtime(client, monkeypatch, "codex")
    monkeypatch.setattr(web.harnesses, "adapter", lambda runtime: FakeAdapter())
    response = client.post("/api/chat/stream", json={
        "message": "mock only", "sessionId": "native-web", "turnId": "turn-1", "runtime": "codex",
    })
    assert response.status_code == 200
    assert "fake-answer" in response.text
    assert "fake-reasoning" in response.text
    assert captured["request"].session_key == "native-web"
    assert captured["request"].env["EASEL_AGENT_RUNTIME"] == "codex"
    assert store.binding(client.sessions, "native-web") == {
        "runtime": "codex", "native_id": "fake-thread",
    }
    snapshot = json.loads((client.sessions / "web_native-web.json").read_text(encoding="utf-8"))
    assert snapshot["runtime"] == "codex"
    assert snapshot["text"] == "fake-answer"


def test_switching_default_back_only_allows_existing_codex_session(client, monkeypatch):
    entered: list[str] = []

    class FakeAdapter:
        async def run(self, request, emit, ask, bind):
            entered.append(request.session_key)
            bind(request.native_id or "owned-native-thread")
            emit("token", "ok")

    _save_harness_runtime(client, monkeypatch, "codex")
    monkeypatch.setattr(web.harnesses, "adapter", lambda _runtime: FakeAdapter())
    first = client.post("/api/chat/stream", json={
        "message": "first", "sessionId": "owned-native", "turnId": "turn-first", "runtime": "codex",
    })
    assert first.status_code == 200
    assert store.binding(client.sessions, "owned-native") == {
        "runtime": "codex", "native_id": "owned-native-thread",
    }

    _save_harness_runtime(client, monkeypatch, "openclaw")
    resumed = client.post("/api/chat/stream", json={
        "message": "resume", "sessionId": "owned-native", "turnId": "turn-resume", "runtime": "codex",
    })
    denied = client.post("/api/chat/stream", json={
        "message": "new", "sessionId": "new-native", "turnId": "turn-denied", "runtime": "codex",
    })

    assert resumed.status_code == 200
    assert denied.status_code == 409
    assert entered == ["owned-native", "owned-native"]
    assert store.binding(client.sessions, "new-native") == {}
    assert not (client.sessions / "web_new-native.json").exists()
    assert not web._job_event_file("turn-denied").exists()


def test_malformed_and_expired_native_sessions_fail_closed_without_mutation(client, monkeypatch):
    entered = []

    class FakeAdapter:
        async def run(self, *_args):
            entered.append(True)

    _save_harness_runtime(client, monkeypatch, "codex")
    monkeypatch.setattr(web.harnesses, "adapter", lambda _runtime: FakeAdapter())
    malformed = store.binding_path(client.sessions, "malformed-native")
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("{broken", encoding="utf-8")
    malformed_before = malformed.read_bytes()
    rejected = client.post("/api/chat/stream", json={
        "message": "bad", "sessionId": "malformed-native", "turnId": "turn-malformed", "runtime": "codex",
    })
    assert rejected.status_code == 409
    assert malformed.read_bytes() == malformed_before
    assert not (client.sessions / "web_malformed-native.json").exists()

    _save_harness_runtime(client, monkeypatch, "openclaw")
    web._save_turn("web:expired-native", "done", "old result", {"turn_id": "expired-turn"})
    expired = client.sessions / "web_expired-native.json"
    expired_before = expired.read_bytes()
    rejected = client.post("/api/chat/stream", json={
        "message": "bad", "sessionId": "expired-native", "turnId": "new-turn", "runtime": "codex",
    })
    assert rejected.status_code == 409
    assert expired.read_bytes() == expired_before
    assert entered == []
    assert store.binding(client.sessions, "expired-native") == {}
    assert not web._job_event_file("new-turn").exists()


def test_pre_spawn_preference_change_fails_closed_without_turn_or_binding(client, monkeypatch):
    entered = []

    class FakeAdapter:
        async def run(self, *_args):
            entered.append(True)

    _save_harness_runtime(client, monkeypatch, "codex")
    monkeypatch.setattr(web.harnesses, "adapter", lambda _runtime: FakeAdapter())
    resolve_runtime = web._chat_runtime
    calls = 0

    def switch_after_initial_check(request):
        nonlocal calls
        runtime = resolve_runtime(request)
        calls += 1
        if calls == 1:
            store.write_json(client.outputs / "_harness" / "config.json", {
                "runtime": "openclaw", "models": {"codex": "gpt-test"}, "sandbox": "read-only",
            })
        return runtime

    monkeypatch.setattr(web, "_chat_runtime", switch_after_initial_check)
    response = client.post("/api/chat/stream", json={
        "message": "race", "sessionId": "raced-native", "turnId": "turn-race", "runtime": "codex",
    })

    assert response.status_code == 200
    assert "未在服务端启用" in response.text
    assert entered == []
    assert store.binding(client.sessions, "raced-native") == {}
    assert not (client.sessions / "web_raced-native.json").exists()


def test_pre_spawn_model_change_fails_closed_without_adapter_entry(client, monkeypatch):
    entered = []

    class FakeAdapter:
        async def run(self, *_args):
            entered.append(True)

    _save_harness_runtime(client, monkeypatch, "codex")
    monkeypatch.setattr(web.harnesses, "adapter", lambda _runtime: FakeAdapter())
    resolve_runtime = web._chat_runtime
    calls = 0

    def change_config_after_pre_spawn_check(request):
        nonlocal calls
        runtime = resolve_runtime(request)
        calls += 1
        if calls == 2:
            store.write_json(client.outputs / "_harness" / "config.json", {
                "runtime": "codex", "models": {"codex": "changed-model"},
                "sandbox": "read-only",
            })
        return runtime

    monkeypatch.setattr(web, "_chat_runtime", change_config_after_pre_spawn_check)
    response = client.post("/api/chat/stream", json={
        "message": "race", "sessionId": "model-raced-native",
        "turnId": "turn-model-race", "runtime": "codex",
    })

    assert response.status_code == 200
    assert "设置已变化" in response.text
    assert entered == []
    assert store.binding(client.sessions, "model-raced-native") == {}
    assert not (client.sessions / "web_model-raced-native.json").exists()


def test_native_permission_answer_rejects_other_session(client):
    class Pending:
        value = None

        def done(self):
            return self.value is not None

        def set_result(self, value):
            self.value = value

    pending = Pending()
    web._HARNESS_QUESTIONS["harness_owned"] = {
        "session": "owner-session",
        "future": pending,
        "status": "pending",
        "questions": [{"questionId": "permission"}],
    }
    try:
        denied = client.post("/api/chat/question/answer", json={
            "sessionId": "other-session", "questionId": "harness_owned",
            "answers": {"permission": ["允许本次"]},
        })
        assert denied.status_code == 403
        assert pending.value is None
        status_denied = client.post("/api/chat/question/status", json={
            "sessionId": "other-session", "questionIds": ["harness_owned"],
        })
        assert status_denied.status_code == 403
        status = client.post("/api/chat/question/status", json={
            "sessionId": "owner-session", "questionIds": ["harness_owned"],
        })
        assert status.json()["questions"]["harness_owned"]["status"] == "pending"
        accepted = client.post("/api/chat/question/answer", json={
            "sessionId": "owner-session", "questionId": "harness_owned",
            "answers": {"permission": ["拒绝"]},
        })
        assert accepted.status_code == 200
        assert pending.value == {"permission": ["拒绝"]}
    finally:
        web._HARNESS_QUESTIONS.pop("harness_owned", None)
