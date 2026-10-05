from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from easel.openai_plan_gateway import (
    EXPECTED_OPENCLAW_VERSION,
    GatewayClientUnavailable,
    OpenAIPlanGatewaySidecar,
    GatewaySidecarResult,
    ensure_gateway_ready,
    is_safe_auth_url,
    resolve_gateway_runtime,
)


def _fake_openclaw_package(
    tmp_path: Path,
    *,
    version: str = EXPECTED_OPENCLAW_VERSION,
    exported: bool = True,
    runtime_source: str = "export class GatewayClient {}\n",
):
    package_root = tmp_path / "openclaw"
    package_root.mkdir()
    entry = package_root / "openclaw.mjs"
    entry.write_text("// fake CLI entry\n", encoding="utf-8")
    runtime = package_root / "public" / "gateway-runtime.js"
    runtime.parent.mkdir()
    runtime.write_text(runtime_source, encoding="utf-8")
    exports = {"./plugin-sdk/gateway-runtime": {"default": "./public/gateway-runtime.js"}} if exported else {}
    (package_root / "package.json").write_text(
        json.dumps({"name": "openclaw", "version": version, "type": "module", "exports": exports}),
        encoding="utf-8",
    )
    return package_root, entry, runtime


def test_public_gateway_runtime_is_resolved_from_package_exports(tmp_path):
    package_root, entry, runtime = _fake_openclaw_package(tmp_path)

    resolved = resolve_gateway_runtime(["node.exe", str(entry)])

    assert resolved.node_argv == "node.exe"
    assert resolved.package_root == package_root.resolve()
    assert resolved.openclaw_entry == entry.resolve()
    assert resolved.gateway_runtime == runtime.resolve()
    assert "dist" not in str(resolved.gateway_runtime)


@pytest.mark.parametrize(
    ("version", "exported"),
    [("2026.9.6", True), (EXPECTED_OPENCLAW_VERSION, False)],
)
def test_public_gateway_runtime_mismatch_fails_closed(tmp_path, version, exported):
    _, entry, _ = _fake_openclaw_package(tmp_path, version=version, exported=exported)

    with pytest.raises(GatewayClientUnavailable) as exc:
        resolve_gateway_runtime(["node.exe", str(entry)])

    assert exc.value.code == "gateway_client_unavailable"


@pytest.mark.parametrize(
    "url",
    [
        "https://auth.openai.com/authorize?state=SECRET_SENTINEL",
        "https://chatgpt.com/auth/callback?code=SECRET_SENTINEL",
    ],
)
def test_browser_auth_url_accepts_only_expected_https_surfaces(url):
    assert is_safe_auth_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.openai.com/authorize",
        "https://127.0.0.1/authorize",
        "https://localhost/authorize",
        "file:///tmp/token",
        "javascript:alert(1)",
        "data:text/plain,secret",
        "https://openai.com.evil.test/authorize",
        "https://user:pass@auth.openai.com/authorize",
        "https://auth.openai.com:444/authorize",
    ],
)
def test_browser_auth_url_rejects_non_https_private_or_lookalike_surfaces(url):
    assert not is_safe_auth_url(url)


def test_gateway_readiness_reuses_existing_gateway_without_spawn(tmp_path):
    calls = []

    result = ensure_gateway_ready(
        cwd=tmp_path,
        ready_probe=lambda: True,
        command_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        platform="win32",
    )

    assert result == ""
    assert calls == []


def test_gateway_readiness_starts_with_existing_script_and_never_force(tmp_path):
    script = tmp_path / "scripts" / "gateway.ps1"
    script.parent.mkdir()
    script.write_text("# fake\n", encoding="utf-8")
    probes = iter([False, True])
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Completed", (), {"returncode": 0})()

    result = ensure_gateway_ready(
        cwd=tmp_path,
        ready_probe=lambda: next(probes),
        command_runner=run,
        platform="win32",
    )

    assert result == ""
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[-2:] == [str(script), "start"]
    assert "--force" not in " ".join(argv)
    assert kwargs["shell"] is False
    assert kwargs["timeout"] > 0


def test_gateway_readiness_failure_is_safe_and_bounded(tmp_path):
    script = tmp_path / "scripts" / "gateway.sh"
    script.parent.mkdir()
    script.write_text("# fake\n", encoding="utf-8")

    result = ensure_gateway_ready(
        cwd=tmp_path,
        ready_probe=lambda: False,
        command_runner=lambda *args, **kwargs: type("Completed", (), {"returncode": 1})(),
        platform="linux",
    )

    assert result == "gateway_unavailable"


def test_sidecar_result_is_a_code_only_boundary():
    result = GatewaySidecarResult(
        outcome="failure",
        error_code="gateway_rpc_failed",
        fallback_eligible=True,
    )
    encoded = json.dumps(result.public_dict())

    assert encoded == '{"outcome": "failure", "errorCode": "gateway_rpc_failed"}'
    assert "fallback" not in encoded.lower()
    assert "SECRET_SENTINEL" not in encoded


FAKE_GATEWAY_RUNTIME = r'''
import fs from "node:fs";

let nextClientId = 0;
function trace(row) {
  fs.appendFileSync(process.env.EASEL_GATEWAY_SIDECAR_TEST_TRACE, JSON.stringify(row) + "\n");
}

export class GatewayClient {
  constructor(options) {
    this.options = options;
    this.id = ++nextClientId;
    this.nextCount = 0;
    trace({type: "construct", client: this.id, url: options.url, clientName: options.clientName,
      clientDisplayName: options.clientDisplayName, mode: options.mode, role: options.role,
      scopes: options.scopes, caps: options.caps, instanceId: options.instanceId});
  }
  start() {
    queueMicrotask(() => this.options.onHelloOk?.({features: {methods: [
      "models.authStatus", "models.authLogin", "wizard.next", "wizard.cancel", "wizard.status"
    ]}}));
  }
  async request(method, params) {
    trace({type: "request", client: this.id, method, params});
    const mode = process.env.EASEL_GATEWAY_SIDECAR_TEST_MODE || "success";
    if (method === "models.authStatus") {
      if (mode === "oauth-unavailable") {
        return {providerCapabilities: [{provider: "openai", loginOptions: [
          {id: "openai/openai-token-sharing", kind: "oauth"}
        ]}]};
      }
      return {providerCapabilities: [{provider: "openai", loginOptions: [
        {id: "openai/openai", kind: "oauth"},
        {id: "openai/openai-token-sharing", kind: "oauth"}
      ]}]};
    }
    if (method === "models.authLogin") {
      if (mode === "auth-login-block") return await new Promise(() => {});
      return {done: false, status: "running"};
    }
    if (method === "wizard.cancel") return {done: true, status: "cancelled"};
    if (method === "wizard.status") return {done: false, status: "running"};
    if (method === "wizard.next") {
      this.nextCount += 1;
      if (mode === "cancel") return await new Promise(() => {});
      if (mode === "rpc-error") throw new Error("Authorization: Bearer TOKEN_SENTINEL https://auth.openai.com/?code=RAW_CODE");
      if (mode === "unsupported") {
        return {done: false, status: "running", step: {id: "secret-step", type: "secret", prompt: "SECRET_SENTINEL"}};
      }
      if (this.nextCount === 1) {
        return {done: false, status: "running", step: {id: "browser-step", type: "note",
          externalUrl: "https://auth.openai.com/authorize?state=AUTHORIZE_URL_SENTINEL"}};
      }
      return {done: true, status: "done"};
    }
    throw new Error("arbitrary method reached fake client");
  }
  async stopAndWait() { trace({type: "stop", client: this.id}); }
}

export async function startGatewayClientWhenEventLoopReady(client) {
  client.start();
  if (process.env.EASEL_GATEWAY_SIDECAR_TEST_MODE === "auth-unavailable") {
    console.error("Gateway password DEVICE_TOKEN_SENTINEL");
    return {ready: false, elapsedMs: 0, maxDriftMs: 0, checks: 1, aborted: false};
  }
  return {ready: true, elapsedMs: 0, maxDriftMs: 0, checks: 1, aborted: false};
}
'''


def _read_trace(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _sidecar(tmp_path: Path, *, mode: str = "success", browser_opener=lambda _url: True):
    package_root, entry, _ = _fake_openclaw_package(
        tmp_path,
        runtime_source=FAKE_GATEWAY_RUNTIME,
    )
    trace = tmp_path / "trace.jsonl"
    env = os.environ.copy()
    env["EASEL_GATEWAY_SIDECAR_TEST_TRACE"] = str(trace)
    env["EASEL_GATEWAY_SIDECAR_TEST_MODE"] = mode
    observed = {}

    def popen(argv, **kwargs):
        observed["argv"] = list(argv)
        observed["kwargs"] = dict(kwargs)
        return subprocess.Popen(argv, **kwargs)

    sidecar = OpenAIPlanGatewaySidecar(
        profile="easel",
        cwd=tmp_path,
        base_cmd_factory=lambda: ["node", str(entry)],
        gateway_url_factory=lambda: "ws://127.0.0.1:37289",
        browser_opener=browser_opener,
        popen_factory=popen,
        environment=env,
    )
    return sidecar, trace, observed, package_root


def test_sidecar_uses_one_official_client_for_siwc_wizard_and_keeps_url_internal(tmp_path):
    opened = []
    phases = []
    sidecar, trace, observed, _ = _sidecar(tmp_path, browser_opener=lambda url: opened.append(url) or True)

    result = sidecar.run(
        method="siwc",
        session_id="opaque-session-123456",
        timeout=5,
        cancel_event=threading.Event(),
        on_phase=phases.append,
    )

    rows = _read_trace(trace)
    constructor = next(row for row in rows if row["type"] == "construct")
    requests = [row for row in rows if row["type"] == "request"]
    assert result == GatewaySidecarResult("success", "")
    assert len([row for row in rows if row["type"] == "construct"]) == 1
    assert {row["client"] for row in requests} == {constructor["client"]}
    assert constructor["url"] == "ws://127.0.0.1:37289"
    assert constructor["clientName"] == "cli"
    assert constructor["clientDisplayName"] == "easel-plan-auth"
    assert constructor["mode"] == "cli"
    assert constructor["role"] == "operator"
    assert constructor["scopes"] == ["operator.admin"]
    assert constructor["caps"] == []
    assert constructor["instanceId"]
    assert requests[0] == {
        "type": "request",
        "client": constructor["client"],
        "method": "models.authLogin",
        "params": {
            "sessionId": "opaque-session-123456",
            "agentId": "main",
            "authChoice": "openai-token-sharing",
        },
    }
    assert requests[1]["method"] == "wizard.next"
    assert requests[2]["method"] == "wizard.next"
    assert requests[2]["params"]["answer"] == {"stepId": "browser-step"}
    assert rows[-1] == {"type": "stop", "client": constructor["client"]}
    assert opened == ["https://auth.openai.com/authorize?state=AUTHORIZE_URL_SENTINEL"]
    assert phases == ["gateway_connecting", "waiting", "browser_opened", "waiting"]
    assert observed["kwargs"]["shell"] is False
    assert observed["argv"] == ["node", str(sidecar.sidecar_path)]
    public = json.dumps(result.public_dict()) + json.dumps(phases)
    assert "AUTHORIZE_URL_SENTINEL" not in public
    assert "opaque-session-123456" not in " ".join(observed["argv"])


def test_sidecar_oauth_choice_is_confirmed_from_current_capabilities(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path)

    result = sidecar.run(
        method="oauth",
        session_id="oauth-session-1234567",
        timeout=5,
        cancel_event=threading.Event(),
    )

    requests = [row for row in _read_trace(trace) if row["type"] == "request"]
    assert result.outcome == "success"
    assert requests[0]["method"] == "models.authStatus"
    login = next(row for row in requests if row["method"] == "models.authLogin")
    assert login["params"]["authChoice"] == "openai/openai"


def test_sidecar_unsupported_input_cancels_exact_session_and_fails_closed(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="unsupported")

    result = sidecar.run(
        method="siwc",
        session_id="unsupported-session-123456",
        timeout=5,
        cancel_event=threading.Event(),
    )

    rows = _read_trace(trace)
    cancel = next(row for row in rows if row.get("method") == "wizard.cancel")
    assert cancel["params"] == {"sessionId": "unsupported-session-123456"}
    assert result == GatewaySidecarResult("failure", "unsupported_wizard_step")
    assert "SECRET_SENTINEL" not in json.dumps(result.public_dict())
    assert rows[-1]["type"] == "stop"


def test_sidecar_cancel_uses_same_client_and_stop_and_wait(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="cancel")
    cancel_event = threading.Event()

    def cancel_after_next():
        deadline = time.time() + 3
        while time.time() < deadline:
            if trace.exists() and '"method":"wizard.next"' in trace.read_text(encoding="utf-8"):
                cancel_event.set()
                return
            time.sleep(0.01)
        raise AssertionError("wizard.next was not reached")

    canceller = threading.Thread(target=cancel_after_next)
    canceller.start()
    result = sidecar.run(
        method="siwc",
        session_id="cancel-session-123456",
        timeout=5,
        cancel_event=cancel_event,
    )
    canceller.join(timeout=1)

    rows = _read_trace(trace)
    client_ids = {row["client"] for row in rows if "client" in row}
    assert result == GatewaySidecarResult("cancelled", "auth_cancelled")
    assert len(client_ids) == 1
    assert any(row.get("method") == "wizard.cancel" for row in rows)
    assert rows[-1]["type"] == "stop"


def test_cancel_during_auth_login_cancels_exact_session_on_same_client(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="auth-login-block")
    cancel_event = threading.Event()

    def cancel_after_login_request():
        deadline = time.time() + 3
        while time.time() < deadline:
            if trace.exists() and '"method":"models.authLogin"' in trace.read_text(encoding="utf-8"):
                cancel_event.set()
                return
            time.sleep(0.01)
        raise AssertionError("models.authLogin was not reached")

    canceller = threading.Thread(target=cancel_after_login_request)
    canceller.start()
    result = sidecar.run(
        method="siwc",
        session_id="auth-login-cancel-123456",
        timeout=5,
        cancel_event=cancel_event,
    )
    canceller.join(timeout=1)

    rows = _read_trace(trace)
    login = next(row for row in rows if row.get("method") == "models.authLogin")
    cancel = next(row for row in rows if row.get("method") == "wizard.cancel")
    assert result == GatewaySidecarResult("cancelled", "auth_cancelled")
    assert cancel["client"] == login["client"]
    assert cancel["params"] == {"sessionId": "auth-login-cancel-123456"}
    assert rows[-1]["type"] == "stop"


def test_sidecar_rejects_non_loopback_gateway_without_starting_node(tmp_path):
    sidecar, trace, observed, _ = _sidecar(tmp_path)
    sidecar._gateway_url_factory = lambda: "ws://192.0.2.10:37289"

    result = sidecar.run(
        method="siwc",
        session_id="blocked-session",
        timeout=5,
        cancel_event=threading.Event(),
    )

    assert result == GatewaySidecarResult(
        "failure", "gateway_client_unavailable", fallback_eligible=True
    )
    assert not trace.exists()
    assert observed == {}


def test_sidecar_browser_open_failure_cancels_without_publishing_url(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, browser_opener=lambda _url: False)

    result = sidecar.run(
        method="siwc",
        session_id="browser-fail-session-123456",
        timeout=5,
        cancel_event=threading.Event(),
    )

    rows = _read_trace(trace)
    assert result == GatewaySidecarResult("failure", "auth_browser_open_failed")
    assert any(row.get("method") == "wizard.cancel" for row in rows)
    assert rows[-1]["type"] == "stop"
    assert "AUTHORIZE_URL_SENTINEL" not in json.dumps(result.public_dict())


def test_sidecar_timeout_cancels_same_session_and_stops(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="cancel")

    result = sidecar.run(
        method="siwc",
        session_id="timeout-session-123456",
        timeout=0.3,
        cancel_event=threading.Event(),
    )

    rows = _read_trace(trace)
    cancel = next(row for row in rows if row.get("method") == "wizard.cancel")
    assert cancel["params"] == {"sessionId": "timeout-session-123456"}
    assert result == GatewaySidecarResult("failure", "auth_timeout")
    assert rows[-1]["type"] == "stop"


def test_gateway_rpc_raw_error_is_reduced_to_code_only(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="rpc-error")

    result = sidecar.run(
        method="siwc",
        session_id="rpc-error-session-123456",
        timeout=5,
        cancel_event=threading.Event(),
    )

    assert result == GatewaySidecarResult("failure", "gateway_rpc_failed")
    assert "TOKEN_SENTINEL" not in json.dumps(result.public_dict())
    assert "RAW_CODE" not in json.dumps(result.public_dict())
    assert _read_trace(trace)[-1]["type"] == "stop"


def test_gateway_auth_failure_and_child_stderr_are_reduced_to_code_only(tmp_path, capsys):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="auth-unavailable")

    result = sidecar.run(
        method="siwc",
        session_id="auth-unavailable-123456",
        timeout=5,
        cancel_event=threading.Event(),
    )

    captured = capsys.readouterr()
    assert result == GatewaySidecarResult(
        "failure", "gateway_auth_unavailable", fallback_eligible=True
    )
    assert "DEVICE_TOKEN_SENTINEL" not in captured.out
    assert "DEVICE_TOKEN_SENTINEL" not in captured.err
    assert "DEVICE_TOKEN_SENTINEL" not in json.dumps(result.public_dict())
    assert _read_trace(trace)[-1]["type"] == "stop"


def test_oauth_without_one_current_official_choice_fails_closed(tmp_path):
    sidecar, trace, _, _ = _sidecar(tmp_path, mode="oauth-unavailable")

    result = sidecar.run(
        method="oauth",
        session_id="oauth-unavailable-123456",
        timeout=5,
        cancel_event=threading.Event(),
    )

    rows = _read_trace(trace)
    assert result == GatewaySidecarResult(
        "failure", "oauth_choice_unavailable", fallback_eligible=True
    )
    assert not any(row.get("method") == "models.authLogin" for row in rows)
    assert rows[-1]["type"] == "stop"


def test_sidecar_ipc_has_no_arbitrary_rpc_passthrough():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "openai_plan_gateway_sidecar.mjs").read_text(
        encoding="utf-8"
    )
    for method in (
        "models.authStatus",
        "models.authLogin",
        "wizard.next",
        "wizard.cancel",
        "wizard.status",
    ):
        assert f'"{method}"' in source
    for forbidden in ("config.set", "config.apply", "node.invoke", "exec", "auth.order"):
        assert f'"{forbidden}"' not in source
    assert "command.method" not in source
    assert "new WebSocket" not in source
    assert "ws.send" not in source
