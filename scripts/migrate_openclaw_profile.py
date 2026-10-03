#!/usr/bin/env python3
"""Repair the minimal legacy OpenClaw provider/model schema safely."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


OFFICIAL_OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_PROVIDER = "openai"
OPENAI_COMPATIBLE_PROVIDER = "openai-compatible"


class ProviderNamespaceCollision(ValueError):
    """Raised when migration cannot converge without overwriting a provider."""


def _normalized_base_url(base_url: object) -> str:
    if not isinstance(base_url, str):
        return ""
    return base_url.strip().rstrip("/")


def _is_valid_custom_openai_base(normalized: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in normalized):
        return False
    if "@" in normalized:
        return False
    try:
        parsed = urlsplit(normalized)
        host = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(host)


def classify_openai_provider(base_url: object) -> str:
    """Return the canonical provider key for a newly written OpenAI-style URL."""
    normalized = _normalized_base_url(base_url)
    if not normalized or normalized == OFFICIAL_OPENAI_BASE_URL:
        return OPENAI_PROVIDER
    if _is_valid_custom_openai_base(normalized):
        return OPENAI_COMPATIBLE_PROVIDER
    raise ValueError("invalid OpenAI Base URL; expected an absolute HTTP(S) URL with a host")


def _is_clear_custom_openai_base(base_url: object) -> bool:
    """Only migrate an existing provider when its URL is clearly custom HTTP(S)."""
    normalized = _normalized_base_url(base_url)
    if not normalized or normalized == OFFICIAL_OPENAI_BASE_URL:
        return False
    return _is_valid_custom_openai_base(normalized)


def _migrate_primary_reference(config: dict) -> bool:
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return False
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return False
    model = defaults.get("model")
    if not isinstance(model, dict):
        return False
    primary = model.get("primary")
    if not isinstance(primary, str) or not primary.startswith(f"{OPENAI_PROVIDER}/"):
        return False
    model["primary"] = f"{OPENAI_COMPATIBLE_PROVIDER}/{primary.split('/', 1)[1]}"
    return True


def _providers_semantically_equal(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left == right:
        return True
    left_normalized = dict(left)
    right_normalized = dict(right)
    left_normalized["baseUrl"] = _normalized_base_url(left.get("baseUrl"))
    right_normalized["baseUrl"] = _normalized_base_url(right.get("baseUrl"))
    return left_normalized == right_normalized


def migrate_openai_provider_namespace(config: object) -> bool:
    """Move a clearly custom legacy ``openai`` block without merging data."""
    if not isinstance(config, dict):
        return False
    models = config.get("models")
    if not isinstance(models, dict):
        return False
    providers = models.get("providers")
    if not isinstance(providers, dict):
        return False
    source = providers.get(OPENAI_PROVIDER)
    if not isinstance(source, dict) or not _is_clear_custom_openai_base(source.get("baseUrl")):
        return False

    if OPENAI_COMPATIBLE_PROVIDER in providers:
        target = providers[OPENAI_COMPATIBLE_PROVIDER]
        if not _providers_semantically_equal(target, source):
            raise ProviderNamespaceCollision(
                "openai-compatible provider collision; existing providers were not changed"
            )
    else:
        providers[OPENAI_COMPATIBLE_PROVIDER] = source
    del providers[OPENAI_PROVIDER]
    _migrate_primary_reference(config)
    return True


def repair_model_names(config: object) -> int:
    """Set missing/invalid model names to a valid id and nothing else."""
    if not isinstance(config, dict):
        return 0
    models_root = config.get("models")
    if not isinstance(models_root, dict):
        return 0
    providers = models_root.get("providers")
    if not isinstance(providers, dict):
        return 0

    changed = 0
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            name = model.get("name")
            if not isinstance(name, str) or not name.strip():
                model["name"] = model_id
                changed += 1
    return changed


def _serialized_config(config: object, original: bytes) -> bytes:
    had_bom = original.startswith(b"\xef\xbb\xbf")
    original_without_bom = original[3:] if had_bom else original
    newline = "\r\n" if b"\r\n" in original_without_bom else "\n"
    had_trailing_newline = original_without_bom.endswith((b"\n", b"\r"))
    text = json.dumps(config, ensure_ascii=False, indent=2)
    if newline != "\n":
        text = text.replace("\n", newline)
    if had_trailing_newline:
        text += newline
    encoded = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if had_bom else encoded


def _create_backup(path: Path) -> Path:
    descriptor, backup_name = tempfile.mkstemp(
        prefix=f"{path.name}.easel-backup-",
        suffix=".json",
        dir=path.parent,
    )
    os.close(descriptor)
    backup = Path(backup_name)
    try:
        shutil.copy2(path, backup)
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    return backup


def _atomic_replace(path: Path, content: bytes, mode: int) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def migrate(path: Path) -> int:
    if not path.is_file():
        return 0

    original = path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    namespace_changed = migrate_openai_provider_namespace(config)
    changed = repair_model_names(config)
    if changed == 0 and not namespace_changed:
        return 0

    replacement = _serialized_config(config, original)
    mode = stat.S_IMODE(path.stat().st_mode)
    _create_backup(path)
    _atomic_replace(path, replacement, mode)
    return changed


def _classify_openai_provider_from_stdin() -> int:
    try:
        provider = classify_openai_provider(sys.stdin.read())
    except ValueError:
        print(
            "OpenAI Base URL classification failed: expected an absolute HTTP(S) URL with a host",
            file=sys.stderr,
        )
        return 1
    print(provider)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--classify-openai-provider-stdin"]:
        return _classify_openai_provider_from_stdin()
    if len(arguments) != 1:
        print("usage: migrate_openclaw_profile.py OPENCLAW_JSON", file=sys.stderr)
        return 2
    try:
        changed = migrate(Path(arguments[0]))
    except json.JSONDecodeError:
        print("OpenClaw model schema migration failed: invalid JSON", file=sys.stderr)
        return 1
    except ProviderNamespaceCollision as exc:
        print(f"OpenClaw provider namespace migration failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"OpenClaw model schema migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"OpenClaw model schema migration: {changed} model name(s) repaired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
