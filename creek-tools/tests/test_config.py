"""Tests for creek.config module — configuration loader with Pydantic Settings."""

from pathlib import Path

import pytest
import yaml

from creek.config import (
    ClassificationConfig,
    CreekConfig,
    EmbeddingsConfig,
    GoogleDriveConfig,
    LinkingConfig,
    LLMConfig,
    OCRConfig,
    RedactionConfig,
    SourcePaths,
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


class TestClassificationConfig:
    """Tests for ClassificationConfig model."""

    def test_defaults(self) -> None:
        """ClassificationConfig should have sensible defaults."""
        cfg = ClassificationConfig()
        assert cfg.confidence_threshold == 0.7
        assert cfg.auto_classify_sources == ["claude", "chatgpt", "discord"]
        assert cfg.human_review_sources == ["journal"]


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
        monkeypatch.setenv("CREEK_VAULT_PATH", "/tmp/my-vault")
        cfg = CreekConfig()
        assert cfg.vault_path == Path("/tmp/my-vault")

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
            "timezone": "US/Eastern",
            "llm": {"provider": "anthropic", "model": "claude-3"},
            "embeddings": {"similarity_threshold": 0.85},
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.vault_path == Path("/home/user/vault")
        assert cfg.timezone == "US/Eastern"
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
