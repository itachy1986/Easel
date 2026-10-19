"""Windows double-click launcher safety contract."""
from __future__ import annotations

import json
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


def _run_failed_start_cleanup_harness(
    tmp_path: Path,
    *,
    reuse_launcher_pid: bool,
    listener_query_fails: bool = False,
) -> dict:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is unavailable")

    launcher = str(LAUNCHER_PS1).replace("'", "''")
    project_root = str(tmp_path).replace("'", "''")
    creation = "2026-10-19T08:00:00.0000000Z"
    current_creation = "2026-10-19T08:00:01.0000000Z" if reuse_launcher_pid else creation
    listener_query = (
        "function Get-ListenerProcessId { throw 'listener lookup unavailable' }"
        if listener_query_fails
        else "function Get-ListenerProcessId { return 222 }"
    )
    harness = tmp_path / "failed-start-cleanup.ps1"
    harness.write_text(
        f"""
. '{launcher}' -Action Start -ProjectRoot '{project_root}' -LibraryOnly
$script:Stopped = @()
$launcherCommand = \"$PythonPath -m uvicorn web.app:app --app-dir `\"$ProjectRoot`\" --host 127.0.0.1 --port 7860\"
$listenerCommand = \"python -m uvicorn web.app:app --app-dir `\"$ProjectRoot`\" --host 127.0.0.1 --port 7860\"
{listener_query}
function Get-ProcessIdentity([int]$TargetProcessId) {{
    if ($TargetProcessId -eq 111) {{
        return [pscustomobject]@{{
            ProcessId = 111
            ParentProcessId = 999
            CreationDate = '{current_creation}'
            ExecutablePath = $PythonPath
            CommandLine = $launcherCommand
        }}
    }}
    if ($TargetProcessId -eq 222) {{
        return [pscustomobject]@{{
            ProcessId = 222
            ParentProcessId = 111
            CreationDate = '2026-10-19T08:00:00.5000000Z'
            ExecutablePath = 'C:\\Python312\\python.exe'
            CommandLine = $listenerCommand
        }}
    }}
    return $null
}}
function Stop-Process {{
    [CmdletBinding()]
    param([int]$Id)
    $script:Stopped += $Id
}}
$expected = [pscustomobject]@{{
    ProcessId = 111
    ParentProcessId = 999
    CreationDate = '{creation}'
    ExecutablePath = $PythonPath
    CommandLine = $launcherCommand
}}
$result = Stop-FailedStartProcessTree $expected
[pscustomobject]@{{ Result = $result; Stopped = @($script:Stopped) }} | ConvertTo-Json -Compress -Depth 5
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip())


def test_failed_start_cleans_only_the_verified_spawned_process_tree(tmp_path: Path) -> None:
    outcome = _run_failed_start_cleanup_harness(tmp_path, reuse_launcher_pid=False)

    assert outcome["Result"]["LauncherStopped"] is True
    assert outcome["Result"]["ListenerStopped"] is True
    assert outcome["Stopped"] == [222, 111]


def test_failed_start_refuses_cleanup_after_launcher_pid_reuse(tmp_path: Path) -> None:
    outcome = _run_failed_start_cleanup_harness(tmp_path, reuse_launcher_pid=True)

    assert outcome["Result"]["LauncherStopped"] is False
    assert outcome["Result"]["ListenerStopped"] is False
    assert outcome["Stopped"] == []


def test_failed_start_still_cleans_verified_launcher_when_listener_lookup_fails(
    tmp_path: Path,
) -> None:
    outcome = _run_failed_start_cleanup_harness(
        tmp_path,
        reuse_launcher_pid=False,
        listener_query_fails=True,
    )

    assert outcome["Result"]["LauncherStopped"] is True
    assert outcome["Result"]["ListenerStopped"] is False
    assert outcome["Stopped"] == [111]


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
