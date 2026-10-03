"""Safe OpenClaw-managed OpenAI plan-auth façade.

OpenClaw is the only credential authority.  This module invokes supported CLI
surfaces and projects their output into a deliberately small allowlist; it
never reads an auth database or stores OAuth material.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from easel.openclaw_cmd import openclaw_base_cmd


_ALLOWED_METHODS = frozenset({"siwc", "oauth", "device-code"})
_PLAN_PROFILE_TYPE = "oauth"
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$")
_MODEL_REF_RE = re.compile(r"^openai/([A-Za-z0-9][A-Za-z0-9._:+\-]{0,127})$")
_FINAL_JOB_STATES = frozenset({"interaction_required", "success", "fail", "cancelled"})
_TEST_PROMPT = "Reply with exactly OK."


class InvalidRequestError(ValueError):
    """A client value is outside the safe contract."""


class ActiveJobError(RuntimeError):
    """A second auth mutation was requested while one is active."""


class JobNotFoundError(LookupError):
    """A job is absent, including after a server restart."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    outcome: str = "completed"  # completed | timeout | cancelled


class BoundedProcessRunner:
    """Run an argv command without a shell and retain only bounded output."""

    def __init__(self, *, max_capture_bytes: int = 64 * 1024, cwd: Path | None = None):
        self.max_capture_bytes = max(1024, int(max_capture_bytes))
        self.cwd = str(cwd) if cwd else None

    @staticmethod
    def _stop_process(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - cleanup must proceed to kill
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001 - process may already be gone
                pass

    def run(
        self,
        argv: list[str],
        *,
        timeout: float,
        cancel_event: threading.Event | None = None,
        on_process: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> ProcessResult:
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise TypeError("argv must be a non-empty list of strings")
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=self.cwd,
            shell=False,
        )
        if on_process:
            on_process(proc)

        stdout = bytearray()
        stderr = bytearray()

        def drain(stream, target: bytearray) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                remaining = self.max_capture_bytes - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])

        readers = [
            threading.Thread(target=drain, args=(proc.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + max(0.1, float(timeout))
        outcome = "completed"
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                outcome = "cancelled"
                self._stop_process(proc)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                self._stop_process(proc)
                break
            try:
                proc.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue

        for reader in readers:
            reader.join(timeout=2)
        return ProcessResult(
            int(proc.returncode if proc.returncode is not None else -1),
            bytes(stdout).decode("utf-8", errors="replace"),
            bytes(stderr).decode("utf-8", errors="replace"),
            outcome,
        )


@dataclass
class _Job:
    job_id: str
    method: str
    state: str = "running"
    message: str = "Waiting for OpenClaw sign-in"
    error_code: str = ""
    started_at: int = field(default_factory=lambda: int(time.time()))
    ended_at: int | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)


def _safe_text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.strip() if ch >= " " and ch != "\x7f")[:limit]


def _safe_profile_id(value: Any) -> str:
    text = _safe_text(value, 128)
    return text if _PROFILE_ID_RE.fullmatch(text) else ""


def _plan_profiles(payload: Any) -> list[dict[str, str | bool]]:
    rows = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    normalized: list[dict[str, str | bool]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("provider") != "openai" or row.get("type") != _PLAN_PROFILE_TYPE:
            continue
        profile_id = _safe_profile_id(row.get("id"))
        if not profile_id:
            continue
        method = row.get("method") if row.get("method") in _ALLOWED_METHODS else "unknown"
        expires_at = row.get("expiresAt")
        expired = False
        if isinstance(expires_at, str):
            try:
                expired = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
            except (TypeError, ValueError):
                # An explicitly present but malformed expiry is not safe to
                # treat as healthy.
                expired = True
        unusable = bool(row.get("disabledUntil") or row.get("cooldownUntil") or expired)
        normalized.append(
            {
                "id": profile_id,
                "method": method,
                "label": _safe_text(row.get("label") or row.get("displayName")),
                "unusable": unusable,
            }
        )
    return normalized


def _billing_source(payload: Any, plans: list[dict[str, str | bool]]) -> str:
    rows = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return "unknown"
    api_key = any(
        isinstance(row, dict)
        and row.get("provider") == "openai"
        and row.get("type") in {"api_key", "api-key"}
        for row in rows
    )
    unknown = any(
        isinstance(row, dict)
        and row.get("provider") == "openai"
        and row.get("type") not in {_PLAN_PROFILE_TYPE, "api_key", "api-key"}
        for row in rows
    )
    if plans and api_key:
        return "mixed"
    if plans:
        return "chatgpt_plan"
    if api_key:
        return "platform_api"
    if unknown:
        return "unknown"
    return "none"


class OpenAIPlanAuthFacade:
    """Normalize OpenClaw OpenAI plan-auth status and orchestrate safe jobs."""

    def __init__(
        self,
        *,
        profile: str = "easel",
        base_cmd_factory: Callable[[], list[str]] = openclaw_base_cmd,
        runner: Any | None = None,
        cwd: Path | None = None,
        auth_timeout: float = 600,
        command_timeout: float = 30,
        test_timeout: float = 120,
        max_jobs: int = 32,
        workspace_factory: Callable[[], Any] | None = None,
    ):
        self.profile = profile
        self._base_cmd_factory = base_cmd_factory
        self._runner = runner or BoundedProcessRunner(cwd=cwd)
        self._auth_timeout = auth_timeout
        self._command_timeout = command_timeout
        self._test_timeout = test_timeout
        self._max_jobs = max(4, int(max_jobs))
        self._workspace_factory = workspace_factory or (
            lambda: tempfile.TemporaryDirectory(prefix="easel-openai-plan-test-")
        )
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.RLock()

    def _argv(self, *parts: str) -> list[str]:
        return list(self._base_cmd_factory()) + ["--profile", self.profile, *parts]

    def _run(self, parts: list[str], *, timeout: float | None = None) -> ProcessResult:
        return self._runner.run(
            self._argv(*parts),
            timeout=timeout if timeout is not None else self._command_timeout,
        )

    def _run_json(self, parts: list[str], *, timeout: float | None = None) -> tuple[Any | None, str]:
        try:
            completed = self._run(parts, timeout=timeout)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return None, "cli_unavailable"
        if completed.outcome == "timeout":
            return None, "cli_timeout"
        if completed.outcome != "completed" or completed.returncode != 0:
            return None, "cli_failed"
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return None, "invalid_cli_json"
        return payload, ""

    def _auth_payload(self) -> tuple[Any | None, str]:
        return self._run_json(["models", "auth", "list", "--provider", "openai", "--json"])

    def _current_plan_profiles(self) -> tuple[list[dict[str, str | bool]], Any | None, str]:
        payload, error = self._auth_payload()
        return _plan_profiles(payload), payload, error

    def status(self) -> dict[str, Any]:
        plans, auth_payload, auth_error = self._current_plan_profiles()
        if auth_error:
            return {
                "available": False,
                "connected": False,
                "usable": False,
                "reauthRequired": False,
                "authMethod": "unknown",
                "billingSource": "unknown",
                "activeProfileId": "",
                "displayLabel": "",
                "selectedModel": "",
                "runtimeStatus": "unknown",
                "errorCode": auth_error,
                "recoveryAction": "check_openclaw",
                "deviceCodeWebSupported": False,
                "profiles": [],
            }

        status_payload, status_error = self._run_json(["models", "status", "--json"])
        status_dict = status_payload if isinstance(status_payload, dict) else {}
        plan_ids = {str(row["id"]) for row in plans}
        active_profile_id = ""
        runtime_status = "unknown"
        routes = (status_dict.get("auth") or {}).get("runtimeAuthRoutes") if isinstance(status_dict.get("auth"), dict) else []
        if isinstance(routes, list):
            route = next((row for row in routes if isinstance(row, dict) and row.get("provider") == "openai"), None)
            if route:
                candidate = route.get("status")
                runtime_status = candidate if candidate in {"usable", "missing", "indeterminate", "unavailable"} else "unknown"
                effective = route.get("effective")
                detail = _safe_profile_id(effective.get("detail")) if isinstance(effective, dict) and effective.get("kind") == "profiles" else ""
                if detail in plan_ids:
                    active_profile_id = detail

        selected = status_dict.get("resolvedDefault") or status_dict.get("defaultModel")
        selected_model = selected if isinstance(selected, str) and _MODEL_REF_RE.fullmatch(selected) else ""
        active = next((row for row in plans if row["id"] == active_profile_id), plans[0] if len(plans) == 1 else None)
        connected = bool(plans)
        selected_unusable = bool(active["unusable"]) if active else all(bool(row["unusable"]) for row in plans)
        reauth = connected and (selected_unusable or runtime_status in {"missing", "unavailable"})
        usable = connected and runtime_status == "usable" and not reauth
        method = str(active["method"]) if active else "unknown"
        label = str(active["label"]) if active else ""
        error_code = status_error
        recovery = "check_openclaw" if status_error else ("reauthenticate" if reauth else "")
        return {
            "available": not bool(status_error),
            "connected": connected,
            "usable": usable,
            "reauthRequired": reauth,
            "authMethod": method,
            "billingSource": _billing_source(auth_payload, plans),
            "activeProfileId": active_profile_id,
            "displayLabel": label,
            "selectedModel": selected_model,
            "runtimeStatus": runtime_status,
            "errorCode": error_code,
            "recoveryAction": recovery,
            "deviceCodeWebSupported": False,
            "profiles": [
                {
                    "id": row["id"],
                    "displayLabel": row["label"],
                    "authMethod": row["method"],
                    "usable": not bool(row["unusable"]),
                }
                for row in plans
            ],
        }

    def _prune_jobs_locked(self) -> None:
        if len(self._jobs) < self._max_jobs:
            return
        finished = sorted(
            (job for job in self._jobs.values() if job.state in _FINAL_JOB_STATES),
            key=lambda job: job.ended_at or job.started_at,
        )
        for job in finished[: max(1, len(self._jobs) - self._max_jobs + 1)]:
            self._jobs.pop(job.job_id, None)

    @staticmethod
    def _public_job(job: _Job) -> dict[str, Any]:
        response: dict[str, Any] = {
            "jobId": job.job_id,
            "state": job.state,
            "method": job.method,
            "message": job.message,
            "errorCode": job.error_code,
        }
        if job.method == "device-code":
            response["deviceCodeWebSupported"] = False
        return response

    def start_connect(self, method: str) -> dict[str, Any]:
        if method not in _ALLOWED_METHODS:
            raise InvalidRequestError("unsupported_auth_method")
        with self._lock:
            if any(job.state == "running" for job in self._jobs.values()):
                raise ActiveJobError("auth_job_already_running")
            self._prune_jobs_locked()
            job = _Job(job_id=uuid.uuid4().hex, method=method)
            self._jobs[job.job_id] = job
            if method == "device-code":
                job.state = "interaction_required"
                job.message = "Device-code sign-in is available from the OpenClaw terminal"
                job.error_code = "device_code_terminal_only"
                job.ended_at = int(time.time())
                return self._public_job(job)
            job.thread = threading.Thread(target=self._run_auth_job, args=(job,), daemon=True)
            job.thread.start()
            return self._public_job(job)

    def _run_auth_job(self, job: _Job) -> None:
        def remember_process(proc: subprocess.Popen[bytes]) -> None:
            with self._lock:
                job.process = proc

        try:
            completed = self._runner.run(
                self._argv("models", "auth", "login", "--provider", "openai", "--method", job.method),
                timeout=self._auth_timeout,
                cancel_event=job.cancel_event,
                on_process=remember_process,
            )
            with self._lock:
                if completed.outcome == "cancelled" or job.cancel_event.is_set():
                    job.state = "cancelled"
                    job.message = "Sign-in cancelled"
                    job.error_code = "auth_cancelled"
                elif completed.outcome == "timeout":
                    job.state = "fail"
                    job.message = "OpenClaw sign-in timed out"
                    job.error_code = "auth_timeout"
                elif completed.returncode != 0:
                    job.state = "fail"
                    job.message = "OpenClaw sign-in failed"
                    job.error_code = "auth_failed"
                else:
                    # Success is confirmed from a fresh safe auth listing, never
                    # from potentially sensitive interactive command output.
                    plans, _, error = self._current_plan_profiles()
                    if plans and not error:
                        job.state = "success"
                        job.message = "OpenAI plan sign-in connected"
                        job.error_code = ""
                    else:
                        job.state = "fail"
                        job.message = "OpenClaw did not confirm an OpenAI plan profile"
                        job.error_code = "auth_not_confirmed"
        except Exception:  # noqa: BLE001 - expose only a bounded generic state
            with self._lock:
                if job.cancel_event.is_set():
                    job.state = "cancelled"
                    job.message = "Sign-in cancelled"
                    job.error_code = "auth_cancelled"
                else:
                    job.state = "fail"
                    job.message = "OpenClaw sign-in could not be started"
                    job.error_code = "auth_process_error"
        finally:
            with self._lock:
                job.process = None
                job.ended_at = int(time.time())

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFoundError("auth_job_not_found")
            return self._public_job(job)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFoundError("auth_job_not_found")
            if job.state == "running":
                job.cancel_event.set()
                proc = job.process
                if proc is not None:
                    BoundedProcessRunner._stop_process(proc)
            return self._public_job(job)

    def _catalog(self) -> dict[str, Any]:
        payload, error = self._run_json(["models", "list", "--provider", "openai", "--json"])
        if error or not isinstance(payload, dict):
            return {"status": "unavailable", "models": [], "errorCode": "catalog_unavailable"}
        if payload.get("stale") is True:
            return {"status": "stale", "models": [], "errorCode": "catalog_stale"}
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return {"status": "unavailable", "models": [], "errorCode": "catalog_unavailable"}
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in raw_models:
            if not isinstance(row, dict):
                continue
            ref = row.get("key") or row.get("ref")
            if not isinstance(ref, str):
                provider = row.get("provider")
                model_id = row.get("id")
                ref = f"{provider}/{model_id}" if isinstance(provider, str) and isinstance(model_id, str) else ""
            match = _MODEL_REF_RE.fullmatch(ref)
            if not match or ref in seen or not isinstance(row.get("available"), bool):
                continue
            seen.add(ref)
            model_id = match.group(1)
            name = _safe_text(row.get("name") or row.get("displayName") or model_id)
            models.append(
                {
                    "provider": "openai",
                    "ref": ref,
                    "id": model_id,
                    "name": name,
                    "displayName": name,
                    "availability": "available" if row["available"] else "unavailable",
                }
            )
        if not models:
            return {"status": "empty", "models": [], "errorCode": "catalog_empty"}
        return {"status": "available", "models": models, "errorCode": ""}

    def models(self) -> dict[str, Any]:
        plans, _, error = self._current_plan_profiles()
        if error:
            return {"status": "unavailable", "models": [], "errorCode": "auth_status_unavailable"}
        if not plans:
            return {"status": "unavailable", "models": [], "errorCode": "plan_auth_required"}
        return self._catalog()

    def test_and_use(self, profile_id: str, model: str) -> dict[str, Any]:
        plans, _, auth_error = self._current_plan_profiles()
        current_ids = {str(row["id"]) for row in plans}
        if auth_error or profile_id not in current_ids:
            raise InvalidRequestError("profile_not_current_plan")
        if not isinstance(model, str) or not _MODEL_REF_RE.fullmatch(model):
            raise InvalidRequestError("invalid_openai_model")

        # A fresh catalog check is intentionally separate from the profile
        # check above: both facts must still be true at mutation time.
        catalog = self._catalog()
        allowed = {
            row["ref"]
            for row in catalog.get("models", [])
            if row.get("availability") == "available"
        }
        if catalog.get("status") != "available" or model not in allowed:
            raise InvalidRequestError("model_not_in_current_catalog")

        activated = self._run(["models", "auth", "activate", profile_id])
        if activated.outcome != "completed" or activated.returncode != 0:
            return self._test_failure("profile_activation_failed")

        # Use an empty one-shot workspace so this proof cannot include Easel's
        # repository, user files, or chat history in model context.
        with self._workspace_factory() as test_workspace:
            turn_payload, turn_error = self._run_json(
                [
                    "agent", "exec", _TEST_PROMPT,
                    "--cwd", str(test_workspace),
                    "--model", model,
                    "--code-mode", "direct",
                    "--thinking", "off",
                    "--timeout", str(int(self._test_timeout)),
                    "--json",
                ],
                timeout=self._test_timeout + 15,
            )
        if turn_error or not self._turn_proves_model(turn_payload, model):
            return self._test_failure("model_test_unproven")

        persisted = self._run(["models", "set", model])
        if persisted.outcome != "completed" or persisted.returncode != 0:
            return self._test_failure("model_persist_failed")
        return {
            "ok": True,
            "selectedModel": model,
            "effectiveProvider": "openai",
            "testResult": "success",
            "errorCode": "",
        }

    @staticmethod
    def _turn_proves_model(payload: Any, requested: str) -> bool:
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("provider") != "openai":
            return False
        model = payload.get("model")
        effective = model if isinstance(model, str) and model.startswith("openai/") else f"openai/{model}" if isinstance(model, str) else ""
        return effective == requested

    @staticmethod
    def _test_failure(error_code: str) -> dict[str, Any]:
        return {
            "ok": False,
            "selectedModel": "",
            "effectiveProvider": "",
            "testResult": "fail",
            "errorCode": error_code,
        }

    def shutdown(self) -> None:
        with self._lock:
            running = [job for job in self._jobs.values() if job.state == "running"]
            for job in running:
                job.cancel_event.set()
                if job.process is not None:
                    BoundedProcessRunner._stop_process(job.process)
            threads = [job.thread for job in running if job.thread is not None]
        for thread in threads:
            thread.join(timeout=5)
