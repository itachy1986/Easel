from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from easel.openclaw_cmd import openclaw_base_cmd


def test_real_openclaw_2026_9_7_parser_route_and_projection_contract():
    """No-network executable contract against the installed 2026.9.7 runtime."""
    try:
        base = openclaw_base_cmd()
    except FileNotFoundError:
        pytest.skip("OpenClaw is not installed")
    if len(base) != 2 or Path(base[1]).name != "openclaw.mjs":
        pytest.skip("OpenClaw package entry point is not directly resolvable")

    harness = Path(__file__).with_name("openclaw_2026_9_7_contract_harness.mjs")
    completed = subprocess.run(
        [base[0], str(harness), base[1]],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if completed.returncode == 77:
        payload = json.loads(completed.stdout)
        pytest.skip(f"requires OpenClaw 2026.9.7, found {payload.get('version')}")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "version": "2026.9.7",
        "exactAllowlistRejectsPinnedHeader": True,
        "unrestrictedAcceptsPinnedHeader": True,
        "explicitModelDisablesConfiguredFallbacks": True,
        "agentExecEnvelopeHasCredentialIdentity": False,
    }
