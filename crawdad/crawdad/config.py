"""CrawDad configuration model (FEAT-013 §Pre-decided choices).

Secrets — the Discord bot token and the Anthropic API key — come from
environment variables (``DISCORD_BOT_TOKEN``, ``ANTHROPIC_API_KEY``).
Everything else (vault path, MCP server command, allowlists) comes from
``crawdad.yaml``. The two sources are merged in :func:`load_config`.

FEAT-014 adds the agent-loop knobs. The Haiku router model identifier
lives here so no other module under ``crawdad/crawdad/`` references a
model ID literal — model IDs move (today: ``claude-haiku-4-5-20251001``;
the constant is the contract, not the literal). The
``CRAWDAD_ROUTER_MODEL`` env var overrides the fallback for
local-only experiments.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_DISCORD_TOKEN = "DISCORD_BOT_TOKEN"
_ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
_ENV_ROUTER_MODEL = "CRAWDAD_ROUTER_MODEL"
_DEFAULT_MCP_COMMAND: tuple[str, ...] = ("creek-tools-mcp",)

# The single place a Haiku model literal lives in this package. Other
# modules must read ``DEFAULT_ROUTER_MODEL`` (or accept a model argument
# resolved from it) — see the regression test that enforces this.
_DEFAULT_ROUTER_MODEL_FALLBACK = "claude-haiku-4-5-20251001"
DEFAULT_ROUTER_MODEL: str = os.environ.get(
    _ENV_ROUTER_MODEL, _DEFAULT_ROUTER_MODEL_FALLBACK
)

# History truncation knobs (ADOPT-008 hard cliff; FEAT-016 may refine).
HISTORY_MAX_ENTRIES: int = 20
HISTORY_MAX_CHARS_PER_ENTRY: int = 2000


class CrawDadConfig(BaseModel):
    """Immutable runtime configuration for the bot.

    Allowlists are tuples (frozen) to discourage in-place mutation
    across an async session.
    """

    model_config = ConfigDict(frozen=True)

    discord_bot_token: str = Field(min_length=1)
    anthropic_api_key: str = Field(min_length=1)
    vault_path: Path
    mcp_server_command: tuple[str, ...] = _DEFAULT_MCP_COMMAND
    allowed_user_ids: tuple[int, ...]
    allowed_channel_ids: tuple[int, ...]

    @field_validator("allowed_user_ids", "allowed_channel_ids")
    @classmethod
    def _non_empty(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Refuse an empty allowlist — FEAT-013 §Pre-decided choices."""
        if not value:
            msg = "allowlist must contain at least one entry"
            raise ValueError(msg)
        return value

    def is_allowed(self, *, user_id: int, channel_id: int) -> bool:
        """Return True only when both the user and channel are allowlisted."""
        return (
            user_id in self.allowed_user_ids and channel_id in self.allowed_channel_ids
        )


def load_config(yaml_path: Path | None = None) -> CrawDadConfig:
    """Merge ``crawdad.yaml`` with env secrets into a :class:`CrawDadConfig`.

    Args:
        yaml_path: Path to the YAML config. Defaults to ``./crawdad.yaml``.

    Raises:
        RuntimeError: when ``DISCORD_BOT_TOKEN`` or ``ANTHROPIC_API_KEY``
            is missing — secrets must never come from the YAML.
        FileNotFoundError: when the YAML config is missing.
    """
    discord_token = os.environ.get(_ENV_DISCORD_TOKEN)
    anthropic_key = os.environ.get(_ENV_ANTHROPIC_KEY)
    if not discord_token:
        msg = f"missing required env var {_ENV_DISCORD_TOKEN}"
        raise RuntimeError(msg)
    if not anthropic_key:
        msg = f"missing required env var {_ENV_ANTHROPIC_KEY}"
        raise RuntimeError(msg)

    path = yaml_path or Path("crawdad.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["discord_bot_token"] = discord_token
    raw["anthropic_api_key"] = anthropic_key
    return CrawDadConfig.model_validate(raw)
