from __future__ import annotations

import json
import sys
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

    def test_and_use(self, profile_id, model):
        self.calls.append(("test", profile_id, model))
        return {"ok": True, "selectedModel": model, "effectiveProvider": "openai", "testResult": "success", "errorCode": ""}

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
            json={"profileId": "openai:work", "model": "openai/gpt-6-astra"},
        )
        assert response.status_code == 200
        assert response.json()["selectedModel"] == "openai/gpt-6-astra"
    assert ("shutdown",) in fake.calls


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
