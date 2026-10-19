"""Windows double-click launcher safety contract."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
START_CMD = PROJECT_ROOT / "start.cmd"
STOP_CMD = PROJECT_ROOT / "stop.cmd"
LAUNCHER_PS1 = PROJECT_ROOT / "scripts" / "windows_launcher.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_batch_entrypoints_are_project_scoped_and_loopback_only() -> None:
    start = _read(START_CMD)
    stop = _read(STOP_CMD)

    for text in (start, stop):
        assert "%~dp0" in text
        assert 'set "EASEL_ROOT=%~dp0"' in text
        assert 'set "EASEL_PORT=7860"' in text
        assert 'set "EASEL_PROJECT_ROOT=%%~fI"' in text
        assert '-ProjectRoot "%EASEL_PROJECT_ROOT%"' in text
        assert "windows_launcher.ps1" in text
        assert "7880" not in text
        assert "0.0.0.0" not in text
        assert "taskkill" not in text.lower()


def test_launcher_records_and_verifies_exact_process_identity_before_stop() -> None:
    text = _read(LAUNCHER_PS1)

    assert ".venv\\Scripts\\python.exe" in text
    assert "web.app:app" in text
    assert "127.0.0.1" in text
    assert "Get-NetTCPConnection" in text
    assert "Get-CimInstance" in text
    assert "ParentProcessId" in text
    assert "CreationDate" in text
    assert "ExecutablePath" in text
    assert "CommandLine" in text
    assert "Stop-Process -Id $record.ListenerPid" in text
    assert "Stop-Process -Name" not in text
    assert "taskkill" not in text.lower()
    assert "/api/settings/harness" in text
    assert "outputs" in text and "_launcher" in text


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
def test_launcher_parses_in_available_powershell(shell_name: str) -> None:
    executable = shutil.which(shell_name)
    if not executable:
        pytest.skip(f"{shell_name} is unavailable")
    command = (
        "$ErrorActionPreference='Stop'; "
        f"[void][ScriptBlock]::Create((Get-Content -LiteralPath '{LAUNCHER_PS1}' -Raw))"
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
