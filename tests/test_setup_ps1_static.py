"""Static and simulated Windows checks for setup.ps1.

The executable probe mocks winget, so it never installs software or changes
machine/user PATH.  It verifies that setup refreshes registry PATH entries
without discarding values already available only to the current process.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETUP_PS1 = PROJECT_ROOT / "setup.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _ensure_command_functions() -> str:
    text = _read(SETUP_PS1)
    start = text.index("function Require-Command")
    return text[start : text.index("function Read-EnvFile", start)]


def test_dependency_install_preserves_process_path() -> None:
    body = _ensure_command_functions()
    assert re.search(r"\$env:Path\s*=\s*\$env:Path\s*\+", body)
    assert "GetEnvironmentVariable('Path', 'Machine')" in body
    assert "GetEnvironmentVariable('Path', 'User')" in body


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
def test_dependency_install_path_in_powershell(shell_name: str, tmp_path: Path) -> None:
    executable = shutil.which(shell_name)
    if not executable:
        pytest.skip(f"{shell_name} is unavailable")

    script = tmp_path / "path-probe.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        + _ensure_command_functions()
        + r"""
function Info($Message) {}
function Fail($Message) { throw $Message }
$script:Installed = $false
function Get-Command($Name, $ErrorAction) {
    if ($Name -eq 'winget' -or $script:Installed) {
        return [pscustomobject]@{ Name = $Name }
    }
}
function winget {
    $script:Installed = $true
    $global:LASTEXITCODE = 0
}
$originalPath = 'C:\easel probe\node;C:\easel probe\python'
$env:Path = $originalPath
Ensure-Command 'easel-path-probe' 'Easel.Test' 'mock dependency'
if (-not $script:Installed) { throw 'Mock dependency was not installed' }
$expectedPath = $originalPath + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
if ($env:Path -ne $expectedPath) { throw 'Process PATH was lost or registry PATH was not refreshed' }
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        timeout=60,
    )
    output = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    assert result.returncode == 0, output
