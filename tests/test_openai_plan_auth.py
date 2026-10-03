from __future__ import annotations

import io
import json
import threading
import time
from contextlib import nullcontext

import pytest

from easel.openai_plan_auth import (
    ActiveJobError,
    BoundedProcessRunner,
    InvalidRequestError,
    OpenAIPlanAuthFacade,
    ProcessResult,
)


PLAN = {
    "agentId": "main",
    "profiles": [
        {
            "id": "openai:work",
            "provider": "openai",
            "type": "oauth",
            "label": "OpenAI plan",
        }
    ],
}
API_KEY = {
    "profiles": [
        {
            "id": "openai:api-key",
            "provider": "openai",
            "type": "api_key",
            "label": "Platform key",
        }
    ],
}
STATUS = {
    "defaultModel": "openai/gpt-6-astra",
    "resolvedDefault": "openai/gpt-6-astra",
    "auth": {
        "shellEnvFallback": {"enabled": False, "appliedKeys": []},
        "providers": [
            {
                "provider": "openai",
                "effective": {"kind": "profiles", "detail": "auth-store"},
                "profiles": {
                    "count": 1,
                    "oauth": 1,
                    "token": 0,
                    "apiKey": 0,
                    "labels": ["openai:work=OAuth"],
                },
            }
        ],
        "modelRouteIssues": [],
        "unusableProfiles": [],
        "runtimeAuthRoutes": [
            {
                "provider": "openai",
                "runtime": "codex",
                "status": "usable",
                "effective": {"kind": "profiles", "detail": "openai:work"},
            }
        ]
    },
}


def status_with_openai(**updates):
    payload = json.loads(json.dumps(STATUS))
    provider = payload["auth"]["providers"][0]
    provider.update(updates)
    return payload


def status_with_saved_api_key():
    payload = json.loads(json.dumps(STATUS))
    payload["auth"]["providers"][0]["profiles"] = {
        "count": 2,
        "oauth": 1,
        "token": 0,
        "apiKey": 1,
        "labels": ["openai:work=OAuth", "openai:api-key=masked"],
    }
    return payload
CATALOG = {
    "count": 3,
    "models": [
        {
            "key": "openai/gpt-6-astra",
            "name": "GPT-6 Astra",
            "available": True,
            "contextWindow": 200000,
            "secretRef": "TOKEN_SENTINEL",
        },
        {"key": "openai/gpt-disabled", "name": "Disabled", "available": False},
        {"key": "evil/model", "name": "Wrong provider", "available": True},
    ],
}


def result(payload=None, *, code=0, stderr=""):
    stdout = json.dumps(payload) if payload is not None else ""
    return ProcessResult(code, stdout, stderr, "completed")


class QueueRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run(self, argv, *, timeout, cancel_event=None, on_process=None):
        self.calls.append((list(argv), timeout, cancel_event))
        if not self.results:
            raise AssertionError(f"unexpected command: {argv}")
        item = self.results.pop(0)
        if callable(item):
            return item(argv, cancel_event)
        return item


def facade(runner, **kwargs):
    return OpenAIPlanAuthFacade(
        profile="easel",
        base_cmd_factory=lambda: ["node.exe", r"C:\npm\openclaw.mjs"],
        runner=runner,
        workspace_factory=lambda: nullcontext(r"C:\empty-test-workspace"),
        **kwargs,
    )


def wait_done(auth, job_id, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = auth.get_job(job_id)
        if job["state"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_status_normalizes_plan_without_raw_or_secrets():
    raw_plan = json.loads(json.dumps(PLAN))
    raw_plan["profiles"][0].update(
        access_token="ACCESS_SENTINEL",
        refresh_token="REFRESH_SENTINEL",
        email="private@example.test",
    )
    runner = QueueRunner(result(raw_plan), result(STATUS))

    body = facade(runner).status()

    assert body == {
        "available": True,
        "connected": True,
        "usable": True,
        "reauthRequired": False,
        "authMethod": "unknown",
        "billingSource": "chatgpt_plan",
        "activeProfileId": "openai:work",
        "displayLabel": "OpenAI plan",
        "selectedModel": "openai/gpt-6-astra",
        "runtimeStatus": "usable",
        "errorCode": "",
        "recoveryAction": "",
        "deviceCodeWebSupported": False,
        "profiles": [
            {
                "id": "openai:work",
                "displayLabel": "OpenAI plan",
                "authMethod": "unknown",
                "usable": True,
            }
        ],
    }
    encoded = json.dumps(body)
    for sentinel in ("ACCESS_SENTINEL", "REFRESH_SENTINEL", "private@example.test", "authStatePath"):
        assert sentinel not in encoded


def test_api_key_is_not_plan_and_mixed_is_explicit():
    auth = facade(QueueRunner(result(API_KEY), result(STATUS)))
    only_key = auth.status()
    assert only_key["connected"] is False
    assert only_key["usable"] is False
    assert only_key["billingSource"] == "platform_api"

    mixed = {"profiles": PLAN["profiles"] + API_KEY["profiles"]}
    body = facade(QueueRunner(result(mixed), result(STATUS))).status()
    assert body["connected"] is True
    assert body["billingSource"] == "mixed"
    assert body["activeProfileId"] == "openai:work"


@pytest.mark.parametrize(
    "platform_status",
    [
        status_with_openai(
            env={"value": "sk-…masked", "source": "OPENAI_API_KEY"},
            effective={"kind": "env", "detail": "sk-…masked"},
        ),
        status_with_openai(
            modelsJson={"value": "sk-…masked", "source": "models.json: <safe>"},
            effective={"kind": "models.json", "detail": "sk-…masked"},
        ),
    ],
)
def test_status_never_labels_runtime_platform_evidence_as_plan_only(platform_status):
    body = facade(QueueRunner(result(PLAN), result(platform_status))).status()
    assert body["billingSource"] == "mixed"
    assert body["usable"] is False
    assert body["errorCode"] == "platform_fallback_present"


def test_expired_plan_is_connected_but_requires_reauth():
    expired = json.loads(json.dumps(PLAN))
    expired["profiles"][0]["expiresAt"] = "2000-01-01T00:00:00Z"
    body = facade(QueueRunner(result(expired), result(STATUS))).status()
    assert body["connected"] is True
    assert body["usable"] is False
    assert body["reauthRequired"] is True
    assert body["profiles"][0]["usable"] is False


def test_expired_secondary_profile_does_not_hide_usable_active_profile():
    profiles = json.loads(json.dumps(PLAN))
    profiles["profiles"].append(
        {
            "id": "openai:expired",
            "provider": "openai",
            "type": "oauth",
            "label": "Old plan",
            "expiresAt": "2000-01-01T00:00:00Z",
        }
    )
    matching_status = json.loads(json.dumps(STATUS))
    matching_status["auth"]["providers"][0]["profiles"] = {
        "count": 2,
        "oauth": 2,
        "token": 0,
        "apiKey": 0,
        "labels": ["openai:work=OAuth", "openai:expired=OAuth"],
    }
    body = facade(QueueRunner(result(profiles), result(matching_status))).status()
    assert body["activeProfileId"] == "openai:work"
    assert body["usable"] is True
    assert body["reauthRequired"] is False


def test_incomplete_status_metadata_is_not_labeled_plan_only():
    ambiguous = {"auth": {"runtimeAuthRoutes": STATUS["auth"]["runtimeAuthRoutes"]}}
    body = facade(QueueRunner(result(PLAN), result(ambiguous))).status()
    assert body["billingSource"] == "unknown"
    assert body["errorCode"] == "billing_ambiguity"


@pytest.mark.parametrize("method", ["api-key", "token", "anything", "", "oauth "])
def test_connect_rejects_non_allowlisted_methods(method):
    with pytest.raises(InvalidRequestError):
        facade(QueueRunner()).start_connect(method)


@pytest.mark.parametrize("method", ["siwc", "oauth"])
def test_connect_uses_exact_safe_argv_and_never_exposes_raw_output(method):
    runner = QueueRunner(
        ProcessResult(0, "ACCESS_SENTINEL", "Authorization: Bearer SECRET", "completed"),
        result(PLAN),
    )
    auth = facade(runner)

    job = wait_done(auth, auth.start_connect(method)["jobId"])

    assert runner.calls[0][0] == [
        "node.exe", r"C:\npm\openclaw.mjs", "--profile", "easel",
        "models", "auth", "login", "--provider", "openai", "--method", method,
    ]
    command = " ".join(runner.calls[0][0])
    assert "--force" not in command
    assert "--set-default" not in command
    assert "api-key" not in command
    assert job["state"] == "success"
    assert "ACCESS_SENTINEL" not in json.dumps(job)
    assert "Authorization" not in json.dumps(job)


def test_only_one_active_mutation_job_and_cancel_cleanup():
    started = threading.Event()

    def block(_argv, cancel_event):
        started.set()
        assert cancel_event is not None
        cancel_event.wait(2)
        return ProcessResult(-1, "TOKEN_SENTINEL", "", "cancelled")

    auth = facade(QueueRunner(block))
    first = auth.start_connect("siwc")
    assert started.wait(1)
    with pytest.raises(ActiveJobError):
        auth.start_connect("oauth")

    cancelled = auth.cancel_job(first["jobId"])
    assert cancelled["state"] in {"running", "cancelled"}
    assert wait_done(auth, first["jobId"])["state"] == "cancelled"
    auth.shutdown()


@pytest.mark.parametrize(
    ("process_result", "state", "error_code"),
    [
        (ProcessResult(1, "", "TOKEN_SENTINEL", "completed"), "fail", "auth_failed"),
        (ProcessResult(-1, "", "", "timeout"), "fail", "auth_timeout"),
        (ProcessResult(-1, "", "", "cancelled"), "cancelled", "auth_cancelled"),
    ],
)
def test_connect_failure_timeout_cancel_are_bounded(process_result, state, error_code):
    auth = facade(QueueRunner(process_result))
    job = wait_done(auth, auth.start_connect("oauth")["jobId"])
    assert job["state"] == state
    assert job["errorCode"] == error_code
    assert "TOKEN_SENTINEL" not in json.dumps(job)


def test_device_code_is_safe_terminal_only_fallback_without_spawning():
    runner = QueueRunner()
    auth = facade(runner)
    started = auth.start_connect("device-code")
    job = auth.get_job(started["jobId"])
    assert job["state"] == "interaction_required"
    assert job["deviceCodeWebSupported"] is False
    assert job["errorCode"] == "device_code_terminal_only"
    assert runner.calls == []


def test_catalog_filters_provider_and_fields_and_fails_closed():
    body = facade(QueueRunner(result(PLAN), result(STATUS), result(CATALOG))).models()
    assert body == {
        "status": "available",
        "models": [
            {
                "provider": "openai",
                "ref": "openai/gpt-6-astra",
                "id": "gpt-6-astra",
                "name": "GPT-6 Astra",
                "displayName": "GPT-6 Astra",
                "availability": "available",
            },
            {
                "provider": "openai",
                "ref": "openai/gpt-disabled",
                "id": "gpt-disabled",
                "name": "Disabled",
                "displayName": "Disabled",
                "availability": "unavailable",
            },
        ],
        "errorCode": "",
    }
    assert "TOKEN_SENTINEL" not in json.dumps(body)

    stale = facade(QueueRunner(result(PLAN), result(STATUS), result({"stale": True, "models": CATALOG["models"]}))).models()
    assert stale == {"status": "stale", "models": [], "errorCode": "catalog_stale"}
    empty = facade(QueueRunner(result(PLAN), result(STATUS), result({"models": []}))).models()
    assert empty == {"status": "empty", "models": [], "errorCode": "catalog_empty"}
    unavailable = facade(QueueRunner(result(PLAN), result(STATUS), ProcessResult(1, "", "SECRET", "completed"))).models()
    assert unavailable == {"status": "unavailable", "models": [], "errorCode": "catalog_unavailable"}


def test_catalog_requires_current_plan_profile():
    body = facade(QueueRunner(result(API_KEY))).models()
    assert body == {"status": "unavailable", "models": [], "errorCode": "plan_auth_required"}


def test_mixed_catalog_fails_closed_without_discovery():
    mixed = {"profiles": PLAN["profiles"] + API_KEY["profiles"]}
    runner = QueueRunner(result(mixed), result(status_with_saved_api_key()))
    body = facade(runner).models()
    assert body == {"status": "unavailable", "models": [], "errorCode": "platform_fallback_present"}
    assert len(runner.calls) == 2


def test_runtime_platform_evidence_blocks_catalog_without_entitlement_rows():
    platform_status = status_with_openai(
        env={"value": "TOKEN_SENTINEL", "source": "OPENAI_API_KEY"},
        effective={"kind": "env", "detail": "TOKEN_SENTINEL"},
    )
    runner = QueueRunner(result(PLAN), result(platform_status))
    body = facade(runner).models()
    assert body == {"status": "unavailable", "models": [], "errorCode": "platform_fallback_present"}
    assert "TOKEN_SENTINEL" not in json.dumps(body)
    assert len(runner.calls) == 2


def test_test_use_rechecks_profile_catalog_activates_turn_then_persists():
    turn = {
        "ok": True,
        "status": "ok",
        "provider": "openai",
        "model": "gpt-6-astra",
        "authProfileId": "openai:work",
    }
    runner = QueueRunner(
        result(PLAN),
        result(STATUS),
        result(CATALOG),
        ProcessResult(0, "activated", "", "completed"),
        result(PLAN), result(STATUS),
        result(turn),
        result(PLAN), result(STATUS),
        ProcessResult(0, "set", "", "completed"),
    )
    auth = facade(runner)

    body = auth.test_and_use("openai:work", "openai/gpt-6-astra")

    assert body == {
        "ok": True,
        "selectedModel": "openai/gpt-6-astra",
        "effectiveProvider": "openai",
        "testResult": "success",
        "errorCode": "",
        "credentialProof": "exclusive_plan_profile",
    }
    calls = [c[0] for c in runner.calls]
    assert calls[0][-6:] == ["models", "auth", "list", "--provider", "openai", "--json"]
    assert calls[1][-3:] == ["models", "status", "--json"]
    assert calls[2][-5:] == ["models", "list", "--provider", "openai", "--json"]
    assert calls[3][-4:] == ["models", "auth", "activate", "openai:work"]
    turn_call = calls[6]
    assert turn_call[4:6] == ["agent", "exec"]
    assert turn_call[turn_call.index("--cwd") + 1] == r"C:\empty-test-workspace"
    assert "--model" in turn_call and turn_call[turn_call.index("--model") + 1] == "openai/gpt-6-astra"
    assert "--fallback" not in turn_call
    assert calls[9][-3:] == ["models", "set", "openai/gpt-6-astra"]


def test_saved_api_key_blocks_test_use_before_catalog_inference_or_persist():
    mixed = {"profiles": PLAN["profiles"] + API_KEY["profiles"]}
    runner = QueueRunner(result(mixed), result(status_with_saved_api_key()))
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "platform_fallback_present"
    assert len(runner.calls) == 2


def test_runtime_platform_key_blocks_test_use_before_inference():
    platform_status = status_with_openai(
        modelsJson={"value": "TOKEN_SENTINEL", "source": "models.json: <safe>"},
        effective={"kind": "models.json", "detail": "TOKEN_SENTINEL"},
    )
    runner = QueueRunner(result(PLAN), result(platform_status))
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "platform_fallback_present"
    assert "TOKEN_SENTINEL" not in json.dumps(body)
    assert len(runner.calls) == 2


def test_unusable_requested_plan_profile_never_runs_inference():
    unusable = json.loads(json.dumps(PLAN))
    unusable["profiles"][0]["cooldownUntil"] = "2099-01-01T00:00:00Z"
    runner = QueueRunner(result(unusable))
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "profile_unusable"
    assert len(runner.calls) == 1


def test_no_safe_credential_proof_fails_closed_before_catalog():
    ambiguous = {"defaultModel": "openai/gpt-6-astra", "auth": {"runtimeAuthRoutes": []}}
    runner = QueueRunner(result(PLAN), result(ambiguous))
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "billing_ambiguity"
    assert len(runner.calls) == 2


def test_multiple_plan_profiles_cannot_prove_the_requested_profile():
    profiles = json.loads(json.dumps(PLAN))
    profiles["profiles"].append(
        {"id": "openai:other", "provider": "openai", "type": "oauth"}
    )
    matching_status = json.loads(json.dumps(STATUS))
    matching_status["auth"]["providers"][0]["profiles"].update(count=2, oauth=2)
    runner = QueueRunner(result(profiles), result(matching_status))
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "billing_ambiguity"
    assert len(runner.calls) == 2


def test_activation_must_confirm_requested_profile_before_inference():
    wrong_route = json.loads(json.dumps(STATUS))
    wrong_route["auth"]["runtimeAuthRoutes"][0]["effective"]["detail"] = "openai:other"
    runner = QueueRunner(
        result(PLAN), result(STATUS), result(CATALOG),
        ProcessResult(0, "", "", "completed"),
        result(PLAN), result(wrong_route),
    )
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "profile_activation_unconfirmed"
    assert not any(call[0][4:6] == ["agent", "exec"] for call in runner.calls)


@pytest.mark.parametrize(
    "credential_fields",
    [
        {"authProfileId": "openai:api-key"},
        {"authProfileId": "openai:work", "credentialSource": "api_key"},
    ],
)
def test_wrong_actual_profile_identity_or_source_cannot_pass_or_persist(credential_fields):
    turn = {
        "ok": True,
        "provider": "openai",
        "model": "gpt-6-astra",
        **credential_fields,
    }
    runner = QueueRunner(
        result(PLAN), result(STATUS), result(CATALOG),
        ProcessResult(0, "", "", "completed"),
        result(PLAN), result(STATUS), result(turn),
    )
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "credential_proof_mismatch"
    assert not any(call[0][-3:-1] == ["models", "set"] for call in runner.calls)


def test_credential_source_added_during_turn_blocks_persist():
    turn = {"ok": True, "provider": "openai", "model": "gpt-6-astra"}
    platform_status = status_with_openai(
        env={"value": "sk-…masked", "source": "OPENAI_API_KEY"},
        effective={"kind": "env", "detail": "sk-…masked"},
    )
    runner = QueueRunner(
        result(PLAN), result(STATUS), result(CATALOG),
        ProcessResult(0, "", "", "completed"),
        result(PLAN), result(STATUS), result(turn),
        result(PLAN), result(platform_status),
    )
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "platform_fallback_present"
    assert not any(call[0][-3:-1] == ["models", "set"] for call in runner.calls)


@pytest.mark.parametrize(
    "turn",
    [
        {"ok": False, "provider": "openai", "model": "gpt-6-astra"},
        {"ok": True, "provider": "other", "model": "gpt-6-astra"},
        {"ok": True, "provider": "openai", "model": "wrong"},
        {"ok": True, "provider": "openai"},
    ],
)
def test_failed_or_unprovable_turn_never_persists_model(turn):
    runner = QueueRunner(
        result(PLAN), result(STATUS), result(CATALOG),
        ProcessResult(0, "", "", "completed"),
        result(PLAN), result(STATUS), result(turn),
    )
    body = facade(runner).test_and_use("openai:work", "openai/gpt-6-astra")
    assert body["ok"] is False
    assert body["errorCode"] == "model_test_unproven"
    assert not any(call[0][-3:-1] == ["models", "set"] for call in runner.calls)


def test_test_use_rejects_stale_profile_and_model_before_activation():
    auth = facade(QueueRunner(result(PLAN)))
    with pytest.raises(InvalidRequestError):
        auth.test_and_use("openai:not-current", "openai/gpt-6-astra")

    runner = QueueRunner(result(PLAN), result(STATUS), result(CATALOG))
    with pytest.raises(InvalidRequestError):
        facade(runner).test_and_use("openai:work", "openai/not-entitled")
    assert not any("activate" in call[0] for call in runner.calls)


def test_no_api_key_mutation_or_auth_order_commands_exist_in_facade_source():
    import inspect

    source = inspect.getsource(OpenAIPlanAuthFacade)
    assert "paste-api-key" not in source
    assert "auth order" not in source
    assert "--force" not in source
    assert "--set-default" not in source


def test_bounded_runner_uses_argv_without_shell(monkeypatch):
    seen = {}

    class Proc:
        stdout = io.BytesIO(b"{}")
        stderr = io.BytesIO(b"")
        returncode = 0

        def wait(self, timeout=None):
            seen["wait_timeout"] = timeout
            return 0

        def poll(self):
            return self.returncode

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr("easel.openai_plan_auth.subprocess.Popen", fake_popen)
    got = BoundedProcessRunner(max_capture_bytes=16).run(["node.exe", "openclaw.mjs"], timeout=1)
    assert got.returncode == 0
    assert seen["argv"] == ["node.exe", "openclaw.mjs"]
    assert seen["kwargs"]["shell"] is False
