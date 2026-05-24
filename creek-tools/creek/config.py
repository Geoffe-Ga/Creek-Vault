"""Creek configuration module — loads creek_config.yaml to Pydantic Settings models.

Provides typed configuration for every subsystem in the Creek pipeline:
LLM, embeddings, OCR, linking, classification, redaction, Google Drive,
and source paths. Configuration values are loaded from a YAML file and
can be overridden by environment variables prefixed with ``CREEK_``.

API keys (e.g. ``ANTHROPIC_API_KEY``) are **never** stored in the YAML
file — they must come from environment variables.
"""

import logging
import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV_VAR = "CREEK_CONFIG"
"""Environment variable that, when set, supplies the default config path.

Honoured by :func:`load_config` when no explicit ``config_path``
argument is given. Subprocess-friendly: any spawned process inherits
the variable, so callers (e.g. CrawDad spawning ``creek-tools-mcp``)
can keep the configured vault config discoverable regardless of cwd.
"""


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    provider: str = "ollama"
    """LLM backend — ``ollama``, ``anthropic``, or ``openai``."""

    model: str = "mistral"
    """Model identifier recognised by the chosen provider."""

    ollama_url: str = "http://localhost:11434"
    """Base URL for the Ollama API server."""

    batch_size: int = 50
    """Number of items to process per LLM batch call."""

    max_concurrent: int = 5
    """Maximum number of concurrent LLM requests."""

    unclassified_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    """Per-dimension confidence floor below which Mode / Orientation /
    Dosage default to ``unclassified`` rather than the model's pick (FEAT-017).

    Frequency, Phase, and Voice Register are not gated by this knob —
    they are more stable signals empirically and using them is the
    point of having an LLM pass at all. The 0.55 default is deliberately
    lenient; tighten it once a calibration set exists (FEAT-017b) and
    the per-dimension agreement rate on your corpus is known.
    """


class EmbeddingsConfig(BaseModel):
    """Embedding model configuration."""

    model: str = "all-MiniLM-L6-v2"
    """Sentence-transformer model used to generate embeddings."""

    similarity_threshold: float = 0.75
    """Minimum cosine similarity for linking fragments."""

    cache_dir: str | None = None
    """Local directory for caching downloaded models."""

    batch_size: int = 32
    """Number of texts to encode per batch."""


class OCRConfig(BaseModel):
    """OCR configuration."""

    enabled: bool = True
    """Whether to run OCR on image-based sources."""

    engine: str = "pytesseract"
    """OCR engine to use."""

    languages: list[str] = Field(default_factory=lambda: ["eng"])
    """Tesseract language codes for OCR."""

    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    """Minimum OCR confidence below which a fragment lands in the review queue.

    Image and scanned-PDF ingestors compare the per-page confidence
    reported by the engine to this threshold; a fragment whose OCR
    score falls below it is tagged ``review: pending_review`` in
    frontmatter so a human can verify the recovered text.
    """


class LinkingConfig(BaseModel):
    """Linking pipeline configuration."""

    temporal_window_hours: int = 168
    """Time window (hours) for temporal proximity linking (default 1 week)."""

    thread_min_fragments: int = 3
    """Minimum fragments required to form a Thread."""

    eddy_min_fragments: int = 5
    """Minimum fragments required to form an Eddy."""

    exchange_max_gap_minutes: int = Field(default=30, ge=1)
    """Maximum minute-gap between consecutive messages still grouped into
    the same ``exchange`` by the FEAT-022 aggregator. Inclusive boundary.
    """

    burst_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    """Cosine-similarity floor below which two consecutive exchanges start
    separate ``burst``-level parents in the FEAT-022 aggregator.
    Inclusive boundary.
    """

    session_max_gap_minutes: int = Field(default=360, ge=1)
    """Maximum minute-gap between consecutive bursts still grouped into the
    same ``session`` by the FEAT-022 aggregator. Inclusive boundary.
    """

    hierarchy_sibling_skip_window: int = Field(default=2, ge=0)
    """FEAT-024 sibling-suppression window for hierarchy-aware linking.

    With re-atomization (FEAT-020..023) every parent fragment shares the
    vault with its own children, and adjacent children of one parent are
    usually topically continuous — so naive cosine similarity emits noise
    instead of meaningful resonances. The embedding linker drops pairs
    that are ancestor/descendant unconditionally, and pairs that share a
    parent and sit within this many positions of each other in the
    parent's ``child_ids`` list. ``0`` disables sibling suppression
    entirely (ancestor suppression still applies); the default of ``2``
    skips immediate and one-removed neighbours on either side.
    """


class ClassificationConfig(BaseModel):
    """Classification pipeline configuration."""

    confidence_threshold: float = 0.7
    """Minimum confidence score for automatic classification."""

    auto_classify_sources: list[str] = Field(
        default_factory=lambda: ["claude", "chatgpt", "discord"],
    )
    """Sources that are auto-classified without human review."""

    human_review_sources: list[str] = Field(
        default_factory=lambda: ["journal"],
    )
    """Sources that require human review after classification."""

    reatomize: bool = False
    """Opt-in switch for FEAT-023 confidence-driven re-atomization.

    When ``False`` (default) the classify pipeline behaves exactly as
    it did before FEAT-023: a single pass over each fragment, no
    structural decomposition. When ``True``, low-confidence /
    ``unclassified`` results trigger the zoom-in splitter (FEAT-021) or
    zoom-out aggregator (FEAT-022), recursing until a leaf reaches the
    confidence threshold, a terminal level (sentence / session), or
    :attr:`reatomize_max_depth`.
    """

    reatomize_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """Confidence floor that triggers re-atomization (FEAT-023).

    ``None`` (default) means "use :attr:`confidence_threshold`" — the
    same floor that already gates a paid LLM call. Set this lower than
    the main threshold to keep the rules-vs-LLM gate at one place but
    only fire re-atomization on truly hopeless fragments.
    """

    reatomize_max_depth: int = Field(default=4, ge=1)
    """Hard ceiling on FEAT-023 recursion depth.

    Caps unbounded zoom-in on a fractal document. Counted from the
    root fragment (depth 0). Once reached, the orchestrator accepts
    whatever classification it has, even if still ``unclassified``.
    """

    reatomize_direction: Literal["auto", "split", "aggregate"] = "auto"
    """FEAT-023 direction-choice override.

    ``"auto"`` (default) routes by ``source.platform`` and ``level``
    per the heuristic in :mod:`creek.classify.reatomize`. ``"split"``
    or ``"aggregate"`` force a single direction regardless of source
    — useful for triage runs over a known-uniform corpus.
    """


class ContextConfig(BaseModel):
    """Non-user content handling configuration.

    Controls how content authored by others (e.g. Discord messages,
    collaborative documents) is represented in the vault.
    """

    mode: str = "context_metadata"
    """Handling mode: ``context_metadata``, ``low_priority``, or ``skip``.

    - ``context_metadata``: Store others' content as context in the user's
      fragment; do not index separately.
    - ``low_priority``: Ingest as separate fragments with ``author: other``
      and reduced quality scores; exclude from voice proxy.
    - ``skip``: Drop others' content entirely; preserve only user's words.
    """

    quality_penalty: float = 0.5
    """Multiplier applied to quality scores for ``low_priority`` fragments."""

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate that *v* is a recognised content handling mode.

        Args:
            v: Mode string to validate.

        Returns:
            The validated mode string.

        Raises:
            ValueError: If the mode is not one of the allowed values.
        """
        allowed = {"context_metadata", "low_priority", "skip"}
        if v not in allowed:
            msg = f"Invalid context mode: {v!r}. Must be one of {allowed}"
            raise ValueError(msg)
        return v

    @field_validator("quality_penalty")
    @classmethod
    def validate_quality_penalty(cls, v: float) -> float:
        """Validate quality penalty is in [0.0, 1.0].

        Args:
            v: Penalty multiplier to validate.

        Returns:
            The validated penalty value.

        Raises:
            ValueError: If the penalty is outside the valid range.
        """
        if not 0.0 <= v <= 1.0:
            msg = f"quality_penalty must be in [0.0, 1.0], got {v}"
            raise ValueError(msg)
        return v


class RedactionConfig(BaseModel):
    """Redaction scanner configuration."""

    enabled: bool = True
    """Whether to run the PII redaction scanner."""

    dry_run: bool = False
    """If ``True``, report redactions but do not apply them."""

    custom_patterns: dict[str, str] = Field(default_factory=dict)
    """Extra regex patterns (name -> pattern) for the scanner."""

    false_positive_allowlist: list[str] = Field(default_factory=list)
    """Strings that should never be flagged as PII."""

    supported_extensions: list[str] = Field(
        default_factory=lambda: [
            ".txt",
            ".md",
            ".json",
            ".py",
            ".env",
            ".yaml",
            ".toml",
            ".csv",
        ],
    )
    """File extensions the scanner will process."""

    exclude_patterns: list[str] = Field(
        default_factory=lambda: [".git", "node_modules"],
    )
    """Directory name patterns to exclude from recursive scanning."""

    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    """Confidence threshold for the generic high-entropy secret detector.

    Higher values demand more entropy before flagging a substring; ``0.0``
    catches everything that looks base64-ish, ``1.0`` requires near-random
    output. The default ``0.6`` corresponds to roughly 4.2 bits/char which
    suppresses most natural-language false positives.
    """

    replacement_template: str = "[REDACTED:{name}]"
    """Format string used by :class:`creek.redact.redactor.Redactor`.

    Must contain the ``{name}`` placeholder so the matched pattern's name
    is interpolated into the marker. Other format placeholders are
    rejected at validation time to surface typos early.
    """

    @field_validator("replacement_template")
    @classmethod
    def validate_replacement_template(cls, v: str) -> str:
        """Reject templates lacking ``{name}`` or carrying other placeholders."""
        # Format with a sentinel; if substitution didn't change the string,
        # `{name}` was absent. KeyError/IndexError/ValueError surface unknown
        # placeholders like `{type}` or malformed format specs.
        try:
            formatted = v.format(name="\x00creek_name_sentinel\x00")
        except (KeyError, IndexError, ValueError) as exc:
            msg = (
                f"replacement_template {v!r} has invalid placeholders; "
                "only '{name}' is supported."
            )
            raise ValueError(msg) from exc
        if formatted == v:
            msg = f"replacement_template {v!r} must include the '{{name}}' placeholder."
            raise ValueError(msg)
        return v


_READONLY_SCOPES: set[str] = {
    "https://www.googleapis.com/auth/drive.readonly",
}


class GoogleDriveConfig(BaseModel):
    """Google Drive configuration (READ-ONLY scopes enforced)."""

    credentials_file: str = "credentials.json"
    """Path to the OAuth2 credentials file."""

    token_file: str = "token.json"
    """Path to the cached OAuth2 token file."""

    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    """OAuth2 scopes — **must** be read-only."""

    staging_dir: str = "google-drive-export/"
    """Local directory for staging downloaded files."""

    @field_validator("scopes")
    @classmethod
    def validate_readonly_scopes(cls, v: list[str]) -> list[str]:
        """Enforce read-only scopes.

        Args:
            v: List of OAuth2 scope strings.

        Returns:
            The validated list of scopes.

        Raises:
            ValueError: If any scope is not in the read-only allowlist.
        """
        for scope in v:
            if scope not in _READONLY_SCOPES:
                msg = f"Only read-only scopes allowed. Got: {scope}"
                raise ValueError(msg)
        return v


class DiscordCleaningConfig(BaseModel):
    """Discord message cleaning configuration."""

    filter_bot_messages: bool = True
    """Whether to filter out messages from bots."""

    strip_emoji: bool = False
    """Whether to strip emoji characters from messages."""

    filter_commands: bool = True
    """Whether to filter out bot command messages (e.g. ``!help``)."""

    min_message_length: int = 10
    """Minimum character length for a message to be kept."""


class ChatbotCleaningConfig(BaseModel):
    """Chatbot export cleaning configuration.

    Controls which noise types are filtered from chatbot conversation
    exports before fragment extraction.
    """

    filter_system_prompts: bool = True
    """Whether to filter out system prompt content."""

    filter_tool_outputs: bool = True
    """Whether to filter out tool/function call outputs."""

    filter_regenerations: bool = True
    """Whether to filter out regenerated responses."""

    min_human_turn_length: int = 20
    """Minimum character count for human turns (below = skip)."""

    code_block_threshold: float = 0.9
    """Code-block ratio above which a response is flagged as code-only."""

    max_abandoned_turns: int = 2
    """Max turn pairs for abandoned conversation detection."""


class MarkdownCleaningConfig(BaseModel):
    """Markdown file cleaning configuration."""

    skip_empty_files: bool = True
    """Whether to skip files with no meaningful body content."""

    min_body_length: int = 50
    """Minimum character length for the body to be considered non-empty."""


class GoogleDriveCleaningConfig(BaseModel):
    """Google Drive document cleaning configuration."""

    deduplicate: bool = True
    """Whether to deduplicate downloaded documents."""

    filter_empty_docs: bool = True
    """Whether to filter out empty or placeholder documents."""

    max_collaboration_ratio: float = 0.9
    """Maximum ratio of non-owner edits before a doc is flagged as collaborative."""


class ValidationConfig(BaseModel):
    """Content validation configuration."""

    min_characters: int = 20
    """Minimum character count for valid content."""

    min_words: int = 5
    """Minimum word count for valid content."""

    max_stop_word_ratio: float = 0.8
    """Maximum ratio of stop words before content is flagged as low-quality."""

    require_metadata: bool = True
    """Whether to require metadata fields (title, date, source)."""


class QualityConfig(BaseModel):
    """Quality scoring configuration."""

    accept_threshold: float = 0.7
    """Minimum quality score to automatically accept content."""

    skip_threshold: float = 0.3
    """Quality score below which content is automatically skipped."""


class DeduplicationConfig(BaseModel):
    """Deduplication configuration."""

    strategy: str = "fuzzy"
    """Matching strategy — ``exact`` or ``fuzzy``."""

    similarity_threshold: float = 0.85
    """Minimum similarity score for fuzzy deduplication."""


class HygieneConfig(BaseModel):
    """Data hygiene configuration."""

    track_orphans: bool = True
    """Whether to track orphaned fragments with no connections."""

    staleness_days: int = 90
    """Number of days before a fragment is considered stale."""


class CleaningConfig(BaseModel):
    """Top-level cleaning pipeline configuration."""

    discord: DiscordCleaningConfig = Field(
        default_factory=DiscordCleaningConfig,
    )
    """Discord message cleaning settings."""

    chatbot: ChatbotCleaningConfig = Field(
        default_factory=ChatbotCleaningConfig,
    )
    """Chatbot export cleaning settings."""

    markdown: MarkdownCleaningConfig = Field(
        default_factory=MarkdownCleaningConfig,
    )
    """Markdown file cleaning settings."""

    google_drive: GoogleDriveCleaningConfig = Field(
        default_factory=GoogleDriveCleaningConfig,
    )
    """Google Drive document cleaning settings."""

    validation: ValidationConfig = Field(
        default_factory=ValidationConfig,
    )
    """Content validation settings."""

    quality: QualityConfig = Field(
        default_factory=QualityConfig,
    )
    """Quality scoring settings."""

    deduplication: DeduplicationConfig = Field(
        default_factory=DeduplicationConfig,
    )
    """Deduplication settings."""

    hygiene: HygieneConfig = Field(
        default_factory=HygieneConfig,
    )
    """Data hygiene settings."""


class CompostConfig(BaseModel):
    """Compost detection configuration (FEAT-018).

    Replaces the legacy five-phrase abandonment keyword regex with a
    two-stage pipeline: an embedding-similarity gate against curated
    exemplars (``creek/generate/exemplars/compost.yaml``), followed by
    an optional LLM verifier that returns ``yes`` / ``no`` /
    ``ambiguous``. Ambiguous verdicts route to a review queue rather
    than the canonical compost folder.
    """

    embedding_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    """Cosine-similarity floor above which a fragment is sent to the
    verifier. Deliberately wide-net (0.6) — the verifier owns precision.
    Tighten once a calibration set exists for your real corpus.
    """

    llm_verification: bool = True
    """When ``True``, candidates that pass the embedding gate are sent
    to the LLM verifier. When ``False``, the embedding gate alone
    decides acceptance (faster + offline, but more false positives).
    """

    review_queue_relpath: str = "10-Liminal/Compost/Review"
    """Vault-relative path under which ``ambiguous`` verdicts are filed.
    Operators triage this queue manually and either promote to canonical
    compost or delete.
    """

    skip_paradox: bool = True
    """When ``True``, fragments tagged ``paradox`` or living under
    ``10-Liminal/Paradoxes/`` are skipped — paradoxes are contradiction-
    holding notes by design, not compost candidates.
    """

    exemplars_relpath: str | None = None
    """Vault-relative path to a custom exemplars YAML. When ``None``,
    the packaged default (``creek/generate/exemplars/compost.yaml``)
    is used. Override only after running ``creek compost calibrate``
    against your corpus has shown the defaults under-recall.
    """


class LintConfig(BaseModel):
    """Configuration for ``creek lint`` checks (FEAT-008, FEAT-025).

    Today only the paradox check carries a knob; this class exists so
    additional check-level toggles can land without expanding the
    top-level :class:`CreekConfig` surface.
    """

    paradox_cross_level: bool = False
    """FEAT-025 opt-in for cross-level paradox detection.

    ``False`` (default): the paradox check skips pairs whose
    :attr:`creek.models.Fragment.level` values differ — a paragraph
    contradicting the section it lives inside is rhetorical structure,
    not the contradiction `10-Liminal/Paradoxes/` is for.

    ``True``: restore the pre-FEAT-025 behaviour. Useful when the
    operator wants the cross-level pairs surfaced (e.g. to audit a
    re-atomized vault for splitter mistakes).
    """


class SourcePaths(BaseModel):
    """Source data paths (relative to ``source_drive``)."""

    claude: str = "chatbot-exports/claude/"
    """Claude conversation exports."""

    chatgpt: str = "chatbot-exports/chatgpt/"
    """ChatGPT conversation exports."""

    discord: str = "discord-export/"
    """Discord message exports."""

    gdrive: str = "google-drive-export/"
    """Google Drive staged files."""

    aptitude: str = "projects/aptitude/course-files/"
    """APTITUDE course materials."""

    essays: str = "writing/substack/"
    """Published essays (Substack)."""

    journal: str = "personal/journal/"
    """Personal journal entries."""

    code: str = "projects/"
    """Code project directories."""


class CreekConfig(BaseSettings):
    """Top-level Creek configuration.

    Values are loaded from a YAML file and can be overridden by
    environment variables prefixed with ``CREEK_`` (e.g.
    ``CREEK_VAULT_PATH``, ``CREEK_TIMEZONE``).
    """

    model_config = SettingsConfigDict(
        env_prefix="CREEK_",
    )

    vault_path: Path = Path()
    """Path to the Obsidian vault root."""

    source_drive: Path = Path()
    """Path to the mounted source drive containing raw exports."""

    timezone: str = "America/Los_Angeles"
    """IANA timezone for timestamp normalisation."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    """LLM provider settings."""

    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    """Embedding model settings."""

    ocr: OCRConfig = Field(default_factory=OCRConfig)
    """OCR processing settings."""

    linking: LinkingConfig = Field(default_factory=LinkingConfig)
    """Fragment linking settings."""

    classification: ClassificationConfig = Field(
        default_factory=ClassificationConfig,
    )
    """Classification pipeline settings."""

    context: ContextConfig = Field(default_factory=ContextConfig)
    """Non-user content handling settings."""

    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    """PII redaction scanner settings."""

    google_drive: GoogleDriveConfig = Field(
        default_factory=GoogleDriveConfig,
    )
    """Google Drive connector settings."""

    cleaning: CleaningConfig = Field(default_factory=CleaningConfig)
    """Data cleaning pipeline settings."""

    compost: CompostConfig = Field(default_factory=CompostConfig)
    """Compost detection settings (FEAT-018: embedding gate + verifier)."""

    lint: LintConfig = Field(default_factory=LintConfig)
    """Lint check toggles (FEAT-008 / FEAT-025)."""

    sources: SourcePaths = Field(default_factory=SourcePaths)
    """Source data path mappings."""

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Validate that *v* is a recognised IANA timezone.

        Args:
            v: Timezone string to validate.

        Returns:
            The validated timezone string.

        Raises:
            ValueError: If the timezone is not recognised by ``zoneinfo``.
        """
        try:
            ZoneInfo(v)
        except KeyError as exc:
            msg = f"Invalid timezone: {v}"
            raise ValueError(msg) from exc
        return v


def load_config(
    config_path: Path | None = None,
    *,
    warn_on_missing: bool = True,
) -> CreekConfig:
    """Load configuration from a YAML file with environment variable overrides.

    If *config_path* does not exist, returns a ``CreekConfig`` populated
    entirely from defaults and environment variables and (per ARCH-002)
    emits a ``WARNING`` so the operator knows their data-handling
    decisions are being made by the defaults rather than their own
    config. Pass ``warn_on_missing=False`` to silence the warning when
    the caller has already established that the missing file is
    expected (e.g. ``creek init`` runs before any config exists).

    Args:
        config_path: Path to a ``creek_config.yaml`` file. When ``None``
            (the default), :func:`load_config` consults the
            ``CREEK_CONFIG`` environment variable; if that is unset or
            empty, it falls back to ``creek_config.yaml`` in the
            current directory.
        warn_on_missing: When ``True`` (default), log a WARNING if the
            file does not exist. Suppress for CLI commands that
            legitimately operate in the no-config state.

    Returns:
        A fully-validated ``CreekConfig`` instance.

    Raises:
        FileNotFoundError: When ``CREEK_CONFIG`` is set to a path that
            does not exist. The explicit env var declares intent, so a
            silent fallback to defaults would mask the misconfiguration.
    """
    if config_path is None:
        env_value = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
        if env_value:
            config_path = Path(env_value)
            if not config_path.exists():
                msg = (
                    f"{CONFIG_PATH_ENV_VAR} points to {config_path}, "
                    "but no file exists there. Unset the environment "
                    "variable or correct the path."
                )
                raise FileNotFoundError(msg)
        else:
            config_path = Path("creek_config.yaml")

    if config_path.exists():
        with config_path.open() as f:
            data: dict[str, object] = yaml.safe_load(f) or {}
        return CreekConfig.model_validate(data)

    if warn_on_missing:
        logger.warning(
            "Config file %s not found; running with built-in defaults. "
            "Run `creek init --vault <vault>` to write a starter config, "
            "set %s to an absolute config path, or pass --config <path> "
            "explicitly. Pipeline behaviour (privacy, redaction, "
            "cleaning) depends on this file — silent defaults are "
            "usually not what you want.",
            config_path,
            CONFIG_PATH_ENV_VAR,
        )
    return CreekConfig()


def generate_default_config(output_path: Path) -> None:
    """Generate a default ``creek_config.yaml`` file.

    Serialises the default ``CreekConfig`` to YAML and writes it to
    *output_path*, providing a starting template that users can customise.

    Args:
        output_path: Destination file path for the generated YAML.
    """
    data: dict[str, object] = CreekConfig().model_dump(mode="json")
    with output_path.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
