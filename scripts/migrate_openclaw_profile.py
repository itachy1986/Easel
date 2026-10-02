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
    changed = repair_model_names(config)
    if changed == 0:
        return 0

    replacement = _serialized_config(config, original)
    mode = stat.S_IMODE(path.stat().st_mode)
    _create_backup(path)
    _atomic_replace(path, replacement, mode)
    return changed


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: migrate_openclaw_profile.py OPENCLAW_JSON", file=sys.stderr)
        return 2
    try:
        changed = migrate(Path(arguments[0]))
    except json.JSONDecodeError:
        print("OpenClaw model schema migration failed: invalid JSON", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"OpenClaw model schema migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"OpenClaw model schema migration: {changed} model name(s) repaired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
