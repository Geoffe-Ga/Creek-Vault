"""Tests for creek.config module — configuration loader with Pydantic Settings."""

from pathlib import Path

import pytest
import yaml

from creek.config import (
    ChatbotCleaningConfig,
    ClassificationConfig,
    CleaningConfig,
    CreekConfig,
    DeduplicationConfig,
    DiscordCleaningConfig,
    EmbeddingsConfig,
    GoogleDriveCleaningConfig,
    GoogleDriveConfig,
    HygieneConfig,
    LinkingConfig,
    LLMConfig,
    MarkdownCleaningConfig,
    OCRConfig,
    QualityConfig,
    RedactionConfig,
    SourcePaths,
    ValidationConfig,
    generate_default_config,
    load_config,
)

# ---------------------------------------------------------------------------
# Individual nested model defaults
# ---------------------------------------------------------------------------


class TestLLMConfig:
    """Tests for LLMConfig model."""

    def test_defaults(self) -> None:
        """LLMConfig should have sensible defaults."""
        cfg = LLMConfig()
        assert cfg.provider == "ollama"
        assert cfg.model == "mistral"
        assert cfg.ollama_url == "http://localhost:11434"
        assert cfg.batch_size == 50
        assert cfg.max_concurrent == 5

    def test_custom_values(self) -> None:
        """LLMConfig should accept custom values."""
        cfg = LLMConfig(provider="anthropic", model="claude-3", batch_size=100)
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-3"
        assert cfg.batch_size == 100


class TestEmbeddingsConfig:
    """Tests for EmbeddingsConfig model."""

    def test_defaults(self) -> None:
        """EmbeddingsConfig should have sensible defaults."""
        cfg = EmbeddingsConfig()
        assert cfg.model == "all-MiniLM-L6-v2"
        assert cfg.similarity_threshold == 0.75

    def test_custom_values(self) -> None:
        """EmbeddingsConfig should accept custom values."""
        cfg = EmbeddingsConfig(model="custom-model", similarity_threshold=0.9)
        assert cfg.model == "custom-model"
        assert cfg.similarity_threshold == 0.9


class TestOCRConfig:
    """Tests for OCRConfig model."""

    def test_defaults(self) -> None:
        """OCRConfig should have sensible defaults."""
        cfg = OCRConfig()
        assert cfg.enabled is True
        assert cfg.engine == "pytesseract"
        assert cfg.languages == ["eng"]

    def test_custom_languages(self) -> None:
        """OCRConfig should accept a custom language list."""
        cfg = OCRConfig(languages=["eng", "deu"])
        assert cfg.languages == ["eng", "deu"]


class TestLinkingConfig:
    """Tests for LinkingConfig model."""

    def test_defaults(self) -> None:
        """LinkingConfig should have sensible defaults."""
        cfg = LinkingConfig()
        assert cfg.temporal_window_hours == 168
        assert cfg.thread_min_fragments == 3
        assert cfg.eddy_min_fragments == 5

    def test_hierarchy_sibling_skip_window_default_is_two(self) -> None:
        """FEAT-024 sibling-skip window defaults to 2 positions either side."""
        cfg = LinkingConfig()
        assert cfg.hierarchy_sibling_skip_window == 2

    def test_hierarchy_sibling_skip_window_accepts_zero(self) -> None:
        """A skip window of 0 disables sibling suppression (still valid)."""
        cfg = LinkingConfig(hierarchy_sibling_skip_window=0)
        assert cfg.hierarchy_sibling_skip_window == 0

    def test_hierarchy_sibling_skip_window_rejects_negative(self) -> None:
        """Negative skip windows are nonsensical and rejected by validation."""
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            LinkingConfig(hierarchy_sibling_skip_window=-1)


class TestClassificationConfig:
    """Tests for ClassificationConfig model."""

    def test_defaults(self) -> None:
        """ClassificationConfig should have sensible defaults."""
        cfg = ClassificationConfig()
        assert cfg.confidence_threshold == 0.7
        assert cfg.auto_classify_sources == ["claude", "chatgpt", "discord"]
        assert cfg.human_review_sources == ["journal"]

    def test_reatomize_defaults_match_feat_023_spec(self) -> None:
        """FEAT-023 knobs default to disabled / inherit / 4 / auto."""
        cfg = ClassificationConfig()
        assert cfg.reatomize is False
        assert cfg.reatomize_threshold is None
        assert cfg.reatomize_max_depth == 4
        assert cfg.reatomize_direction == "auto"

    def test_reatomize_threshold_accepts_explicit_float(self) -> None:
        """An explicit reatomize_threshold survives validation."""
        cfg = ClassificationConfig(reatomize_threshold=0.5)
        assert cfg.reatomize_threshold == 0.5

    def test_reatomize_threshold_rejects_out_of_range(self) -> None:
        """The reatomize_threshold guard rails are [0.0, 1.0]."""
        with pytest.raises(ValueError, match="reatomize_threshold"):
            ClassificationConfig(reatomize_threshold=1.5)

    def test_reatomize_max_depth_must_be_positive(self) -> None:
        """``reatomize_max_depth`` rejects zero/negative values."""
        with pytest.raises(ValueError, match="reatomize_max_depth"):
            ClassificationConfig(reatomize_max_depth=0)

    def test_reatomize_direction_rejects_unknown_value(self) -> None:
        """Only ``auto`` / ``split`` / ``aggregate`` are accepted."""
        with pytest.raises(ValueError, match="reatomize_direction"):
            ClassificationConfig(reatomize_direction="sideways")


class TestRedactionConfig:
    """Tests for RedactionConfig model."""

    def test_defaults(self) -> None:
        """RedactionConfig should have sensible defaults."""
        cfg = RedactionConfig()
        assert cfg.enabled is True
        assert cfg.dry_run is False
        assert cfg.custom_patterns == {}
        assert cfg.false_positive_allowlist == []


class TestGoogleDriveConfig:
    """Tests for GoogleDriveConfig model."""

    def test_defaults(self) -> None:
        """GoogleDriveConfig should have sensible defaults."""
        cfg = GoogleDriveConfig()
        assert cfg.credentials_file == "credentials.json"
        assert cfg.token_file == "token.json"
        assert cfg.scopes == ["https://www.googleapis.com/auth/drive.readonly"]
        assert cfg.staging_dir == "google-drive-export/"

    def test_readonly_scopes_accepted(self) -> None:
        """GoogleDriveConfig should accept read-only scopes."""
        cfg = GoogleDriveConfig(
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        assert cfg.scopes == ["https://www.googleapis.com/auth/drive.readonly"]

    def test_write_scopes_rejected(self) -> None:
        """GoogleDriveConfig must reject non-read-only scopes."""
        with pytest.raises(ValueError, match="Only read-only scopes allowed"):
            GoogleDriveConfig(scopes=["https://www.googleapis.com/auth/drive"])

    def test_mixed_scopes_rejected(self) -> None:
        """GoogleDriveConfig must reject mixed scope lists."""
        with pytest.raises(ValueError, match="Only read-only scopes allowed"):
            GoogleDriveConfig(
                scopes=[
                    "https://www.googleapis.com/auth/drive.readonly",
                    "https://www.googleapis.com/auth/drive.file",
                ]
            )


class TestSourcePaths:
    """Tests for SourcePaths model."""

    def test_defaults(self) -> None:
        """SourcePaths should have sensible defaults."""
        cfg = SourcePaths()
        assert cfg.claude == "chatbot-exports/claude/"
        assert cfg.chatgpt == "chatbot-exports/chatgpt/"
        assert cfg.discord == "discord-export/"
        assert cfg.gdrive == "google-drive-export/"
        assert cfg.aptitude == "projects/aptitude/course-files/"
        assert cfg.essays == "writing/substack/"
        assert cfg.journal == "personal/journal/"
        assert cfg.code == "projects/"


# ---------------------------------------------------------------------------
# Cleaning pipeline configuration models
# ---------------------------------------------------------------------------


class TestDiscordCleaningConfig:
    """Tests for DiscordCleaningConfig model."""

    def test_defaults(self) -> None:
        """DiscordCleaningConfig should have sensible defaults."""
        cfg = DiscordCleaningConfig()
        assert cfg.filter_bot_messages is True
        assert cfg.strip_emoji is False
        assert cfg.filter_commands is True
        assert cfg.min_message_length == 10

    def test_custom_values(self) -> None:
        """DiscordCleaningConfig should accept custom values."""
        cfg = DiscordCleaningConfig(
            filter_bot_messages=False,
            strip_emoji=True,
            min_message_length=50,
        )
        assert cfg.filter_bot_messages is False
        assert cfg.strip_emoji is True
        assert cfg.min_message_length == 50


class TestChatbotCleaningConfig:
    """Tests for ChatbotCleaningConfig model."""

    def test_defaults(self) -> None:
        """ChatbotCleaningConfig should have sensible defaults."""
        cfg = ChatbotCleaningConfig()
        assert cfg.filter_system_prompts is True
        assert cfg.filter_tool_outputs is True
        assert cfg.filter_regenerations is True

    def test_custom_values(self) -> None:
        """ChatbotCleaningConfig should accept custom values."""
        cfg = ChatbotCleaningConfig(filter_system_prompts=False)
        assert cfg.filter_system_prompts is False


class TestMarkdownCleaningConfig:
    """Tests for MarkdownCleaningConfig model."""

    def test_defaults(self) -> None:
        """MarkdownCleaningConfig should have sensible defaults."""
        cfg = MarkdownCleaningConfig()
        assert cfg.skip_empty_files is True
        assert cfg.min_body_length == 50

    def test_custom_values(self) -> None:
        """MarkdownCleaningConfig should accept custom values."""
        cfg = MarkdownCleaningConfig(min_body_length=100)
        assert cfg.min_body_length == 100


class TestGoogleDriveCleaningConfig:
    """Tests for GoogleDriveCleaningConfig model."""

    def test_defaults(self) -> None:
        """GoogleDriveCleaningConfig should have sensible defaults."""
        cfg = GoogleDriveCleaningConfig()
        assert cfg.deduplicate is True
        assert cfg.filter_empty_docs is True
        assert cfg.max_collaboration_ratio == 0.9

    def test_custom_values(self) -> None:
        """GoogleDriveCleaningConfig should accept custom values."""
        cfg = GoogleDriveCleaningConfig(max_collaboration_ratio=0.5)
        assert cfg.max_collaboration_ratio == 0.5


class TestValidationConfig:
    """Tests for ValidationConfig model."""

    def test_defaults(self) -> None:
        """ValidationConfig should have sensible defaults."""
        cfg = ValidationConfig()
        assert cfg.min_characters == 20
        assert cfg.min_words == 5
        assert cfg.max_stop_word_ratio == 0.8
        assert cfg.require_metadata is True

    def test_custom_values(self) -> None:
        """ValidationConfig should accept custom values."""
        cfg = ValidationConfig(min_characters=50, min_words=10)
        assert cfg.min_characters == 50
        assert cfg.min_words == 10


class TestQualityConfig:
    """Tests for QualityConfig model."""

    def test_defaults(self) -> None:
        """QualityConfig should have sensible defaults."""
        cfg = QualityConfig()
        assert cfg.accept_threshold == 0.7
        assert cfg.skip_threshold == 0.3

    def test_custom_values(self) -> None:
        """QualityConfig should accept custom values."""
        cfg = QualityConfig(accept_threshold=0.8, skip_threshold=0.2)
        assert cfg.accept_threshold == 0.8
        assert cfg.skip_threshold == 0.2


class TestDeduplicationConfig:
    """Tests for DeduplicationConfig model."""

    def test_defaults(self) -> None:
        """DeduplicationConfig should have sensible defaults."""
        cfg = DeduplicationConfig()
        assert cfg.strategy == "fuzzy"
        assert cfg.similarity_threshold == 0.85

    def test_custom_values(self) -> None:
        """DeduplicationConfig should accept custom values."""
        cfg = DeduplicationConfig(strategy="exact", similarity_threshold=1.0)
        assert cfg.strategy == "exact"
        assert cfg.similarity_threshold == 1.0


class TestHygieneConfig:
    """Tests for HygieneConfig model."""

    def test_defaults(self) -> None:
        """HygieneConfig should have sensible defaults."""
        cfg = HygieneConfig()
        assert cfg.track_orphans is True
        assert cfg.staleness_days == 90

    def test_custom_values(self) -> None:
        """HygieneConfig should accept custom values."""
        cfg = HygieneConfig(track_orphans=False, staleness_days=30)
        assert cfg.track_orphans is False
        assert cfg.staleness_days == 30


class TestCleaningConfig:
    """Tests for top-level CleaningConfig model."""

    def test_defaults(self) -> None:
        """CleaningConfig should compose all sub-configs with defaults."""
        cfg = CleaningConfig()
        assert isinstance(cfg.discord, DiscordCleaningConfig)
        assert isinstance(cfg.chatbot, ChatbotCleaningConfig)
        assert isinstance(cfg.markdown, MarkdownCleaningConfig)
        assert isinstance(cfg.google_drive, GoogleDriveCleaningConfig)
        assert isinstance(cfg.validation, ValidationConfig)
        assert isinstance(cfg.quality, QualityConfig)
        assert isinstance(cfg.deduplication, DeduplicationConfig)
        assert isinstance(cfg.hygiene, HygieneConfig)

    def test_partial_override(self) -> None:
        """CleaningConfig should accept partial overrides."""
        cfg = CleaningConfig(
            discord=DiscordCleaningConfig(min_message_length=25),
            quality=QualityConfig(accept_threshold=0.9),
        )
        assert cfg.discord.min_message_length == 25
        assert cfg.quality.accept_threshold == 0.9
        # Other sub-configs keep defaults
        assert cfg.chatbot.filter_system_prompts is True


# ---------------------------------------------------------------------------
# Top-level CreekConfig
# ---------------------------------------------------------------------------


class TestCreekConfig:
    """Tests for CreekConfig top-level settings model."""

    def test_all_defaults_valid(self) -> None:
        """CreekConfig() with all defaults should produce a valid config."""
        cfg = CreekConfig()
        assert cfg.vault_path == Path(".")
        assert cfg.source_drive == Path(".")
        assert cfg.timezone == "America/Los_Angeles"
        # Nested models should exist with their own defaults
        assert isinstance(cfg.llm, LLMConfig)
        assert isinstance(cfg.embeddings, EmbeddingsConfig)
        assert isinstance(cfg.ocr, OCRConfig)
        assert isinstance(cfg.linking, LinkingConfig)
        assert isinstance(cfg.classification, ClassificationConfig)
        assert isinstance(cfg.redaction, RedactionConfig)
        assert isinstance(cfg.google_drive, GoogleDriveConfig)
        assert isinstance(cfg.sources, SourcePaths)
        assert isinstance(cfg.cleaning, CleaningConfig)

    def test_valid_timezone(self) -> None:
        """CreekConfig should accept a valid timezone string."""
        cfg = CreekConfig(timezone="Europe/London")
        assert cfg.timezone == "Europe/London"

    def test_invalid_timezone_rejected(self) -> None:
        """CreekConfig must reject an invalid timezone string."""
        with pytest.raises(ValueError, match="Invalid timezone"):
            CreekConfig(timezone="Not/A/Timezone")

    def test_env_var_override_vault_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CREEK_VAULT_PATH env var should override the default."""
        monkeypatch.setenv("CREEK_VAULT_PATH", "/tmp/my-vault")  # nosec B108
        cfg = CreekConfig()
        assert cfg.vault_path == Path("/tmp/my-vault")  # nosec B108

    def test_env_var_override_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CREEK_TIMEZONE env var should override the default."""
        monkeypatch.setenv("CREEK_TIMEZONE", "UTC")
        cfg = CreekConfig()
        assert cfg.timezone == "UTC"

    def test_env_var_override_source_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREEK_SOURCE_DRIVE env var should override source_drive."""
        monkeypatch.setenv("CREEK_SOURCE_DRIVE", "/mnt/data")
        cfg = CreekConfig()
        assert cfg.source_drive == Path("/mnt/data")


# ---------------------------------------------------------------------------
# load_config()
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for load_config function."""

    def test_no_file_returns_defaults(self, tmp_path: Path) -> None:
        """load_config() should return defaults when YAML file does not exist."""
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg.vault_path == Path(".")
        assert cfg.timezone == "America/Los_Angeles"

    def test_loads_yaml_file(self, tmp_path: Path) -> None:
        """load_config() should load values from a YAML file."""
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "vault_path": "/home/user/vault",
            "timezone": "America/New_York",
            "llm": {"provider": "anthropic", "model": "claude-3"},
            "embeddings": {"similarity_threshold": 0.85},
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.vault_path == Path("/home/user/vault")
        assert cfg.timezone == "America/New_York"
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model == "claude-3"
        assert cfg.embeddings.similarity_threshold == 0.85
        # Unspecified fields keep defaults
        assert cfg.llm.batch_size == 50

    def test_loads_empty_yaml(self, tmp_path: Path) -> None:
        """load_config() should handle an empty YAML file gracefully."""
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text("")

        cfg = load_config(config_file)
        assert cfg.vault_path == Path(".")

    def test_partial_nested_config(self, tmp_path: Path) -> None:
        """load_config() should merge partial nested config with defaults."""
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "ocr": {"enabled": False},
            "linking": {"temporal_window_hours": 48},
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.ocr.enabled is False
        assert cfg.ocr.engine == "pytesseract"  # default preserved
        assert cfg.linking.temporal_window_hours == 48
        assert cfg.linking.thread_min_fragments == 3  # default preserved

    def test_loads_cleaning_section(self, tmp_path: Path) -> None:
        """load_config() should load cleaning section from YAML."""
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "cleaning": {
                "discord": {"min_message_length": 25},
                "quality": {"accept_threshold": 0.9},
                "deduplication": {"strategy": "exact"},
            },
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.cleaning.discord.min_message_length == 25
        assert cfg.cleaning.quality.accept_threshold == 0.9
        assert cfg.cleaning.deduplication.strategy == "exact"
        # Unspecified sub-configs keep defaults
        assert cfg.cleaning.chatbot.filter_system_prompts is True
        assert cfg.cleaning.hygiene.staleness_days == 90


# ---------------------------------------------------------------------------
# generate_default_config()
# ---------------------------------------------------------------------------


class TestGenerateDefaultConfig:
    """Tests for generate_default_config function."""

    def test_writes_valid_yaml(self, tmp_path: Path) -> None:
        """generate_default_config() should write valid YAML."""
        output = tmp_path / "creek_config.yaml"
        generate_default_config(output)
        assert output.exists()

        with output.open() as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "vault_path" in data
        assert "timezone" in data

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Generated config should round-trip back through load_config."""
        output = tmp_path / "creek_config.yaml"
        generate_default_config(output)

        cfg = load_config(output)
        assert cfg.vault_path == Path(".")
        assert cfg.timezone == "America/Los_Angeles"
        assert cfg.llm.provider == "ollama"
        assert cfg.embeddings.model == "all-MiniLM-L6-v2"
        assert cfg.google_drive.scopes == [
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        # Cleaning section should round-trip with defaults
        assert isinstance(cfg.cleaning, CleaningConfig)
        assert cfg.cleaning.discord.filter_bot_messages is True
        assert cfg.cleaning.quality.accept_threshold == 0.7
        assert cfg.cleaning.deduplication.strategy == "fuzzy"

    def test_generated_config_contains_cleaning(self, tmp_path: Path) -> None:
        """Generated YAML should include the cleaning section."""
        output = tmp_path / "creek_config.yaml"
        generate_default_config(output)

        with output.open() as f:
            data = yaml.safe_load(f)
        assert "cleaning" in data
        assert "discord" in data["cleaning"]
        assert "quality" in data["cleaning"]
