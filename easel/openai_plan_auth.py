"""Safe OpenClaw-managed OpenAI plan-auth façade.

OpenClaw is the only credential authority.  This module invokes supported CLI
surfaces and projects their output into a deliberately small allowlist; it
never reads an auth database or stores OAuth material.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from easel.gateway_endpoint import websocket_url
from easel.openclaw_cmd import openclaw_base_cmd
from easel.openai_plan_gateway import OpenAIPlanGatewaySidecar, ensure_gateway_ready


_ALLOWED_METHODS = frozenset({"siwc", "oauth", "device-code"})
_PLAN_PROFILE_TYPE = "oauth"
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$")
_PUBLIC_HANDLE_RE = re.compile(r"^plan_[0-9a-f]{32}$")
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


@dataclass(frozen=True)
class _CredentialAssessment:
    """Safe, secret-free evidence about OpenAI credential routing."""

    billing_source: str
    error_code: str
    exclusive_plan_profile_id: str
    active_profile_id: str
    runtime_status: str
    selected_model: str


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
    phase: str = "gateway_starting"
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


def _safe_display_label(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > 64
        or "@" in text
        or any(ch < " " or ch == "\x7f" for ch in text)
        or not all(ch.isalnum() or ch.isspace() or ch in "._()+-" for ch in text)
    ):
        return "OpenAI plan"
    return text


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
                # OpenClaw's synthesized label can contain profileId/email.
                # Only an explicit, allowlisted displayName is public-safe.
                "display_label": _safe_display_label(row.get("displayName")),
                "unusable": unusable,
            }
        )
    return normalized


def _profile_snapshot(profile: dict[str, str | bool]) -> tuple[str, str, str, bool]:
    return (
        str(profile["id"]),
        str(profile["method"]),
        str(profile["display_label"]),
        bool(profile["unusable"]),
    )


def _diagnostic_could_affect_openai(entry: Any, openai_profile_ids: set[str]) -> bool:
    if isinstance(entry, str):
        value = _safe_text(entry, 256)
        if value in openai_profile_ids or value.lower().startswith("openai:"):
            return True
        if ":" in value or "/" in value:
            return False
        return True
    if not isinstance(entry, dict):
        return True

    scopes: list[bool] = []
    for key in ("provider", "authProvider", "auth_provider", "runtimeProvider", "runtime_provider"):
        value = _safe_text(entry.get(key), 64).lower()
        if value:
            scopes.append(value == "openai")
    for key in ("profileId", "profile_id"):
        value = _safe_profile_id(entry.get(key))
        if value:
            if value in openai_profile_ids or value.lower().startswith("openai:"):
                scopes.append(True)
            elif ":" in value:
                scopes.append(False)
    for key in ("model", "modelRef", "model_ref"):
        value = _safe_text(entry.get(key), 256).lower()
        if value:
            if value.startswith("openai/"):
                scopes.append(True)
            elif "/" in value:
                scopes.append(False)
    return any(scopes) if scopes else True


def _has_openai_diagnostic(value: Any, openai_profile_ids: set[str]) -> bool:
    if value in (None, []):
        return False
    if not isinstance(value, list):
        return True
    return any(_diagnostic_could_affect_openai(row, openai_profile_ids) for row in value)


def _credential_assessment(
    auth_payload: Any,
    status_payload: Any,
    plans: list[dict[str, str | bool]],
) -> _CredentialAssessment:
    """Classify only bounded metadata exposed by OpenClaw's JSON CLIs.

    A plan profile is exclusive only when the auth listing and runtime status
    agree that it is the sole usable OpenAI credential. Any possible Platform
    key or incomplete/contradictory metadata removes that proof.
    """

    rows = auth_payload.get("profiles") if isinstance(auth_payload, dict) else None
    openai_rows = [
        row for row in rows
        if isinstance(row, dict) and row.get("provider") == "openai"
    ] if isinstance(rows, list) else []
    saved_api_keys = sum(row.get("type") in {"api_key", "api-key"} for row in openai_rows)
    saved_tokens = sum(row.get("type") == "token" for row in openai_rows)
    known_rows = sum(
        row.get("type") in {_PLAN_PROFILE_TYPE, "api_key", "api-key", "token"}
        and bool(_safe_profile_id(row.get("id")))
        for row in openai_rows
    )
    ambiguous = (
        not isinstance(rows, list)
        or len(openai_rows) != len(rows)
        or known_rows != len(openai_rows)
        or saved_tokens > 0
    )
    platform_present = saved_api_keys > 0

    status_dict = status_payload if isinstance(status_payload, dict) else {}
    auth_status = status_dict.get("auth")
    if not isinstance(auth_status, dict):
        auth_status = {}
        ambiguous = True

    providers = auth_status.get("providers")
    provider_rows = [
        row for row in providers
        if isinstance(row, dict) and row.get("provider") == "openai"
    ] if isinstance(providers, list) else []
    if len(provider_rows) != 1:
        ambiguous = True
        provider = {}
    else:
        provider = provider_rows[0]

    counts = provider.get("profiles") if isinstance(provider, dict) else None
    expected_counts = {
        "count": len(openai_rows),
        "oauth": len(plans),
        "token": saved_tokens,
        "apiKey": saved_api_keys,
    }
    if not isinstance(counts, dict) or any(
        not isinstance(counts.get(key), int) or counts.get(key) != value
        for key, value in expected_counts.items()
    ):
        ambiguous = True
    elif counts.get("apiKey", 0) > 0:
        platform_present = True

    for source_name in ("env", "modelsJson"):
        if source_name in provider:
            source = provider.get(source_name)
            if source:
                platform_present = True
            else:
                ambiguous = True
    if provider.get("syntheticAuth"):
        ambiguous = True

    effective = provider.get("effective") if isinstance(provider, dict) else None
    effective_kind = effective.get("kind") if isinstance(effective, dict) else ""
    if effective_kind in {"env", "models.json", "modelsJson"}:
        platform_present = True
    elif effective_kind != "profiles":
        ambiguous = True

    fallback = auth_status.get("shellEnvFallback")
    applied_keys = fallback.get("appliedKeys") if isinstance(fallback, dict) else None
    if not isinstance(applied_keys, list):
        ambiguous = True
    elif any(isinstance(key, str) and "OPENAI" in key.upper() for key in applied_keys):
        platform_present = True

    routes = auth_status.get("runtimeAuthRoutes")
    openai_routes = [
        row for row in routes
        if isinstance(row, dict) and row.get("provider") == "openai"
    ] if isinstance(routes, list) else []
    active_profile_id = ""
    runtime_status = "unknown"
    if len(openai_routes) != 1:
        ambiguous = True
    else:
        route = openai_routes[0]
        candidate = route.get("status")
        runtime_status = candidate if candidate in {"usable", "missing", "indeterminate", "unavailable"} else "unknown"
        if runtime_status == "unknown":
            ambiguous = True
        route_effective = route.get("effective")
        if isinstance(route_effective, dict) and route_effective.get("kind") == "profiles":
            candidate_profile_id = _safe_profile_id(route_effective.get("detail"))
            plan_ids = {str(row["id"]) for row in plans}
            if candidate_profile_id in plan_ids:
                active_profile_id = candidate_profile_id
            else:
                ambiguous = True
        else:
            kind = route_effective.get("kind") if isinstance(route_effective, dict) else ""
            if kind in {"env", "models.json", "modelsJson"}:
                platform_present = True
            else:
                ambiguous = True
        if not active_profile_id:
            ambiguous = True

    openai_profile_ids = {
        _safe_profile_id(row.get("id")) for row in openai_rows
        if _safe_profile_id(row.get("id"))
    }
    if _has_openai_diagnostic(auth_status.get("modelRouteIssues"), openai_profile_ids):
        ambiguous = True
    if _has_openai_diagnostic(auth_status.get("unusableProfiles"), openai_profile_ids):
        ambiguous = True

    selected = status_dict.get("resolvedDefault") or status_dict.get("defaultModel")
    selected_model = selected if isinstance(selected, str) and _MODEL_REF_RE.fullmatch(selected) else ""

    if plans and platform_present:
        billing_source = "mixed"
    elif platform_present:
        billing_source = "platform_api"
    elif ambiguous:
        billing_source = "unknown"
    elif plans:
        billing_source = "chatgpt_plan"
    elif openai_rows or ambiguous:
        billing_source = "unknown"
    else:
        billing_source = "none"

    error_code = "platform_fallback_present" if platform_present and plans else "billing_ambiguity" if ambiguous else ""
    exclusive_plan_profile_id = ""
    if (
        not error_code
        and len(plans) == 1
        and not bool(plans[0]["unusable"])
        and active_profile_id == plans[0]["id"]
        and runtime_status == "usable"
    ):
        exclusive_plan_profile_id = str(plans[0]["id"])
    elif not error_code and len(plans) != 1:
        error_code = "billing_ambiguity"

    return _CredentialAssessment(
        billing_source=billing_source,
        error_code=error_code,
        exclusive_plan_profile_id=exclusive_plan_profile_id,
        active_profile_id=active_profile_id,
        runtime_status=runtime_status,
        selected_model=selected_model,
    )


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
        cold_start_retry_timeout: float = 45,
        test_timeout: float = 120,
        max_jobs: int = 32,
        workspace_factory: Callable[[], Any] | None = None,
        gateway_sidecar_factory: Callable[[], Any] | None = None,
        gateway_readiness: Callable[[], str] | None = None,
    ):
        self.profile = profile
        self._base_cmd_factory = base_cmd_factory
        self._runner = runner or BoundedProcessRunner(cwd=cwd)
        self._auth_timeout = auth_timeout
        self._command_timeout = command_timeout
        self._cold_start_retry_timeout = max(
            float(command_timeout),
            float(cold_start_retry_timeout),
        )
        self._test_timeout = test_timeout
        self._max_jobs = max(4, int(max_jobs))
        self._cwd = Path(cwd).resolve() if cwd else Path(__file__).resolve().parents[1]
        self._workspace_factory = workspace_factory or (
            lambda: tempfile.TemporaryDirectory(prefix="easel-openai-plan-test-")
        )
        self._gateway_sidecar_factory = gateway_sidecar_factory or (
            lambda: OpenAIPlanGatewaySidecar(
                profile=self.profile,
                cwd=self._cwd,
                base_cmd_factory=self._base_cmd_factory,
                gateway_url_factory=lambda: websocket_url(self.profile),
            )
        )
        self._gateway_readiness = gateway_readiness or (
            lambda: ensure_gateway_ready(cwd=self._cwd)
        )
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.RLock()
        self._profile_handles: dict[str, tuple[str, tuple[str, str, str, bool]]] = {}

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

    def _run_json_readonly_with_cold_start_retry(
        self,
        parts: list[str],
    ) -> tuple[Any | None, str]:
        payload, error = self._run_json(parts)
        if error != "cli_timeout":
            return payload, error
        return self._run_json(parts, timeout=self._cold_start_retry_timeout)

    def _auth_payload(self) -> tuple[Any | None, str]:
        return self._run_json(["models", "auth", "list", "--provider", "openai", "--json"])

    def _current_plan_profiles(self) -> tuple[list[dict[str, str | bool]], Any | None, str]:
        payload, error = self._auth_payload()
        return _plan_profiles(payload), payload, error

    def _current_credential_assessment(
        self,
    ) -> tuple[list[dict[str, str | bool]], _CredentialAssessment | None, str]:
        plans, auth_payload, auth_error = self._current_plan_profiles()
        if auth_error:
            return plans, None, auth_error
        status_payload, status_error = self._run_json(["models", "status", "--json"])
        if status_error:
            return plans, None, status_error
        return plans, _credential_assessment(auth_payload, status_payload, plans), ""

    def _publish_profile_handles(self, plans: list[dict[str, str | bool]]) -> dict[str, str]:
        with self._lock:
            current = self._profile_handles
            updated: dict[str, tuple[str, tuple[str, str, str, bool]]] = {}
            public: dict[str, str] = {}
            for profile in plans:
                profile_id = str(profile["id"])
                snapshot = _profile_snapshot(profile)
                existing = current.get(profile_id)
                handle = (
                    existing[0]
                    if existing and existing[1] == snapshot
                    else f"plan_{secrets.token_hex(16)}"
                )
                updated[profile_id] = (handle, snapshot)
                public[profile_id] = handle
            self._profile_handles = updated
            return public

    def _resolve_profile_handle(
        self,
        handle: str,
        plans: list[dict[str, str | bool]],
    ) -> str:
        with self._lock:
            matches = [
                (profile_id, binding)
                for profile_id, binding in self._profile_handles.items()
                if binding[0] == handle
            ]
            if len(matches) != 1:
                return ""
            profile_id, binding = matches[0]
            fresh = next((row for row in plans if row["id"] == profile_id), None)
            if fresh is None or _profile_snapshot(fresh) != binding[1]:
                self._profile_handles.pop(profile_id, None)
                return ""
            return profile_id

    def status(self) -> dict[str, Any]:
        auth_payload, auth_error = self._run_json_readonly_with_cold_start_retry(
            ["models", "auth", "list", "--provider", "openai", "--json"]
        )
        plans = _plan_profiles(auth_payload)
        if auth_error:
            self._publish_profile_handles([])
            return {
                "available": False,
                "connected": False,
                "usable": False,
                "reauthRequired": False,
                "authMethod": "unknown",
                "billingSource": "unknown",
                "activeProfileHandle": "",
                "displayLabel": "",
                "selectedModel": "",
                "runtimeStatus": "unknown",
                "errorCode": auth_error,
                "recoveryAction": "check_openclaw",
                "deviceCodeWebSupported": False,
                "profiles": [],
            }

        status_payload, status_error = self._run_json_readonly_with_cold_start_retry(
            ["models", "status", "--json"]
        )
        assessment = _credential_assessment(auth_payload, status_payload, plans)
        active_profile_id = assessment.active_profile_id
        runtime_status = assessment.runtime_status
        selected_model = assessment.selected_model
        public_handles = self._publish_profile_handles(plans)
        active = next((row for row in plans if row["id"] == active_profile_id), plans[0] if len(plans) == 1 else None)
        connected = bool(plans)
        selected_unusable = bool(active["unusable"]) if active else all(bool(row["unusable"]) for row in plans)
        reauth = connected and (selected_unusable or runtime_status in {"missing", "unavailable"})
        usable = (
            connected
            and runtime_status == "usable"
            and not reauth
            and not assessment.error_code
        )
        method = str(active["method"]) if active else "unknown"
        label = str(active["display_label"]) if active else ""
        error_code = status_error or assessment.error_code
        recovery = "check_openclaw" if status_error else (
            "review_openai_billing_sources" if assessment.error_code else "reauthenticate" if reauth else ""
        )
        return {
            "available": not bool(status_error),
            "connected": connected,
            "usable": usable,
            "reauthRequired": reauth,
            "authMethod": method,
            "billingSource": assessment.billing_source,
            "activeProfileHandle": public_handles.get(active_profile_id, ""),
            "displayLabel": label,
            "selectedModel": selected_model,
            "runtimeStatus": runtime_status,
            "errorCode": error_code,
            "recoveryAction": recovery,
            "deviceCodeWebSupported": False,
            "profiles": [
                {
                    "handle": public_handles[str(row["id"])],
                    "displayLabel": row["display_label"],
                    "authMethod": row["method"],
                    "usable": not bool(row["unusable"]) and not bool(assessment.error_code),
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
            "phase": job.phase,
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
                job.phase = "complete"
                job.message = "Device-code sign-in is available from the OpenClaw terminal"
                job.error_code = "device_code_terminal_only"
                job.ended_at = int(time.time())
                return self._public_job(job)
            job.thread = threading.Thread(target=self._run_auth_job, args=(job,), daemon=True)
            job.thread.start()
            return self._public_job(job)

    def _run_auth_job(self, job: _Job) -> None:
        def remember_process(proc: subprocess.Popen[Any]) -> None:
            with self._lock:
                job.process = proc

        def update_phase(phase: str) -> None:
            messages = {
                "gateway_connecting": "Connecting to the local OpenClaw Gateway",
                "waiting": "Waiting for OpenClaw sign-in",
                "browser_opened": "Browser opened for OpenClaw sign-in",
            }
            if phase not in messages:
                return
            with self._lock:
                if job.state == "running":
                    job.phase = phase
                    job.message = messages[phase]

        try:
            readiness_error = self._gateway_readiness()
            if readiness_error:
                with self._lock:
                    job.state = "fail"
                    job.phase = "complete"
                    job.message = "The local OpenClaw Gateway is unavailable"
                    job.error_code = "gateway_unavailable"
                return
            if job.cancel_event.is_set():
                with self._lock:
                    job.state = "cancelled"
                    job.phase = "complete"
                    job.message = "Sign-in cancelled"
                    job.error_code = "auth_cancelled"
                return
            sidecar = self._gateway_sidecar_factory()
            completed = sidecar.run(
                method=job.method,
                session_id=secrets.token_urlsafe(24),
                timeout=self._auth_timeout,
                cancel_event=job.cancel_event,
                on_phase=update_phase,
                on_process=remember_process,
            )
            with self._lock:
                if completed.outcome == "cancelled" or (
                    job.cancel_event.is_set() and completed.outcome != "success"
                ):
                    job.state = "cancelled"
                    job.phase = "complete"
                    job.message = "Sign-in cancelled"
                    job.error_code = "auth_cancelled"
                elif completed.outcome != "success":
                    job.state = "fail"
                    job.phase = "complete"
                    job.message = "OpenClaw sign-in failed"
                    job.error_code = completed.error_code or "gateway_rpc_failed"
                else:
                    # Success is confirmed from a fresh safe auth listing, never
                    # from the Wizard terminal event alone.
                    plans, _, error = self._current_plan_profiles()
                    if plans and not error:
                        job.state = "success"
                        job.phase = "complete"
                        job.message = "OpenAI plan sign-in connected"
                        job.error_code = ""
                    else:
                        job.state = "fail"
                        job.phase = "complete"
                        job.message = "OpenClaw did not confirm an OpenAI plan profile"
                        job.error_code = "auth_not_confirmed"
        except Exception:  # noqa: BLE001 - expose only a bounded generic state
            with self._lock:
                if job.cancel_event.is_set():
                    job.state = "cancelled"
                    job.phase = "complete"
                    job.message = "Sign-in cancelled"
                    job.error_code = "auth_cancelled"
                else:
                    job.state = "fail"
                    job.phase = "complete"
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
        plans, auth_payload, error = self._current_plan_profiles()
        if error:
            return {"status": "unavailable", "models": [], "errorCode": "auth_status_unavailable"}
        if not plans:
            return {"status": "unavailable", "models": [], "errorCode": "plan_auth_required"}
        status_payload, status_error = self._run_json(["models", "status", "--json"])
        if status_error:
            return {"status": "unavailable", "models": [], "errorCode": "auth_status_unavailable"}
        assessment = _credential_assessment(auth_payload, status_payload, plans)
        if assessment.error_code or not assessment.exclusive_plan_profile_id:
            error_code = assessment.error_code or "billing_ambiguity"
            return {"status": "unavailable", "models": [], "errorCode": error_code}
        return self._catalog()

    def test_and_use(self, profile_handle: str, model: str) -> dict[str, Any]:
        if not isinstance(profile_handle, str) or not _PUBLIC_HANDLE_RE.fullmatch(profile_handle):
            raise InvalidRequestError("invalid_profile_handle")
        if not isinstance(model, str) or not _MODEL_REF_RE.fullmatch(model):
            raise InvalidRequestError("invalid_openai_model")

        plans, auth_payload, auth_error = self._current_plan_profiles()
        profile_id = self._resolve_profile_handle(profile_handle, plans)
        if auth_error or not profile_id:
            raise InvalidRequestError("profile_handle_not_current")
        selected_profile = next(row for row in plans if row["id"] == profile_id)
        if bool(selected_profile["unusable"]):
            return self._test_failure("profile_unusable")

        status_payload, status_error = self._run_json(["models", "status", "--json"])
        if status_error:
            return self._test_failure("billing_ambiguity")
        assessment = _credential_assessment(auth_payload, status_payload, plans)
        proof_error = self._credential_proof_error(assessment, profile_id)
        if proof_error:
            return self._test_failure(proof_error)

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

        activated_plans, activated_assessment, activated_error = self._current_credential_assessment()
        if activated_error or activated_assessment is None:
            return self._test_failure("profile_activation_unconfirmed")
        if self._resolve_profile_handle(profile_handle, activated_plans) != profile_id:
            return self._test_failure("profile_activation_unconfirmed")
        proof_error = self._credential_proof_error(activated_assessment, profile_id)
        if proof_error:
            return self._test_failure(
                "profile_activation_unconfirmed" if proof_error == "billing_ambiguity" else proof_error
            )

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
        if self._turn_credential_mismatch(turn_payload, profile_id):
            return self._test_failure("credential_proof_mismatch")

        final_plans, final_assessment, final_error = self._current_credential_assessment()
        if final_error or final_assessment is None:
            return self._test_failure("billing_ambiguity")
        if self._resolve_profile_handle(profile_handle, final_plans) != profile_id:
            return self._test_failure("billing_ambiguity")
        proof_error = self._credential_proof_error(final_assessment, profile_id)
        if proof_error:
            return self._test_failure(proof_error)

        persisted = self._run(["models", "set", model])
        if persisted.outcome != "completed" or persisted.returncode != 0:
            return self._test_failure("model_persist_failed")
        return {
            "ok": True,
            "selectedModel": model,
            "effectiveProvider": "openai",
            "testResult": "success",
            "errorCode": "",
            "credentialProof": "exclusive_plan_profile",
        }

    @staticmethod
    def _credential_proof_error(assessment: _CredentialAssessment, requested_profile_id: str) -> str:
        if assessment.error_code:
            return assessment.error_code
        if assessment.exclusive_plan_profile_id != requested_profile_id:
            return "billing_ambiguity"
        return ""

    @staticmethod
    def _turn_credential_mismatch(payload: Any, requested_profile_id: str) -> bool:
        if not isinstance(payload, dict):
            return True
        actual_profile = payload.get("authProfileId")
        if actual_profile is not None and _safe_profile_id(actual_profile) != requested_profile_id:
            return True
        source = payload.get("credentialSource", payload.get("authSource"))
        if source is not None and source not in {"oauth", "chatgpt_plan", "codex_plan"}:
            return True
        return False

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
            threads = [job.thread for job in running if job.thread is not None]
        for thread in threads:
            thread.join(timeout=5)
        with self._lock:
            remaining = [job.process for job in running if job.process is not None]
        for proc in remaining:
            BoundedProcessRunner._stop_process(proc)
