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

# Discord's free-tier upload limit is 25 MiB (Boost levels raise it).
# Bot-side default mirrors that so users see a clear refusal rather
# than a confusing partial download. Override in ``crawdad.yaml`` if
# the channel is on a boosted server.
_DEFAULT_MAX_ATTACHMENT_BYTES: int = 25 * 1024 * 1024

# FEAT-035: the default allow list is narrowed to extensions whose content
# type can be verified — either by magic-byte signature (PDF, OOXML, common
# images) or by text sampling (UTF-8 + no NUL bytes in the first KiB). The
# pre-FEAT-035 list also accepted legacy Office formats (.doc/.xls/.ppt),
# which the ``filetype`` library cannot reliably detect from magic bytes
# and which carry an additional macro-execution risk. Operators who need
# them can re-add the extensions in ``crawdad.yaml``.
_DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".txt",
    ".html",
    ".htm",
    ".pdf",
    ".docx",
    ".json",
    ".csv",
    ".xlsx",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)

# Hard deny for file types the system cannot safely or usefully ingest.
# Override in ``crawdad.yaml`` to widen or tighten per deployment.
_DEFAULT_DENIED_EXTENSIONS: tuple[str, ...] = (
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".bat",
    ".sh",
    ".com",
    ".cmd",
    ".msi",
    ".app",
    ".jar",
)

_DEFAULT_STAGING_SUBPATH = Path("00-Creek-Meta") / "Inbound"

_ENV_DISCORD_TOKEN = "DISCORD_BOT_TOKEN"
_ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
_ENV_ROUTER_MODEL = "CRAWDAD_ROUTER_MODEL"
_ENV_COMPOSER_MODEL = "CRAWDAD_COMPOSER_MODEL"
_DEFAULT_MCP_COMMAND: tuple[str, ...] = ("creek-tools-mcp",)

# The single place a Haiku model literal lives in this package. Other
# modules must read ``DEFAULT_ROUTER_MODEL`` (or accept a model argument
# resolved from it) — see the regression test that enforces this.
_DEFAULT_ROUTER_MODEL_FALLBACK = "claude-haiku-4-5-20251001"
DEFAULT_ROUTER_MODEL: str = os.environ.get(
    _ENV_ROUTER_MODEL, _DEFAULT_ROUTER_MODEL_FALLBACK
)

# Same indirection for the FEAT-015 Sonnet composer. The literal Sonnet
# model ID lives ONLY here; the regression test in
# ``test_no_model_literals.py`` enforces that no other module references
# any ``claude-sonnet-*`` string.
_DEFAULT_COMPOSER_MODEL_FALLBACK = "claude-sonnet-4-6"
DEFAULT_COMPOSER_MODEL: str = os.environ.get(
    _ENV_COMPOSER_MODEL, _DEFAULT_COMPOSER_MODEL_FALLBACK
)

# History truncation knobs (ADOPT-008 hard cliff; FEAT-016 may refine).
HISTORY_MAX_ENTRIES: int = 20
HISTORY_MAX_CHARS_PER_ENTRY: int = 2000

# Agent-loop knobs (FEAT-015 §pre-decided choices).
# Hard cap — the 6th attempt is refused with a documented user reply
# and a session reset. Tune later.
MAX_LOOP_ROUNDS: int = 5

# Voice-skill tree directory inside the user's vault. The loader reads
# ``<vault>/creek-skills/voice-core/SKILL.md`` plus phase- and
# register-specific files; missing files yield empty skill lists so a
# vault without a fleshed-out voice tree still gets a response (just a
# less voice-faithful one).
CREEK_SKILLS_DIRNAME: str = "creek-skills"
DEFAULT_REGISTER: str = "confessional"


class AttachmentConfig(BaseModel):
    """Per-attachment limits and staging-path config (FEAT-027).

    Defaults match Discord's free-tier 25 MiB cap and the canonical
    ``00-Creek-Meta/Inbound/`` staging subpath. Overrides live in
    ``crawdad.yaml`` under the ``attachments`` key.

    Attributes:
        max_size_bytes: Per-attachment ceiling. Files larger than this
            are refused before the download body is consumed (Discord
            reports the size in the message metadata).
        allowed_extensions: Inclusive allow list (lowercase, leading
            dot). When empty, every extension passes the allow list —
            but the deny list still applies.
        denied_extensions: Hard deny list — present here always wins
            over the allow list. Defaults to common executable types
            the ingest pipeline cannot handle.
        staging_subpath: Vault-relative directory under which per-
            channel / per-message subdirectories are created. Default
            ``00-Creek-Meta/Inbound``.
        channel_privacy_tiers: Optional per-channel privacy tier
            override (``open`` / ``personal`` / ``intimate`` / ``all``).
            When a channel id is absent, attachments inherit the bot's
            default ``personal`` tier — ingest writes are personal by
            default per FEAT-011, so a missing entry never silently
            downgrades. Tier strings are validated at config-parse time;
            unknown values raise ``ValueError``.
        reject_on_mime_mismatch: FEAT-035 knob. When ``True`` the bot
            refuses an attachment whose downloaded bytes do not match
            the MIME type implied by its extension; when ``False``
            (default) the mismatch is surfaced as a soft warning in the
            Discord safety report but the file still lands in staging.
            Soft-warning mode preserves the v1 user-consent gate; flip
            to ``True`` when a stricter deployment wants the bot to hard
            reject polyglots (zip-disguised-as-pdf, executable-renamed-
            to-md, etc.) before the user is even asked to ingest.
    """

    model_config = ConfigDict(frozen=True)

    max_size_bytes: int = Field(
        default=_DEFAULT_MAX_ATTACHMENT_BYTES,
        gt=0,
    )
    allowed_extensions: frozenset[str] = Field(
        default_factory=lambda: frozenset(_DEFAULT_ALLOWED_EXTENSIONS),
    )
    denied_extensions: frozenset[str] = Field(
        default_factory=lambda: frozenset(_DEFAULT_DENIED_EXTENSIONS),
    )
    staging_subpath: Path = Field(default=_DEFAULT_STAGING_SUBPATH)
    channel_privacy_tiers: dict[int, str] = Field(default_factory=dict)
    reject_on_mime_mismatch: bool = Field(default=False)

    @field_validator("allowed_extensions", "denied_extensions")
    @classmethod
    def _normalise_extensions(cls, value: frozenset[str]) -> frozenset[str]:
        """Lowercase every extension and ensure each starts with a single dot."""
        normalised: set[str] = set()
        for raw in value:
            if not raw:
                continue
            cleaned = raw.lower().strip()
            if not cleaned.startswith("."):
                cleaned = "." + cleaned
            normalised.add(cleaned)
        return frozenset(normalised)

    @field_validator("staging_subpath")
    @classmethod
    def _refuse_absolute_subpath(cls, value: Path) -> Path:
        """Refuse absolute and ``..``-traversing paths — must stay in the vault.

        ``Path.is_absolute()`` catches obvious ``/etc/foo`` cases; the
        ``..`` check closes the defence-in-depth gap where a relative
        path like ``../../tmp`` would still resolve outside the vault
        root when joined against ``vault_path``.
        """
        if value.is_absolute():
            msg = (
                f"staging_subpath {value!r} must be vault-relative; "
                "absolute paths could escape the vault root."
            )
            raise ValueError(msg)
        if ".." in value.parts:
            msg = (
                f"staging_subpath {value!r} must not contain '..'; "
                "parent-directory segments could escape the vault root."
            )
            raise ValueError(msg)
        return value

    @field_validator("channel_privacy_tiers")
    @classmethod
    def _validate_channel_tiers(cls, value: dict[int, str]) -> dict[int, str]:
        """Refuse unknown privacy tier strings at config-parse time.

        The MCP server validates the ``privacy_tier_ceiling`` argument
        downstream, but a bogus override in ``crawdad.yaml`` would
        surface as a confusing MCP error at runtime instead of a clear
        config error at startup. Restrict the value set to the four
        ``TierCeiling`` values (mirrored from
        :class:`creek_mcp.tier_ceiling.TierCeiling` without importing it
        — crawdad has no Python dependency on creek-tools).
        """
        allowed: frozenset[str] = frozenset({"open", "personal", "intimate", "all"})
        for channel_id, tier in value.items():
            if tier not in allowed:
                msg = (
                    f"channel_privacy_tiers[{channel_id}] = {tier!r} is not a valid "
                    f"tier ceiling; expected one of {sorted(allowed)}."
                )
                raise ValueError(msg)
        return value


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
    attachments: AttachmentConfig = Field(default_factory=AttachmentConfig)
    max_loop_rounds: int = Field(default=MAX_LOOP_ROUNDS, ge=1, le=50)
    """Hard cap on the FEAT-015 agent loop (FEAT-036 / #312).

    Raise this when legitimate multi-step requests trip the default
    ceiling — re-ingesting a multi-source vault, large re-classification
    asks, long workflows — and produce the ``too_deep`` fallback in
    Discord. The upper bound of 50 prevents runaway loops and cost; the
    lower bound of 1 keeps the engine sane.
    """

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
