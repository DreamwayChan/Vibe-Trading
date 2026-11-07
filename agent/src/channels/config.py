"""Channel config loading helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.config.loader import load_agent_config
from src.config.paths import get_data_dir
from src.config.secrets import SECRETS_FILENAME, merge_secrets

logger = logging.getLogger(__name__)


def load_channels_config(
    config_path: Path | None = None,
    *,
    include_secrets: bool = True,
) -> dict[str, Any]:
    """Load the operator IM channel config from the structured agent config.

    Credentials that live in the separate credential store (``secrets.json``)
    are overlaid so every caller sees one complete section. Pass
    ``include_secrets=False`` to inspect the on-disk config as written.

    Args:
        config_path: Optional explicit config path.
        include_secrets: Whether to overlay credentials from ``secrets.json``.

    Returns:
        A plain dictionary suitable for :class:`src.channels.manager.ChannelManager`.
    """
    config = load_agent_config(config_path)
    data = config.channels.model_dump(mode="json", by_alias=False)
    if not include_secrets:
        return data
    try:
        secrets_path = get_data_dir(config_path) / SECRETS_FILENAME
    except OSError:
        # An unwritable data dir must not stop the config from loading.
        logger.warning("Cannot locate the credential store; using config values only")
        return data
    return merge_secrets(data, secrets_path=secrets_path)
