"""Credential store kept separate from the structured agent config.

Operator IM credentials (bot tokens, app secrets) currently live inline in
``agent.json``. That makes the file unsafe to copy between machines or share
for support, and it spreads secrets across a document the UI rewrites often.

This module moves them to ``secrets.json`` next to the config file:

* the store is owner-readable only (``0600``) where the platform supports it,
* ``agent.json`` keeps every non-secret field, so it stays reviewable,
* :func:`merge_secrets` overlays stored values at load time, which means
  adapters keep reading a single merged section and need no changes.

Writes are atomic and merge-only: writing one channel never drops another
channel's credentials.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.config.paths import get_data_dir

logger = logging.getLogger(__name__)

SECRETS_FILENAME = "secrets.json"

# Keys treated as credentials. Kept intentionally explicit: moving a value out
# of agent.json is only safe when we know it is a secret, and a false positive
# would make the field invisible in the config file.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "bot_token",
        "app_token",
        "app_secret",
        "client_secret",
        "password",
        "api_key",
        "apikey",
        "access_token",
        "encrypt_key",
        "verification_token",
        "signing_secret",
    }
)

# Field names that identify a channel inside a section but are not secrets.
_NON_SECRET_ALIASES: frozenset[str] = frozenset({"token_type", "token_expires_at"})


def is_secret_key(key: str) -> bool:
    """Return whether *key* holds a credential."""
    lowered = key.lower()
    if lowered in _NON_SECRET_ALIASES:
        return False
    return lowered in SECRET_KEYS


def get_secrets_path(secrets_path: Path | None = None) -> Path:
    """Return the credential store path."""
    if secrets_path is not None:
        return Path(secrets_path)
    return get_data_dir() / SECRETS_FILENAME


def _read_store(path: Path) -> dict[str, dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("Could not read %s; treating it as empty", path, exc_info=True)
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed credential store at %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(channel): dict(values)
        for channel, values in data.items()
        if isinstance(values, Mapping)
    }


def _write_store(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Create the temp file owner-only before any secret is written into it.
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
    finally:
        if tmp.exists():
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows does not model POSIX permissions; the ACL inherited from the
        # user profile is the effective protection there.
        pass


def load_secrets(secrets_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return the whole credential store."""
    return _read_store(get_secrets_path(secrets_path))


def split_secrets(section: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split *section* into ``(public_fields, secret_fields)``."""
    public: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    for key, value in section.items():
        if is_secret_key(key) and isinstance(value, str):
            secrets[key] = value
        else:
            public[key] = value
    return public, secrets


def store_channel_secrets(
    name: str,
    values: Mapping[str, Any],
    *,
    secrets_path: Path | None = None,
) -> Path:
    """Persist *values* as *name*'s credentials.

    An empty string deletes the stored entry, which is how the UI clears a
    credential without leaving the old value behind.
    """
    path = get_secrets_path(secrets_path)
    store = _read_store(path)
    current = dict(store.get(name, {}))
    for key, value in values.items():
        if isinstance(value, str) and value.strip():
            current[key] = value
        else:
            current.pop(key, None)
    if current:
        store[name] = current
    else:
        store.pop(name, None)
    if store:
        _write_store(path, store)
    elif path.exists():
        path.unlink()
    return path


def clear_channel_secrets(name: str, *, secrets_path: Path | None = None) -> bool:
    """Remove every stored credential for *name*. Returns True if any existed."""
    path = get_secrets_path(secrets_path)
    store = _read_store(path)
    if name not in store:
        return False
    store.pop(name, None)
    if store:
        _write_store(path, store)
    elif path.exists():
        path.unlink()
    return True


def merge_secrets(
    config: Mapping[str, Any],
    *,
    secrets_path: Path | None = None,
) -> dict[str, Any]:
    """Overlay stored credentials onto a channels config mapping.

    Stored values win over whatever ``agent.json`` still contains, so a stale
    inline token can never shadow a freshly bound credential.
    """
    try:
        store = _read_store(get_secrets_path(secrets_path))
    except Exception:  # noqa: BLE001 - a broken store must not break startup
        logger.warning("Could not load the credential store", exc_info=True)
        return dict(config)
    if not store:
        return dict(config)

    merged: dict[str, Any] = dict(config)
    for name, values in store.items():
        section = merged.get(name)
        if not isinstance(section, Mapping):
            # No section means the channel was disabled/removed; skip so we do
            # not resurrect a channel the operator deliberately turned off.
            continue
        merged[name] = {**section, **values}
    return merged
