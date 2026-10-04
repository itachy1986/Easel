"""Bounded bridge to OpenClaw's public GatewayClient runtime.

The Node sidecar owns the official Gateway connection.  Python only resolves
the public package export, supervises the child process, and opens an
allowlisted HTTPS authorization URL without persisting it.
"""

from __future__ import annotations

import json
import ipaddress
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from easel.gateway_endpoint import healthz_url


EXPECTED_OPENCLAW_VERSION = "2026.9.7"
_PUBLIC_EXPORT = "./plugin-sdk/gateway-runtime"
_AUTH_HOST_SUFFIXES = ("openai.com", "chatgpt.com")
_SIDECAR_EVENTS = frozenset({
    "ready", "waiting", "open_url", "success", "cancelled", "failure", "gateway_error",
})
_PUBLIC_ERROR_CODES = frozenset({
    "auth_browser_open_failed",
    "auth_cancelled",
    "auth_failed",
    "auth_timeout",
    "gateway_auth_unavailable",
    "gateway_client_unavailable",
    "gateway_rpc_failed",
    "oauth_choice_unavailable",
    "unsupported_wizard_step",
})


class GatewayClientUnavailable(RuntimeError):
    """The selected OpenClaw install cannot provide the approved public API."""

    code = "gateway_client_unavailable"


@dataclass(frozen=True)
class GatewayRuntime:
    node_argv: str
    package_root: Path
    openclaw_entry: Path
    gateway_runtime: Path
    version: str


@dataclass(frozen=True)
class GatewaySidecarResult:
    outcome: str
    error_code: str = ""

    def public_dict(self) -> dict[str, str]:
        return {"outcome": self.outcome, "errorCode": self.error_code}


def _export_target(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        target = value.get("default")
        return target if isinstance(target, str) else ""
    return ""


def resolve_gateway_runtime(
    base_cmd: list[str],
    *,
    expected_version: str = EXPECTED_OPENCLAW_VERSION,
) -> GatewayRuntime:
    """Resolve the public Gateway runtime from the selected CLI package."""

    try:
        if len(base_cmd) != 2:
            raise ValueError("OpenClaw command does not identify its package entry")
        node_argv = str(base_cmd[0])
        openclaw_entry = Path(base_cmd[1]).resolve(strict=True)
        if openclaw_entry.name != "openclaw.mjs":
            raise ValueError("OpenClaw entry mismatch")
        package_root = openclaw_entry.parent.resolve(strict=True)
        package_json = package_root / "package.json"
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
        if metadata.get("name") != "openclaw" or metadata.get("version") != expected_version:
            raise ValueError("OpenClaw package mismatch")
        exports = metadata.get("exports")
        target = _export_target(exports.get(_PUBLIC_EXPORT) if isinstance(exports, dict) else None)
        if not target.startswith("./"):
            raise ValueError("GatewayClient public export is unavailable")
        gateway_runtime = (package_root / target[2:]).resolve(strict=True)
        gateway_runtime.relative_to(package_root)
        if gateway_runtime.suffix not in {".js", ".mjs"}:
            raise ValueError("GatewayClient public export target is invalid")
        return GatewayRuntime(
            node_argv=node_argv,
            package_root=package_root,
            openclaw_entry=openclaw_entry,
            gateway_runtime=gateway_runtime,
            version=str(metadata["version"]),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GatewayClientUnavailable("gateway_client_unavailable") from exc


def is_safe_auth_url(value: Any) -> bool:
    """Accept only public OpenAI/ChatGPT HTTPS authorization surfaces."""

    if not isinstance(value, str) or len(value) > 8192:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _AUTH_HOST_SUFFIXES)


def _is_loopback_websocket_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        address = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        return False
    return (
        parsed.scheme == "ws"
        and address.is_loopback
        and port is not None
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )


class OpenAIPlanGatewaySidecar:
    """Supervise one official-GatewayClient Node process for one auth job."""

    def __init__(
        self,
        *,
        profile: str,
        cwd: Path,
        base_cmd_factory: Callable[[], list[str]],
        gateway_url_factory: Callable[[], str],
        browser_opener: Callable[[str], Any] = webbrowser.open,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        environment: dict[str, str] | None = None,
        sidecar_path: Path | None = None,
    ):
        self.profile = profile
        self.cwd = Path(cwd).resolve()
        self._base_cmd_factory = base_cmd_factory
        self._gateway_url_factory = gateway_url_factory
        self._browser_opener = browser_opener
        self._popen_factory = popen_factory
        self._environment = dict(environment) if environment is not None else os.environ.copy()
        self.sidecar_path = (
            Path(sidecar_path).resolve()
            if sidecar_path is not None
            else Path(__file__).resolve().parents[1] / "scripts" / "openai_plan_gateway_sidecar.mjs"
        )

    @staticmethod
    def _stop_sidecar(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - child-only bounded cleanup
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _send(proc: subprocess.Popen[str], payload: dict[str, Any]) -> bool:
        try:
            if proc.stdin is None or proc.poll() is not None:
                return False
            proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError, TypeError):
            return False

    def run(
        self,
        *,
        method: str,
        session_id: str,
        timeout: float,
        cancel_event: threading.Event,
        on_phase: Callable[[str], None] | None = None,
        on_process: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> GatewaySidecarResult:
        if method not in {"siwc", "oauth"}:
            return GatewaySidecarResult("failure", "auth_failed")
        try:
            runtime = resolve_gateway_runtime(list(self._base_cmd_factory()))
            gateway_url = self._gateway_url_factory()
            if not _is_loopback_websocket_url(gateway_url) or not self.sidecar_path.is_file():
                raise GatewayClientUnavailable("gateway_client_unavailable")
        except (GatewayClientUnavailable, OSError, TypeError, ValueError):
            return GatewaySidecarResult("failure", "gateway_client_unavailable")

        env = self._environment.copy()
        env["OPENCLAW_PROFILE"] = self.profile
        argv = [runtime.node_argv, str(self.sidecar_path)]
        proc: subprocess.Popen[str] | None = None
        events: queue.Queue[str | None] = queue.Queue()
        deadline = time.monotonic() + max(0.25, float(timeout))
        cancel_sent = False
        timed_out = False

        try:
            proc = self._popen_factory(
                argv,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=env,
            )
            if on_process:
                on_process(proc)
            if on_phase:
                on_phase("gateway_connecting")

            def read_stdout() -> None:
                assert proc is not None
                if proc.stdout is not None:
                    for line in proc.stdout:
                        events.put(line[:65537])
                events.put(None)

            def discard_stderr() -> None:
                assert proc is not None
                if proc.stderr is not None:
                    for _ in proc.stderr:
                        pass

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=discard_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            started = self._send(
                proc,
                {
                    "command": "start",
                    "profile": self.profile,
                    "method": method,
                    "sessionId": session_id,
                    "gatewayUrl": gateway_url,
                    "packageRoot": str(runtime.package_root),
                    "openclawEntry": str(runtime.openclaw_entry),
                    "expectedVersion": runtime.version,
                },
            )
            if not started:
                return GatewaySidecarResult("failure", "gateway_rpc_failed")

            while True:
                now = time.monotonic()
                if cancel_event.is_set() and not cancel_sent:
                    cancel_sent = True
                    self._send(proc, {"command": "cancel"})
                if now >= deadline and not cancel_sent:
                    timed_out = True
                    cancel_sent = True
                    self._send(proc, {"command": "cancel"})
                    deadline = now + 5
                if now >= deadline and cancel_sent:
                    return GatewaySidecarResult(
                        "failure" if timed_out else "cancelled",
                        "auth_timeout" if timed_out else "auth_cancelled",
                    )
                try:
                    raw = events.get(timeout=0.05)
                except queue.Empty:
                    if proc.poll() is not None and events.empty():
                        return GatewaySidecarResult("failure", "gateway_rpc_failed")
                    continue
                if raw is None:
                    return GatewaySidecarResult("failure", "gateway_rpc_failed")
                if len(raw) > 65536:
                    return GatewaySidecarResult("failure", "gateway_rpc_failed")
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return GatewaySidecarResult("failure", "gateway_rpc_failed")
                event = payload.get("event") if isinstance(payload, dict) else None
                if event not in _SIDECAR_EVENTS:
                    return GatewaySidecarResult("failure", "gateway_rpc_failed")
                if event == "ready":
                    continue
                if event == "waiting":
                    if on_phase:
                        on_phase("waiting")
                    continue
                if event == "open_url":
                    auth_url = payload.get("url")
                    opened = False
                    if is_safe_auth_url(auth_url):
                        try:
                            opened = bool(self._browser_opener(auth_url))
                        except Exception:  # noqa: BLE001 - URL and exception remain private
                            opened = False
                    auth_url = None
                    if opened:
                        if on_phase:
                            on_phase("browser_opened")
                        self._send(proc, {"command": "browser_opened"})
                    else:
                        self._send(proc, {"command": "browser_open_failed"})
                    continue
                if event == "success":
                    return GatewaySidecarResult("success", "")
                if event == "cancelled":
                    return GatewaySidecarResult(
                        "failure" if timed_out else "cancelled",
                        "auth_timeout" if timed_out else "auth_cancelled",
                    )
                code = payload.get("code")
                safe_code = code if isinstance(code, str) and code in _PUBLIC_ERROR_CODES else "gateway_rpc_failed"
                return GatewaySidecarResult("failure", safe_code)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return GatewaySidecarResult("failure", "gateway_rpc_failed")
        finally:
            if proc is not None:
                if proc.poll() is None:
                    self._send(proc, {"command": "shutdown"})
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._stop_sidecar(proc)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass


def _default_ready_probe() -> bool:
    try:
        with urllib.request.urlopen(healthz_url(), timeout=2) as response:  # noqa: S310 - fixed loopback URL
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def ensure_gateway_ready(
    *,
    cwd: Path,
    ready_probe: Callable[[], bool] = _default_ready_probe,
    command_runner: Callable[..., Any] = subprocess.run,
    platform: str = sys.platform,
    timeout: float = 75,
) -> str:
    """Reuse a ready Gateway or invoke the existing bounded start script."""

    if ready_probe():
        return ""
    root = Path(cwd).resolve()
    if platform == "win32":
        script = root / "scripts" / "gateway.ps1"
        argv = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "start",
        ]
    else:
        script = root / "scripts" / "gateway.sh"
        argv = ["bash", str(script), "start"]
    if not script.is_file():
        return "gateway_unavailable"
    try:
        completed = command_runner(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=max(1.0, float(timeout)),
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return "gateway_unavailable"
    if int(getattr(completed, "returncode", 1)) != 0 or not ready_probe():
        return "gateway_unavailable"
    return ""
