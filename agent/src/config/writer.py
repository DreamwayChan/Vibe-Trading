"""Safe write path for the operator's structured agent config.

The Web UI needs to change channel settings without asking the operator to
hand-edit ``~/.vibe-trading/agent.json``. Everything that writes that file goes
through :func:`patch_agent_config`, which:

1. reads the existing document (JSON or YAML) without normalising it,
2. deep-merges the patch so unrelated sections keep their exact shape,
3. validates the merged document against :class:`AgentConfig`,
4. backs the previous file up and replaces it atomically.

A failed validation never touches the file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from src.config.paths import get_config_path, get_data_dir
from src.config.schema import AgentConfig
from src.config.secrets import (
    SECRETS_FILENAME,
    clear_channel_secrets,
    is_secret_key,
    split_secrets,
    store_channel_secrets,
)

logger = logging.getLogger(__name__)

_MAX_BACKUPS = 5


class ConfigWriteError(RuntimeError):
    """Raised when the operator config could not be validated or written."""


def _read_raw(path: Path) -> dict[str, Any]:
    """Read *path* as a plain dict without applying schema defaults."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigWriteError(f"{path.name} does not contain a JSON/YAML object")
    return data


def _write_raw(path: Path, data: Mapping[str, Any]) -> None:
    """Write *data* to *path* atomically, readable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".yaml", ".yml"}:
        text = yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(dict(data), indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        # Legacy installs may still carry inline credentials; never widen the
        # mode of a file that can contain them.
        os.chmod(path, 0o600)
    except OSError:
        pass


def _backup(path: Path) -> None:
    """Keep the last few versions of the config next to the original."""
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    try:
        backup.write_bytes(path.read_bytes())
    except OSError:
        logger.warning("Could not back up %s before write", path, exc_info=True)
        return
    backups = sorted(path.parent.glob(f"{path.name}.bak.*"))
    for stale in backups[:-_MAX_BACKUPS]:
        try:
            stale.unlink()
        except OSError:
            pass


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Merge *patch* into *base*; ``None`` removes a key, lists replace wholesale."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def patch_agent_config(
    patch: Mapping[str, Any],
    *,
    config_path: Path | None = None,
) -> Path:
    """Merge *patch* into the operator config and persist it atomically.

    Args:
        patch: Partial document, e.g. ``{"channels": {"weixin": {...}}}``.
        config_path: Optional explicit path (tests use a temp file).

    Returns:
        The path that was written.

    Raises:
        ConfigWriteError: The merged document failed schema validation or the
            file could not be read/written.
    """
    target = get_config_path(config_path)
    try:
        raw = _read_raw(target)
    except (OSError, ValueError) as exc:
        raise ConfigWriteError(f"cannot read {target.name}: {exc}") from exc

    merged = _deep_merge(raw, patch)
    try:
        # Validate a copy: model_validate must not mutate the payload we write.
        AgentConfig.model_validate(json.loads(json.dumps(merged)))
    except Exception as exc:  # noqa: BLE001 - surfaced as a 422 to the operator
        raise ConfigWriteError(f"invalid configuration: {exc}") from exc

    try:
        _backup(target)
        _write_raw(target, merged)
    except OSError as exc:
        raise ConfigWriteError(f"cannot write {target.name}: {exc}") from exc
    return target


def set_channel_section(
    name: str,
    section: Mapping[str, Any] | None,
    *,
    config_path: Path | None = None,
    secrets_path: Path | None = None,
) -> Path:
    """Replace (or remove) one channel's config section.

    Credentials are split out into ``secrets.json``; ``agent.json`` keeps only
    the fields that are safe to read, diff, and share.
    """
    # Keep the credential store beside the config file it belongs to, so an
    # explicit config path (tests, multi-profile installs) stays isolated.
    target_secrets = secrets_path or (get_data_dir(config_path) / SECRETS_FILENAME)
    if section is None:
        clear_channel_secrets(name, secrets_path=target_secrets)
        return patch_agent_config({"channels": {name: None}}, config_path=config_path)

    public, secrets = split_secrets(section)
    patch_section: dict[str, Any] = dict(public)
    # Values written by older versions stay inline until the first save
    # migrates them. Read them first so stripping the key from agent.json can
    # never lose a credential, then delete them from the document.
    try:
        raw = _read_raw(get_config_path(config_path))
    except (OSError, ValueError):
        raw = {}
    existing = raw.get("channels") if isinstance(raw.get("channels"), Mapping) else {}
    existing_section = existing.get(name) if isinstance(existing, Mapping) else None
    inline_secrets: dict[str, Any] = {}
    if isinstance(existing_section, Mapping):
        for key, value in existing_section.items():
            if is_secret_key(str(key)):
                if isinstance(value, str) and value:
                    inline_secrets[str(key)] = value
                patch_section.setdefault(str(key), None)

    store_channel_secrets(name, {**inline_secrets, **secrets}, secrets_path=target_secrets)
    return patch_agent_config({"channels": {name: patch_section}}, config_path=config_path)


def set_channels_global(
    patch: Mapping[str, Any],
    *,
    config_path: Path | None = None,
) -> Path:
    """Patch top-level ``channels`` settings such as ``auto_start``."""
    return patch_agent_config({"channels": dict(patch)}, config_path=config_path)


def mask_secrets(section: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of a channel section with credentials masked.

    Secret-looking keys keep a ``has_<key>`` marker instead of the value so the
    UI can show "configured" without ever shipping the token to the browser.
    """
    if not section:
        return {}
    masked: dict[str, Any] = {}
    for key, value in section.items():
        if is_secret_key(str(key)) and isinstance(value, str):
            masked[key] = ""
            masked[f"has_{key}"] = bool(value)
        else:
            masked[key] = value
    return masked
