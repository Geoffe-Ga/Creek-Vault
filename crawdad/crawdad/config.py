"""CrawDad configuration model (FEAT-013 §Pre-decided choices).

Secrets come from environment variables: the Discord bot token
(``DISCORD_BOT_TOKEN``) and the selected provider's API key. The backend is
chosen by ``CRAWDAD_PROVIDER`` (``anthropic`` default / ``openai`` / ``gemini``,
#610); the matching key (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` /
``GOOGLE_API_KEY``) is validated at load but **never stored** on the config —
each SDK reads it from env. Everything else (vault path, MCP server command,
allowlists) comes from ``crawdad.yaml``. The sources are merged in
:func:`load_config`.

Per-provider router/composer model tiers live here so no other module under
``crawdad/crawdad/`` references a model-ID literal — model IDs move; the
defaults are the contract, not the literal. ``CRAWDAD_ROUTER_MODEL`` /
``CRAWDAD_COMPOSER_MODEL`` override the per-provider fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crawdad.consent import (
    DEFAULT_ABANDON_TOKENS,
    DEFAULT_CONSENT_TOKENS,
    DEFAULT_PENDING_BATCH_TTL_SECONDS,
)

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
_ENV_PROVIDER = "CRAWDAD_PROVIDER"
_ENV_ROUTER_MODEL = "CRAWDAD_ROUTER_MODEL"
_ENV_COMPOSER_MODEL = "CRAWDAD_COMPOSER_MODEL"
_DEFAULT_MCP_COMMAND: tuple[str, ...] = ("creek-tools-mcp",)

DEFAULT_PROVIDER: str = "anthropic"
"""The backend used when ``CRAWDAD_PROVIDER`` is unset."""

# Provider → the environment variable its SDK reads the API key from. The key
# is validated at load time and read by the SDK from env — never stored on
# :class:`CrawDadConfig`, logged, or written to YAML.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}

# Per-provider model tiers. These are the ONLY model literals in the package —
# router/composer modules read the resolved values, never a literal (enforced
# by ``test_no_model_literals.py``). ``CRAWDAD_ROUTER_MODEL`` /
# ``CRAWDAD_COMPOSER_MODEL`` override regardless of provider, as before.
_ROUTER_MODEL_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}
_COMPOSER_MODEL_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.5-pro",
}


def _resolve_model(provider: str, override_env: str, defaults: dict[str, str]) -> str:
    """Resolve a model tier for *provider*, honoring an env override.

    Raises on an unknown provider rather than silently falling back to the
    Anthropic default — the load-time validator catches operator typos, so an
    unknown value here is a programmer error, not something to paper over.

    Args:
        provider: The selected backend.
        override_env: The env var that overrides the default tier when set.
        defaults: The per-provider default model map.

    Returns:
        The override when set, else the provider's default tier.

    Raises:
        ValueError: When *provider* names no known backend.
    """
    override = os.environ.get(override_env)
    if override:
        return override
    try:
        return defaults[provider]
    except KeyError:
        known = ", ".join(sorted(defaults))
        msg = f"unknown provider {provider!r}; expected one of: {known}"
        # The KeyError adds no caller-useful context; suppress its chain.
        raise ValueError(msg) from None


def router_model_for(provider: str) -> str:
    """Resolve the router (intent-extraction) model for *provider*.

    Args:
        provider: The selected backend (``anthropic`` / ``openai`` / ``gemini``).

    Returns:
        ``CRAWDAD_ROUTER_MODEL`` when set, else the provider's default tier.

    Raises:
        ValueError: When *provider* names no known backend.
    """
    return _resolve_model(provider, _ENV_ROUTER_MODEL, _ROUTER_MODEL_DEFAULTS)


def composer_model_for(provider: str) -> str:
    """Resolve the composer (prose) model for *provider*.

    Args:
        provider: The selected backend (``anthropic`` / ``openai`` / ``gemini``).

    Returns:
        ``CRAWDAD_COMPOSER_MODEL`` when set, else the provider's default tier.

    Raises:
        ValueError: When *provider* names no known backend.
    """
    return _resolve_model(provider, _ENV_COMPOSER_MODEL, _COMPOSER_MODEL_DEFAULTS)


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

# The privacy tier vocabulary a channel override may name. Mirrored from
# :class:`creek_mcp.tier_ceiling.TierCeiling` without importing it — crawdad
# has no Python dependency on creek-tools. Enforced at config-parse time by
# :meth:`AttachmentConfig._validate_channel_tiers`.
_VALID_CHANNEL_TIERS: Final[frozenset[str]] = frozenset(
    {"open", "personal", "intimate", "all"}
)

# The only channel tiers whose messages bot-capture may write to the vault
# (#1052).
#
# A capture record carries no tier field (``capture.py::_record_for``), and the
# creek-tools side that reads the capture dir
# (``stage_capture_as_data_package``) drops channel metadata, so a captured
# message lands downstream as ``unclassified`` — which ranks WITH ``personal``
# (``creek_mcp/tier_ceiling.py``, #961). Capture may therefore carry only
# content whose ceiling the record can represent: ``open`` (narrower than
# ``personal`` once landed) and ``personal`` (exact). ``intimate`` must be
# refused, and so must ``all``, which admits intimate content by definition.
#
# Written out explicitly rather than derived as
# ``_VALID_CHANNEL_TIERS - {"intimate", "all"}``: a subtraction would silently
# auto-admit any tier added to the vocabulary later, when the safe default for
# an unreviewed tier is refusal.
#
# The value coincides with ``workflows.WORKFLOW_ADMITTED_CEILINGS`` but is
# deliberately NOT derived from it, and neither is canonical for the other:
# that set draws its line at the cloud composer, this one at what a capture
# record can represent. The reasoning above stands on its own if the workflow
# set ever changes.
#
# Membership (``in``), never a rank comparison: ``crawdad.intents`` owns the
# single tier-ordering table (``CEILING_RANK``, moved there from
# ``crawdad.loop`` by #1152 so the capping rule sits beside the vocabulary it
# caps) and a second one must not exist. The values stay
# plain ``str`` rather than ``intents.PrivacyTierCeiling`` members because
# ``channel_privacy_tiers`` is a ``dict[int, str]`` of operator-written YAML
# and this module deliberately owns the tier vocabulary without importing
# ``crawdad.intents``. (A ``StrEnum`` member would in fact match a plain string
# here — ``str.__hash__`` precedes ``Enum.__hash__`` in the MRO — so this is a
# layering choice, not a correctness workaround.)
CAPTURE_ADMITTED_TIERS: Final[frozenset[str]] = frozenset({"open", "personal"})

# The ceiling assumed for a channel with no ``channel_privacy_tiers`` entry.
# Ingest writes are personal by default per FEAT-011, so a missing entry never
# silently relaxes the ceiling. Named rather than inlined because
# :func:`crawdad.bot._channel_tier` and :data:`CAPTURE_ADMITTED_TIERS` jointly
# decide whether an operator who never wrote a ``channel_privacy_tiers`` block
# keeps bot-capture: that promise holds only while this value is a member of
# that set, which ``test_default_channel_tier_is_capture_admitted`` pins.
DEFAULT_CHANNEL_TIER: Final[str] = "personal"


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
        ``TierCeiling`` values (:data:`_VALID_CHANNEL_TIERS`, mirrored
        from :class:`creek_mcp.tier_ceiling.TierCeiling` without
        importing it — crawdad has no Python dependency on creek-tools).
        """
        for channel_id, tier in value.items():
            if tier not in _VALID_CHANNEL_TIERS:
                msg = (
                    f"channel_privacy_tiers[{channel_id}] = {tier!r} is not a valid "
                    f"tier ceiling; expected one of {sorted(_VALID_CHANNEL_TIERS)}."
                )
                raise ValueError(msg)
        return value


class ConsentConfig(BaseModel):
    """FEAT-034 conversational-consent knobs.

    The consent flow lets a user reply with ``ingest`` (or any of the
    other affirmative tokens below) to dispatch ``creek.ingest`` for a
    previously staged batch of Discord attachments. Operators can
    widen the token lists or shorten the timeout per deployment.

    Attributes:
        consent_tokens: Affirmative tokens that trigger ingest dispatch
            for the channel's most recently staged batch. Matching is
            case-insensitive after stripping surrounding whitespace and
            ASCII punctuation; the user's message must normalise to a
            token verbatim (multi-word tokens like ``"go ahead"`` keep
            their internal space).
        abandon_tokens: Tokens that clear the pending batch without
            dispatching ingest. Matched with the same normalisation
            rules as the consent tokens.
        pending_batch_ttl_seconds: Maximum age of a pending batch
            before it is treated as expired. A stale "yes" after this
            window falls through to the agent loop instead of
            triggering an unexpected ingest.
    """

    model_config = ConfigDict(frozen=True)

    consent_tokens: frozenset[str] = Field(
        default_factory=lambda: frozenset(DEFAULT_CONSENT_TOKENS),
    )
    abandon_tokens: frozenset[str] = Field(
        default_factory=lambda: frozenset(DEFAULT_ABANDON_TOKENS),
    )
    pending_batch_ttl_seconds: float = Field(
        default=DEFAULT_PENDING_BATCH_TTL_SECONDS,
        gt=0,
    )

    @field_validator("consent_tokens", "abandon_tokens")
    @classmethod
    def _normalise_tokens(cls, value: frozenset[str]) -> frozenset[str]:
        """Lowercase + strip every token; drop empties.

        Stored verbatim so the runtime comparison against the
        already-normalised inbound message text is a single equality
        check.
        """
        normalised: set[str] = set()
        for raw in value:
            if not raw:
                continue
            cleaned = raw.lower().strip()
            if cleaned:
                normalised.add(cleaned)
        return frozenset(normalised)

    @model_validator(mode="after")
    def _refuse_token_overlap(self) -> Self:
        """Refuse configurations where consent and abandon tokens overlap.

        ``classify_followup_message`` checks the abandon set before the
        consent set, so any token present in both would silently always
        classify as abandon — a confusing semantic inversion the
        operator did not intend. Surfacing this at config-parse time
        keeps the misconfiguration loud instead of latent.
        """
        overlap = self.consent_tokens & self.abandon_tokens
        if overlap:
            msg = (
                "consent_tokens and abandon_tokens must be disjoint; "
                f"overlapping tokens: {sorted(overlap)}"
            )
            raise ValueError(msg)
        return self


class CrawDadConfig(BaseModel):
    """Immutable runtime configuration for the bot.

    Allowlists are tuples (frozen) to discourage in-place mutation
    across an async session.
    """

    model_config = ConfigDict(frozen=True)

    discord_bot_token: str = Field(min_length=1)
    llm_provider: str = Field(default=DEFAULT_PROVIDER, min_length=1)
    """The selected LLM backend; the SDK reads its key from env, never here."""
    vault_path: Path
    mcp_server_command: tuple[str, ...] = _DEFAULT_MCP_COMMAND
    allowed_user_ids: tuple[int, ...]
    allowed_channel_ids: tuple[int, ...]
    attachments: AttachmentConfig = Field(default_factory=AttachmentConfig)
    consent: ConsentConfig = Field(default_factory=ConsentConfig)
    max_loop_rounds: int = Field(default=MAX_LOOP_ROUNDS, ge=1, le=50)
    """Operator override for the FEAT-015 agent-loop round cap."""

    capture_enabled: bool = Field(default=False)
    """Bot-capture toggle (#687) — OFF by default; opt-in per deployment.

    When ``True`` the bot logs each message in the servers/channels it is in to
    ``<vault>/<capture_subpath>/<channel>/<date>.jsonl`` for Tier-A ingest. The
    bot cannot read DMs (a hard Discord API limit), so this covers channels only.
    """

    capture_subpath: Path = Field(default=Path("discord-capture"))
    """Vault-relative dir the bot-capture writer appends to (#687).

    Mirrors the creek-side ``DiscordSourceConfig.capture_subpath`` default so the
    bot writes exactly where Tier-A ingest reads. Must stay inside the vault.
    """

    @field_validator("capture_subpath")
    @classmethod
    def _refuse_absolute_capture_subpath(cls, value: Path) -> Path:
        """Refuse absolute / ``..`` capture paths — must stay in the vault (#687)."""
        if value.is_absolute():
            msg = (
                f"capture_subpath {value!r} must be vault-relative; "
                "absolute paths could escape the vault root."
            )
            raise ValueError(msg)
        if ".." in value.parts:
            msg = (
                f"capture_subpath {value!r} must not contain '..'; "
                "parent-directory segments could escape the vault root."
            )
            raise ValueError(msg)
        return value

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

    The backend is selected by ``CRAWDAD_PROVIDER`` (default ``anthropic``); the
    matching provider key (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` /
    ``GOOGLE_API_KEY``) must be present in the environment but is **never**
    stored on the config — the SDK reads it from env at call time.

    Args:
        yaml_path: Path to the YAML config. Defaults to ``./crawdad.yaml``.

    Raises:
        RuntimeError: when ``DISCORD_BOT_TOKEN`` is missing, ``CRAWDAD_PROVIDER``
            names an unknown backend, or the selected provider's key env var is
            unset — secrets must never come from the YAML.
        FileNotFoundError: when the YAML config is missing.
    """
    discord_token = os.environ.get(_ENV_DISCORD_TOKEN)
    if not discord_token:
        msg = f"missing required env var {_ENV_DISCORD_TOKEN}"
        raise RuntimeError(msg)

    provider = os.environ.get(_ENV_PROVIDER, DEFAULT_PROVIDER).strip().lower()
    key_env = _PROVIDER_KEY_ENV.get(provider)
    if key_env is None:
        known = ", ".join(sorted(_PROVIDER_KEY_ENV))
        msg = f"unknown {_ENV_PROVIDER} {provider!r}; expected one of: {known}"
        raise RuntimeError(msg)
    if not os.environ.get(key_env, "").strip():
        msg = f"missing required env var {key_env} for {_ENV_PROVIDER}={provider}"
        raise RuntimeError(msg)

    path = yaml_path or Path("crawdad.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw["discord_bot_token"] = discord_token
    raw["llm_provider"] = provider
    return CrawDadConfig.model_validate(raw)
