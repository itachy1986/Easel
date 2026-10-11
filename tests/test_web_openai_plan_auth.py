from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web"))

import app as web  # noqa: E402


class FakeFacade:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"available": True, "connected": False, "sentinel": "safe"}

    def start_connect(self, method):
        self.calls.append(("connect", method))
        return {"jobId": "job123", "state": "running", "method": method}

    def get_job(self, job_id):
        self.calls.append(("job", job_id))
        return {"jobId": job_id, "state": "success", "method": "siwc", "message": "Connected", "errorCode": ""}

    def cancel_job(self, job_id):
        self.calls.append(("cancel", job_id))
        return {"jobId": job_id, "state": "cancelled", "method": "siwc", "message": "Cancelled", "errorCode": "auth_cancelled"}

    def models(self):
        return {"status": "empty", "models": [], "errorCode": "catalog_empty"}

    def test_and_use(self, profile_handle, model):
        self.calls.append(("test", profile_handle, model))
        return {"ok": True, "selectedModel": model, "effectiveProvider": "openai", "testResult": "success", "errorCode": ""}

    def set_platform_fallback(self, enabled):
        self.calls.append(("fallback", enabled))
        return {"ok": True, "platformFallbackEnabled": enabled}

    def prepare_text_inference(self, model=None):
        self.calls.append(("text-preflight", model))
        return {
            "ok": True,
            "billingRoute": "chatgpt_plan",
            "errorCode": "",
            "runtimeModel": f"{model}@openai:work" if model else "",
            "authProfilePinned": True,
        }

    def shutdown(self):
        self.calls.append(("shutdown",))


def test_openai_plan_routes_are_thin_and_safe(monkeypatch):
    fake = FakeFacade()
    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", fake)
    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as client:
        assert client.get("/api/settings/openai-plan/status").json()["available"] is True
        assert client.post("/api/settings/openai-plan/connect", json={"method": "siwc"}).status_code == 200
        assert client.get("/api/settings/openai-plan/job/job123").json()["state"] == "success"
        assert client.post("/api/settings/openai-plan/job/job123/cancel").json()["state"] == "cancelled"
        assert client.get("/api/settings/openai-plan/models").json()["status"] == "empty"
        response = client.post(
            "/api/settings/openai-plan/test-use",
            json={"profileHandle": "plan_0123456789abcdef0123456789abcdef", "model": "openai/gpt-6-astra"},
        )
        assert response.status_code == 200
        assert response.json()["selectedModel"] == "openai/gpt-6-astra"
        fallback = client.post(
            "/api/settings/openai-plan/platform-fallback",
            json={"enabled": True},
        )
        assert fallback.status_code == 200
        assert fallback.json()["platformFallbackEnabled"] is True
    assert ("fallback", True) in fake.calls
    assert ("shutdown",) in fake.calls


def test_platform_fallback_setting_persists_only_after_official_policy_succeeds(tmp_path, monkeypatch):
    fake = FakeFacade()
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=value\n", encoding="utf-8")
    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", fake)
    monkeypatch.setattr(web, "ENV_FILE", env_file)
    local = "http://127.0.0.1:7860"

    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as client:
        response = client.post(
            "/api/settings/openai-plan/platform-fallback",
            json={"enabled": True},
        )

    assert response.status_code == 200
    assert ("fallback", True) in fake.calls
    assert "EASEL_OPENAI_PLATFORM_FALLBACK=1" in env_file.read_text(encoding="utf-8")
    assert "KEEP=value" in env_file.read_text(encoding="utf-8")


def test_platform_fallback_setting_is_not_persisted_when_policy_apply_fails(tmp_path, monkeypatch):
    from easel.openai_plan_auth import InvalidRequestError

    class Reject(FakeFacade):
        def set_platform_fallback(self, enabled):
            raise InvalidRequestError("runtime_policy_unavailable")

    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=value\n", encoding="utf-8")
    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", Reject())
    monkeypatch.setattr(web, "ENV_FILE", env_file)
    local = "http://127.0.0.1:7860"

    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as client:
        response = client.post(
            "/api/settings/openai-plan/platform-fallback",
            json={"enabled": True},
        )

    assert response.status_code == 409
    assert env_file.read_text(encoding="utf-8") == "KEEP=value\n"


def test_platform_fallback_setting_rolls_back_policy_when_env_write_fails(tmp_path, monkeypatch):
    fake = FakeFacade()
    env_file = tmp_path / ".env"
    env_file.write_text("EASEL_OPENAI_PLATFORM_FALLBACK=0\n", encoding="utf-8")
    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", fake)
    monkeypatch.setattr(web, "ENV_FILE", env_file)
    monkeypatch.setattr(web, "_write_env_direct", lambda _updates: (_ for _ in ()).throw(OSError()))
    local = "http://127.0.0.1:7860"

    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as client:
        response = client.post(
            "/api/settings/openai-plan/platform-fallback",
            json={"enabled": True},
        )

    assert response.status_code == 500
    assert fake.calls[:2] == [("fallback", True), ("fallback", False)]
    assert env_file.read_text(encoding="utf-8") == "EASEL_OPENAI_PLATFORM_FALLBACK=0\n"


def test_text_policy_uses_effective_main_agent_model_and_leaves_media_routes_unchanged(
    tmp_path, monkeypatch
):
    fake = FakeFacade()
    config = tmp_path / "openclaw.json"
    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", fake)
    monkeypatch.setattr(web, "_oc_config_path", lambda: config)

    config.write_text(
        json.dumps({
            "agents": {
                "defaults": {"model": {"primary": "anthropic/claude"}},
                "list": [{"id": "main", "model": {"primary": "openai/gpt-6-astra"}}],
            }
        }),
        encoding="utf-8",
    )
    assert web._prepare_openai_text_inference()["billingRoute"] == "chatgpt_plan"
    assert ("text-preflight", "openai/gpt-6-astra") in fake.calls

    fake.calls.clear()
    config.write_text(
        json.dumps({
            "agents": {
                "defaults": {"model": {"primary": "openai/gpt-6-astra"}},
                "list": [{"id": "main", "model": "anthropic/claude"}],
            }
        }),
        encoding="utf-8",
    )
    assert web._prepare_openai_text_inference() == {
        "ok": True,
        "billingRoute": "other",
        "errorCode": "",
        "runtimeModel": "anthropic/claude",
        "authProfilePinned": False,
    }
    assert fake.calls == []


def test_text_runtime_pin_overrides_existing_session_model_for_cli_and_gateway():
    preflight = {
        "ok": True,
        "billingRoute": "chatgpt_plan",
        "errorCode": "",
        "runtimeModel": "openai/gpt-6-astra@openai:work",
        "authProfilePinned": True,
    }

    assert web._text_model_argv(preflight) == [
        "--model", "openai/gpt-6-astra@openai:work",
    ]
    assert web._text_model_headers(preflight) == {
        "x-openclaw-model": "openai/gpt-6-astra@openai:work",
    }


def test_invalid_main_agent_model_fails_closed_instead_of_reusing_session_override(
    tmp_path, monkeypatch
):
    config = tmp_path / "openclaw.json"
    config.write_text(
        json.dumps({"agents": {"defaults": {"model": "anthropic/claude\ninvalid"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(web, "_oc_config_path", lambda: config)

    assert web._prepare_openai_text_inference() == {
        "ok": False,
        "billingRoute": "blocked",
        "errorCode": "model_route_unavailable",
    }


def test_cli_chat_run_passes_exact_runtime_model_as_argv(monkeypatch):
    captured = {}

    class FakeLock:
        def __init__(self, _key):
            pass

        def acquire(self, timeout):
            return timeout > 0

        def release(self):
            pass

    monkeypatch.setattr(web, "_prepare_openai_text_inference", lambda: {
        "ok": True,
        "billingRoute": "chatgpt_plan",
        "errorCode": "",
        "runtimeModel": "openai/gpt-6-astra@openai:work",
        "authProfilePinned": True,
    })
    monkeypatch.setattr(web, "_heal_openclaw_session", lambda _key: None)
    monkeypatch.setattr(web, "_openclaw_session_id", lambda _key: "session-id")
    monkeypatch.setattr(web, "_CrossProcLock", FakeLock)
    monkeypatch.setattr(web, "_proxy_env", lambda: {})

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="OK")

    monkeypatch.setattr(web.subprocess, "run", fake_run)

    assert web.run_agent_sync("hello", timeout=30, session_id="safe-session") == "OK"
    command = captured["cmd"]
    assert command[command.index("--model") + 1] == "openai/gpt-6-astra@openai:work"


def test_test_use_rejects_unknown_handle_as_400_without_echo(monkeypatch):
    from easel.openai_plan_auth import InvalidRequestError

    class Reject(FakeFacade):
        def test_and_use(self, profile_handle, model):
            raise InvalidRequestError("profile_handle_not_current")

    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", Reject())
    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as client:
        response = client.post(
            "/api/settings/openai-plan/test-use",
            json={
                "profileHandle": "openai:user@example.com",
                "model": "openai/gpt-6-astra",
            },
        )
    assert response.status_code == 400
    assert "user@example.com" not in json.dumps(response.json())


def test_connect_validation_is_400_and_never_echoes_payload(monkeypatch):
    from easel.openai_plan_auth import InvalidRequestError

    class Reject(FakeFacade):
        def start_connect(self, method):
            raise InvalidRequestError("unsupported_auth_method")

    monkeypatch.setattr(web, "OPENAI_PLAN_AUTH", Reject())
    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234), headers={"Origin": local}) as client:
        response = client.post(
            "/api/settings/openai-plan/connect",
            json={"method": "api-key-TOKEN_SENTINEL"},
        )
    assert response.status_code == 400
    assert "TOKEN_SENTINEL" not in json.dumps(response.json())


def test_frontend_maps_gateway_job_phases_and_safe_failure_codes():
    hook = (PROJECT_ROOT / "web" / "frontend" / "src" / "components" / "useChatGPTPlan.ts").read_text(
        encoding="utf-8"
    )
    card = (PROJECT_ROOT / "web" / "frontend" / "src" / "components" / "ChatGPTPlanCard.tsx").read_text(
        encoding="utf-8"
    )
    api = (PROJECT_ROOT / "web" / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")

    for code in (
        "gateway_unavailable",
        "gateway_client_unavailable",
        "gateway_auth_unavailable",
        "gateway_rpc_failed",
        "auth_browser_open_failed",
        "unsupported_wizard_step",
        "oauth_choice_unavailable",
    ):
        assert code in hook

    for phase in ("gateway_starting", "gateway_connecting", "waiting", "browser_opened", "terminal_opened"):
        assert phase in api
    assert "正在启动本机 OpenClaw Gateway" in card
    assert "正在建立安全登录连接" in card
    assert "已打开安全登录页面" in card
    assert "已打开 OpenClaw 登录窗口，请在该窗口/浏览器中完成 ChatGPT 登录。" in card
    assert "正在等待 OpenClaw 完成登录" in card
    assert "openclaw --profile easel models auth login --provider openai --method siwc" in hook
    assert "terminalCommand" in hook


def test_frontend_exposes_explicit_platform_fallback_without_weakening_plan_test_gate():
    hook = (PROJECT_ROOT / "web" / "frontend" / "src" / "components" / "useChatGPTPlan.ts").read_text(
        encoding="utf-8"
    )
    card = (PROJECT_ROOT / "web" / "frontend" / "src" / "components" / "ChatGPTPlanCard.tsx").read_text(
        encoding="utf-8"
    )
    api = (PROJECT_ROOT / "web" / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")

    assert "platformFallbackEnabled" in api
    assert "platformCredentialPresent" in api
    assert "effectiveTextBillingRoute" in api
    assert "/api/settings/openai-plan/platform-fallback" in api
    assert "!status.platformFallbackEnabled" in hook
    assert "允许 Platform API 文本 fallback" in card
    assert "可能产生 OpenAI Platform API 费用" in card
