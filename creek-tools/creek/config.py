"""Creek configuration module — loads creek_config.yaml to Pydantic Settings models.

Provides typed configuration for every subsystem in the Creek pipeline:
LLM, embeddings, OCR, linking, classification, redaction, cleaning,
Google Drive, and source paths. Configuration values are loaded from a
YAML file and can be overridden by environment variables prefixed with
``CREEK_``.

API keys (e.g. ``ANTHROPIC_API_KEY``) are **never** stored in the YAML
file — they must come from environment variables.
"""

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class EmbeddingsConfig(BaseModel):
    """Embedding model configuration."""

    model: str = "all-MiniLM-L6-v2"
    """Sentence-transformer model used to generate embeddings."""

    similarity_threshold: float = 0.75
    """Minimum cosine similarity for linking fragments."""


class OCRConfig(BaseModel):
    """OCR configuration."""

    enabled: bool = True
    """Whether to run OCR on image-based sources."""

    engine: str = "pytesseract"
    """OCR engine to use."""

    languages: list[str] = Field(default_factory=lambda: ["eng"])
    """Tesseract language codes for OCR."""


class LinkingConfig(BaseModel):
    """Linking pipeline configuration."""

    temporal_window_hours: int = 168
    """Time window (hours) for temporal proximity linking (default 1 week)."""

    thread_min_fragments: int = 3
    """Minimum fragments required to form a Thread."""

    eddy_min_fragments: int = 5
    """Minimum fragments required to form an Eddy."""


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


class DiscordCleaningConfig(BaseModel):
    """Discord-specific cleaning configuration."""

    skip_bot_messages: bool = True
    """Whether to skip messages from bots."""

    skip_single_emoji: bool = True
    """Whether to skip messages that contain only a single emoji."""

    skip_commands: bool = True
    """Whether to skip bot command invocations (e.g. ``!ping``, ``/roll``)."""

    min_message_length: int = 3
    """Minimum character length for a message to be kept."""

    include_others_as_context: bool = False
    """Whether to include other users' messages as surrounding context."""


class ChatbotCleaningConfig(BaseModel):
    """Chatbot export cleaning configuration."""

    skip_system_prompts: bool = True
    """Whether to skip system prompt turns."""

    skip_tool_outputs: bool = True
    """Whether to skip tool/function call output turns."""

    collapse_regenerations: bool = True
    """Whether to collapse consecutive regenerated responses into one."""

    min_turn_length: int = 5
    """Minimum character length for a turn to be kept."""


class MarkdownCleaningConfig(BaseModel):
    """Markdown file cleaning configuration."""

    skip_empty_files: bool = True
    """Whether to skip files with no meaningful body content."""

    min_body_length: int = 50
    """Minimum character length of the body (excluding front-matter)."""


class GoogleDriveCleaningConfig(BaseModel):
    """Google Drive document cleaning configuration."""

    skip_copy_of_duplicates: bool = True
    """Whether to skip files named ``Copy of ...``."""

    skip_empty_documents: bool = True
    """Whether to skip documents with no content."""

    max_collaborator_ratio: float = 0.5
    """Maximum ratio of other-author content before skipping the document."""


class ValidationConfig(BaseModel):
    """Content validation thresholds."""

    min_content_chars: int = 10
    """Minimum character count for content to be considered valid."""

    min_content_words: int = 3
    """Minimum word count for content to be considered valid."""

    max_stop_word_ratio: float = 0.8
    """Maximum ratio of stop words before content is flagged as low-quality."""

    require_timestamp: bool = False
    """Whether a timestamp is required on every fragment."""

    require_source_platform: bool = True
    """Whether a source platform identifier is required."""


class QualityConfig(BaseModel):
    """Automatic quality-gate thresholds."""

    auto_accept_threshold: float = 0.9
    """Quality score at or above which fragments are auto-accepted."""

    auto_skip_threshold: float = 0.1
    """Quality score at or below which fragments are auto-skipped."""


class DeduplicationConfig(BaseModel):
    """Deduplication strategy configuration."""

    exact_match: bool = True
    """Whether to deduplicate by exact content match."""

    normalized_match: bool = True
    """Whether to deduplicate by normalised (whitespace/case) match."""

    embedding_similarity: bool = False
    """Whether to deduplicate by embedding cosine similarity."""

    similarity_threshold: float = 0.95
    """Cosine similarity threshold for embedding-based deduplication."""


class HygieneConfig(BaseModel):
    """Vault hygiene maintenance configuration."""

    orphan_days: int = 30
    """Days after which an unlinked fragment is flagged as orphaned."""

    review_queue_stale_days: int = 14
    """Days after which a review-queue item is considered stale."""


class CleaningConfig(BaseModel):
    """Top-level cleaning configuration grouping all cleaning sub-configs."""

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
    """Quality-gate settings."""

    deduplication: DeduplicationConfig = Field(
        default_factory=DeduplicationConfig,
    )
    """Deduplication strategy settings."""

    hygiene: HygieneConfig = Field(
        default_factory=HygieneConfig,
    )
    """Vault hygiene settings."""


class CreekConfig(BaseSettings):
    """Top-level Creek configuration.

    Values are loaded from a YAML file and can be overridden by
    environment variables prefixed with ``CREEK_`` (e.g.
    ``CREEK_VAULT_PATH``, ``CREEK_TIMEZONE``).
    """

    model_config = SettingsConfigDict(
        env_prefix="CREEK_",
    )

    vault_path: Path = Path(".")
    """Path to the Obsidian vault root."""

    source_drive: Path = Path(".")
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

    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    """PII redaction scanner settings."""

    google_drive: GoogleDriveConfig = Field(
        default_factory=GoogleDriveConfig,
    )
    """Google Drive connector settings."""

    sources: SourcePaths = Field(default_factory=SourcePaths)
    """Source data path mappings."""

    cleaning: CleaningConfig = Field(default_factory=CleaningConfig)
    """Cleaning pipeline settings."""

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


def load_config(config_path: Path | None = None) -> CreekConfig:
    """Load configuration from a YAML file with environment variable overrides.

    If *config_path* does not exist, returns a ``CreekConfig`` populated
    entirely from defaults and environment variables.

    Args:
        config_path: Path to a ``creek_config.yaml`` file.  Defaults to
            ``creek_config.yaml`` in the current directory.

    Returns:
        A fully-validated ``CreekConfig`` instance.
    """
    if config_path is None:
        config_path = Path("creek_config.yaml")

    if config_path.exists():
        with config_path.open() as f:
            data: dict[str, object] = yaml.safe_load(f) or {}
        return CreekConfig.model_validate(data)

    return CreekConfig()


def generate_default_config(output_path: Path) -> None:
    """Generate a default ``creek_config.yaml`` file.

    Serialises the default ``CreekConfig`` to YAML and writes it to
    *output_path*, providing a starting template that users can customise.

    Args:
        output_path: Destination file path for the generated YAML.
    """
    config = CreekConfig()
    data: dict[str, object] = config.model_dump(mode="json")
    with output_path.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
