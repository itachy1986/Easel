"""OpenClaw legacy provider/model schema migration regression tests."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATOR = PROJECT_ROOT / "scripts" / "migrate_openclaw_profile.py"
SETUP_PS1 = PROJECT_ROOT / "setup.ps1"
SETUP_SH = PROJECT_ROOT / "setup.sh"


def _run_migration(config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MIGRATOR), str(config_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _backups(config_path: Path) -> list[Path]:
    return sorted(config_path.parent.glob(f"{config_path.name}.easel-backup-*.json"))


def _load_migrator():
    spec = importlib.util.spec_from_file_location("easel_profile_migrator", MIGRATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_profile_is_noop(tmp_path):
    config_path = tmp_path / "missing" / "openclaw.json"

    proc = _run_migration(config_path)

    assert proc.returncode == 0, proc.stderr
    assert "0 model name" in proc.stdout
    assert not config_path.exists()
    assert not config_path.parent.exists()


def test_provider_without_models_is_byte_identical_and_has_no_backup(tmp_path):
    config_path = tmp_path / "openclaw.json"
    original = b'{\n  "models": {"providers": {"legacy": {"apiKey": "secret"}}}\n}\n'
    config_path.write_bytes(original)

    proc = _run_migration(config_path)

    assert proc.returncode == 0, proc.stderr
    assert "0 model name" in proc.stdout
    assert config_path.read_bytes() == original
    assert _backups(config_path) == []


def test_repairs_only_authorized_names_and_preserves_credentials(tmp_path):
    config_path = tmp_path / "openclaw.json"
    original = {
        "models": {
            "providers": {
                "legacy": {
                    "apiKey": "secret-api-key-sentinel",
                    "oauth": {"token": "secret-oauth-sentinel", "refresh": "keep"},
                    "secretRef": {"source": "env", "name": "SECRET_SENTINEL"},
                    "headers": {"Authorization": "secret-header-sentinel", "X-Custom": "keep"},
                    "localService": {"command": "adapter", "env": {"TOKEN": "secret-local-sentinel"}},
                    "unknownProviderField": {"nested": [1, True, None]},
                    "models": [
                        {"id": "missing", "reasoning": True, "unknownModelField": {"x": 1}},
                        {"id": "blank", "name": "  \t"},
                        {"id": "invalid-name", "name": 42, "input": ["text"]},
                        {"id": "valid", "name": "Keep this name", "contextWindow": 8192},
                        {"name": "missing id", "token": "model-secret-sentinel"},
                        {"id": "   ", "name": None, "maxTokens": 123},
                        "opaque-item",
                    ],
                }
            }
        },
        "agents": {"defaults": {"model": {"primary": "legacy/missing"}}},
        "gateway": {"bind": "loopback", "auth": {"mode": "token"}, "port": 37289},
        "cookie": "secret-cookie-sentinel",
    }
    expected = copy.deepcopy(original)
    expected_models = expected["models"]["providers"]["legacy"]["models"]
    expected_models[0]["name"] = "missing"
    expected_models[1]["name"] = "blank"
    expected_models[2]["name"] = "invalid-name"
    original_bytes = (json.dumps(original, ensure_ascii=False, indent=4) + "\n").encode("utf-8")
    config_path.write_bytes(original_bytes)
    if os.name != "nt":
        config_path.chmod(0o600)

    proc = _run_migration(config_path)

    assert proc.returncode == 0, proc.stderr
    assert "3 model name" in proc.stdout
    assert "secret-" not in proc.stdout + proc.stderr
    assert json.loads(config_path.read_text(encoding="utf-8")) == expected
    backups = _backups(config_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes
    assert not list(tmp_path.glob(f".{config_path.name}.*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_valid_models_and_invalid_ids_are_untouched(tmp_path):
    config_path = tmp_path / "openclaw.json"
    original = b'{"models":{"providers":{"p":{"models":[{"id":"ok","name":"Valid"},{"name":"no id"},{"id":null,"name":7},{"id":"   ","name":false}]}}}}\n'
    config_path.write_bytes(original)

    proc = _run_migration(config_path)

    assert proc.returncode == 0, proc.stderr
    assert "0 model name" in proc.stdout
    assert config_path.read_bytes() == original
    assert _backups(config_path) == []


def test_migration_is_idempotent_and_does_not_create_second_backup(tmp_path):
    config_path = tmp_path / "openclaw.json"
    config_path.write_text('{"models":{"providers":{"p":{"models":[{"id":"x"}]}}}}\n', encoding="utf-8")

    first = _run_migration(config_path)
    first_bytes = config_path.read_bytes()
    second = _run_migration(config_path)

    assert first.returncode == 0 and "1 model name" in first.stdout
    assert second.returncode == 0 and "0 model name" in second.stdout
    assert config_path.read_bytes() == first_bytes
    assert len(_backups(config_path)) == 1


def test_invalid_json_fails_without_partial_write_or_backup(tmp_path):
    config_path = tmp_path / "openclaw.json"
    original = b'{"models": invalid json\n'
    config_path.write_bytes(original)

    proc = _run_migration(config_path)

    assert proc.returncode != 0
    assert config_path.read_bytes() == original
    assert _backups(config_path) == []
    assert not list(tmp_path.glob(f".{config_path.name}.*.tmp"))


def test_atomic_replace_failure_keeps_original_and_removes_temp(tmp_path, monkeypatch):
    config_path = tmp_path / "openclaw.json"
    original = b'{"models":{"providers":{"p":{"models":[{"id":"x"}]}}}}\n'
    config_path.write_bytes(original)
    migrator = _load_migrator()

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(migrator.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        migrator.migrate(config_path)

    assert config_path.read_bytes() == original
    backups = _backups(config_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not list(tmp_path.glob(f".{config_path.name}.*.tmp"))


def test_migrator_uses_same_directory_temp_backup_and_atomic_replace():
    source = MIGRATOR.read_text(encoding="utf-8")

    assert "dir=path.parent" in source
    assert "shutil.copy2" in source
    assert "os.replace" in source


def test_windows_migration_runs_before_onboard():
    source = SETUP_PS1.read_text(encoding="utf-8")
    migration = source.index("migrate_openclaw_profile.py")
    onboard = source.index("& openclaw @onboardArgs")

    assert migration < onboard


def test_posix_migration_runs_before_onboard_and_first_config_mutation():
    source = SETUP_SH.read_text(encoding="utf-8")
    migration = source.index("migrate_openclaw_profile.py")
    onboard = source.index('$OPENCLAW_BIN --profile "$PROFILE" "${ONBOARD_ARGS[@]}"')
    config_set = source.index("$OC config set")

    assert migration < onboard
    assert migration < config_set


def test_choice_zero_remains_credential_blind_and_writes_no_provider_or_primary():
    ps1 = SETUP_PS1.read_text(encoding="utf-8")
    ps1_choice = ps1[ps1.index("$choice = Read-Host"):ps1.index("$envValues = Read-EnvFile $envPath", ps1.index("$choice = Read-Host"))]
    assert "if ($choice -eq '2')" in ps1_choice
    assert "elseif ($choice -eq '1'" in ps1_choice
    assert "choice -eq '0'" not in ps1_choice
    assert "models.providers" not in ps1_choice
    assert "agents.defaults.model.primary" not in ps1_choice

    sh = SETUP_SH.read_text(encoding="utf-8")
    sh_choice = sh[sh.index('case "${PROVIDER_CHOICE:-1}" in'):sh.index("DEFAULT_PRIMARY_MODEL=")]
    assert "0) ;;" in sh_choice
    assert "models.providers" not in sh_choice
    assert "agents.defaults.model.primary" not in sh_choice


@pytest.mark.parametrize(("base_url", "expected"), [
    (None, "openai"),
    ("", "openai"),
    ("   ", "openai"),
    ("https://api.openai.com/v1", "openai"),
    ("https://api.openai.com/v1/", "openai"),
    ("https://api.openai.com/v1//", "openai"),
    ("https://api.openai.com/v1/chat/completions", "openai-compatible"),
    ("https://api.deepseek.com/v1", "openai-compatible"),
    ("https://proxy.example.com/openai/v1", "openai-compatible"),
])
def test_openai_provider_classification_is_exact(base_url, expected):
    migrator = _load_migrator()

    assert migrator.classify_openai_provider(base_url) == expected


def test_custom_openai_provider_is_moved_deeply_and_primary_is_migrated(tmp_path):
    config_path = tmp_path / "openclaw.json"
    custom_provider = {
        "baseUrl": "https://proxy.example.com/v1",
        "api": "openai-completions",
        "apiKey": "secret-api-key-sentinel",
        "models": [{"id": "custom-model", "name": "Custom", "input": ["text"]}],
        "headers": {"X-Tenant": "keep", "Authorization": "secret-header-sentinel"},
        "request": {"allowPrivateNetwork": False, "nested": {"keep": True}},
        "localService": {"command": "adapter", "env": {"TOKEN": "secret-local-sentinel"}},
        "timeoutSeconds": 321,
        "unknown": {"nested": [1, True, None]},
    }
    original = {
        "models": {"providers": {
            "openai": custom_provider,
            "unrelated": {"baseUrl": "https://unrelated.example/v1", "models": [{"id": "u", "name": "U"}]},
        }},
        "agents": {"defaults": {"model": {"primary": "openai/custom-model"}}},
        "media": {
            "image": {"provider": "openai", "model": "image-model"},
            "audio": {"provider": "openai", "model": "audio-model"},
        },
        "memory": {"search": {"provider": "openai-compatible", "model": "embedding-model"}},
        "auth": {"opaque": "secret-auth-store-sentinel"},
    }
    config_path.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    proc = _run_migration(config_path)

    assert proc.returncode == 0, proc.stderr
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert "openai" not in migrated["models"]["providers"]
    assert migrated["models"]["providers"]["openai-compatible"] == custom_provider
    assert migrated["agents"]["defaults"]["model"]["primary"] == "openai-compatible/custom-model"
    assert migrated["models"]["providers"]["unrelated"] == original["models"]["providers"]["unrelated"]
    assert migrated["media"] == original["media"]
    assert migrated["memory"] == original["memory"]
    assert migrated["auth"] == original["auth"]
    assert "secret-" not in proc.stdout + proc.stderr
    assert len(_backups(config_path)) == 1


def test_official_openai_provider_is_byte_identical_and_has_no_backup(tmp_path):
    config_path = tmp_path / "openclaw.json"
    original = b'{\n  "models": {"providers": {"openai": {"baseUrl": "https://api.openai.com/v1/", "unknown": true}}}\n}\n'
    config_path.write_bytes(original)

    proc = _run_migration(config_path)

    assert proc.returncode == 0, proc.stderr
    assert config_path.read_bytes() == original
    assert _backups(config_path) == []


def test_equivalent_target_collision_converges_and_is_idempotent(tmp_path):
    config_path = tmp_path / "openclaw.json"
    provider = {"baseUrl": "https://proxy.example/v1", "apiKey": "secret-sentinel", "models": []}
    equivalent_target = copy.deepcopy(provider)
    equivalent_target["baseUrl"] += "/"
    config_path.write_text(json.dumps({
        "models": {"providers": {"openai": provider, "openai-compatible": equivalent_target}},
        "agents": {"defaults": {"model": {"primary": "openai/model-x"}}},
    }), encoding="utf-8")

    first = _run_migration(config_path)
    first_bytes = config_path.read_bytes()
    second = _run_migration(config_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    migrated = json.loads(first_bytes.decode("utf-8"))
    assert "openai" not in migrated["models"]["providers"]
    assert migrated["models"]["providers"]["openai-compatible"] == equivalent_target
    assert migrated["agents"]["defaults"]["model"]["primary"] == "openai-compatible/model-x"
    assert len(_backups(config_path)) == 1


def test_non_equivalent_target_collision_fails_without_write_or_backup(tmp_path):
    config_path = tmp_path / "openclaw.json"
    original = json.dumps({
        "models": {"providers": {
            "openai": {"baseUrl": "https://proxy-a.example/v1", "apiKey": "secret-a"},
            "openai-compatible": {"baseUrl": "https://proxy-b.example/v1", "apiKey": "secret-b"},
        }},
        "agents": {"defaults": {"model": {"primary": "openai/model-x"}}},
    }).encode("utf-8")
    config_path.write_bytes(original)

    proc = _run_migration(config_path)

    assert proc.returncode != 0
    assert "collision" in proc.stderr.lower()
    assert "secret-" not in proc.stdout + proc.stderr
    assert config_path.read_bytes() == original
    assert _backups(config_path) == []


def test_migrator_does_not_access_oauth_or_auth_stores():
    source = MIGRATOR.read_text(encoding="utf-8").lower()

    assert "sqlite" not in source
    assert "oauth" not in source
    assert "token.json" not in source
