from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from easel.openclaw_cmd import openclaw_base_cmd


def test_real_openclaw_config_cas_changes_only_main_text_model(tmp_path):
    """No-network regression for the exact 2026.9.7 mutation used by Test & use."""
    try:
        base = openclaw_base_cmd()
    except FileNotFoundError:
        pytest.skip("OpenClaw is not installed")
    version = subprocess.run(
        [*base, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        shell=False,
    )
    if version.returncode != 0 or "2026.9.7" not in version.stdout:
        pytest.skip("requires OpenClaw 2026.9.7")

    config_path = tmp_path / "openclaw.json"
    before = {
        "meta": {
            "lastTouchedVersion": "2026.9.7",
            "migrations": {
                "modelPolicyAllowlist": True,
                "utilityModelSeparation": True,
            },
        },
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "https://api.openai.com/v1",
                    "apiKey": "provider-key-sentinel",
                    "api": "openai-responses",
                    "models": [
                        {"id": "gpt-6-astra", "name": "GPT-6 Astra"},
                        {"id": "gpt-old", "name": "GPT Old"},
                    ],
                },
            },
        },
        "agents": {
            "ownership": "explicit",
            "defaults": {"model": {"primary": "openai/gpt-old"}},
            "entries": {
                "main": {
                    "model": {
                        "primary": "openai/gpt-old",
                        "fallbacks": ["openai/gpt-fallback"],
                    },
                },
                "media": {"model": "openai/gpt-old"},
            },
        },
        "tools": {"media": {"audio": {"enabled": False}}},
    }
    config_path.write_text(json.dumps(before), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "OPENCLAW_STATE_DIR": str(tmp_path),
            "OPENCLAW_CONFIG_PATH": str(config_path),
            "OPENCLAW_SKIP_CHANNELS": "1",
        }
    )
    # The executable check isolates the config engine's CAS/write scope. The
    # companion 2026.9.7 source harness and facade tests cover the auth-profile
    # suffix because a real pinned value correctly requires a stored profile.
    target = "openai/gpt-6-astra"
    command = [
        *base,
        "--profile", "easel",
        "config", "set", "agents.entries.main.model.primary",
        json.dumps(target),
        "--strict-json",
        "--expect-current-json", json.dumps("openai/gpt-old"),
    ]
    updated = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
        env=env,
    )
    assert updated.returncode == 0, updated.stderr

    after = json.loads(config_path.read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(before))
    expected["agents"]["entries"]["main"]["model"]["primary"] = target
    assert after == expected
    assert after["models"]["providers"]["openai"]["apiKey"] == "provider-key-sentinel"
    assert after["tools"]["media"] == before["tools"]["media"]
    assert after["agents"]["entries"]["media"] == before["agents"]["entries"]["media"]

    stale = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
        env=env,
    )
    assert stale.returncode != 0
    assert json.loads(config_path.read_text(encoding="utf-8")) == expected
