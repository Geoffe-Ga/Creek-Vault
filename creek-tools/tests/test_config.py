"""Tests for creek.config module — configuration loader with Pydantic Settings."""

from pathlib import Path

import pytest
import yaml

from creek.config import (
    CONFIG_PATH_ENV_VAR,
    AIStyleConfig,
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
    LLMRoutingConfig,
    MarkdownCleaningConfig,
    OCRConfig,
    QualityConfig,
    RedactionConfig,
    SourcePaths,
    ValidationConfig,
    VoiceAudienceWeightingConfig,
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
        assert cfg.model is None
        assert cfg.ollama_url == "http://localhost:11434"
        assert cfg.batch_size == 50
        assert cfg.max_concurrent == 5

    def test_custom_values(self) -> None:
        """LLMConfig should accept custom values."""
        cfg = LLMConfig(provider="anthropic", model="claude-3", batch_size=100)
        assert cfg.provider == "anthropic"
        assert cfg.model == "claude-3"
        assert cfg.batch_size == 100

    def test_max_concurrent_rejects_zero(self) -> None:
        """Zero concurrent requests would issue no LLM calls at all."""
        with pytest.raises(
            ValueError, match=r"max_concurrent[\s\S]*greater than or equal to 1"
        ):
            LLMConfig(max_concurrent=0)

    def test_max_concurrent_rejects_negative(self) -> None:
        """A negative concurrency limit is nonsensical, not just unhelpful."""
        with pytest.raises(
            ValueError, match=r"max_concurrent[\s\S]*greater than or equal to 1"
        ):
            LLMConfig(max_concurrent=-3)

    def test_max_concurrent_allows_one(self) -> None:
        """1 is the inclusive lower bound — serial LLM operation is legal."""
        cfg = LLMConfig(max_concurrent=1)
        assert cfg.max_concurrent == 1

    def test_batch_size_rejects_zero(self) -> None:
        """A zero batch would feed ``encode(batch_size=0)`` and process nothing."""
        with pytest.raises(
            ValueError, match=r"batch_size[\s\S]*greater than or equal to 1"
        ):
            LLMConfig(batch_size=0)

    def test_batch_size_rejects_negative(self) -> None:
        """A negative batch size is nonsensical, not just unhelpful."""
        with pytest.raises(
            ValueError, match=r"batch_size[\s\S]*greater than or equal to 1"
        ):
            LLMConfig(batch_size=-10)

    def test_batch_size_allows_one(self) -> None:
        """1 is the inclusive lower bound — one item per batch is legal."""
        cfg = LLMConfig(batch_size=1)
        assert cfg.batch_size == 1


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

    def test_cross_source_aggregation_default_is_false(self) -> None:
        """FEAT-027: cross-source aggregation is opt-in (default False)."""
        cfg = LinkingConfig()
        assert cfg.cross_source_aggregation is False

    def test_cross_source_aggregation_accepts_true(self) -> None:
        """FEAT-027: operators can flip cross-source aggregation on."""
        cfg = LinkingConfig(cross_source_aggregation=True)
        assert cfg.cross_source_aggregation is True

    def test_detector_threshold_defaults_match_previous_constants(self) -> None:
        """Issue #880: the five formerly-hardcoded knobs keep their values.

        These thresholds lived as module-private constants in
        ``creek.link.eddies`` and ``creek.link.threads``. Exposing them must
        not change what an existing vault — whose ``creek_config.yaml``
        predates the keys entirely — computes.
        """
        cfg = LinkingConfig()
        assert cfg.eddy_eps == 0.3
        assert cfg.eddy_min_samples == 5
        assert cfg.eddy_correlation_threshold == 0.3
        assert cfg.thread_window_days == 30
        assert cfg.thread_similarity_threshold == 0.6

    def test_cluster_limit_defaults(self) -> None:
        """Issue #880: cluster-ceiling defaults never fire on ordinary vaults."""
        cfg = LinkingConfig()
        assert cfg.cluster_size_ceiling == 500
        assert cfg.cluster_max_fraction == 0.10
        assert cfg.cluster_split_max_depth == 3
        assert cfg.eddy_split_eps_step == 0.05
        assert cfg.thread_split_similarity_step == 0.1

    def test_segmentation_defaults(self) -> None:
        """Issue #880: only Discord and email are segmented into episodes."""
        cfg = LinkingConfig()
        assert cfg.stream_platforms == ["discord", "email"]
        assert cfg.stream_episode_max_gap_hours == 24
        assert cfg.stream_episode_max_span_days == 30

    def test_thread_union_without_embeddings_defaults_to_closed(self) -> None:
        """Issue #880: a partially-embedded vault must not union on frequency."""
        cfg = LinkingConfig()
        assert cfg.thread_union_without_embeddings is False

    def test_cluster_max_fraction_accepts_the_documented_opt_out(self) -> None:
        """``1.0`` disables the ceiling — a cluster may span the whole corpus."""
        cfg = LinkingConfig(cluster_max_fraction=1.0)
        assert cfg.cluster_max_fraction == 1.0

    def test_cluster_max_fraction_rejects_zero(self) -> None:
        """A zero fraction would make every cluster oversized."""
        with pytest.raises(ValueError, match="greater than 0"):
            LinkingConfig(cluster_max_fraction=0.0)

    def test_cluster_split_max_depth_accepts_zero(self) -> None:
        """Depth 0 disables splitting (oversized clusters go straight to noise)."""
        cfg = LinkingConfig(cluster_split_max_depth=0)
        assert cfg.cluster_split_max_depth == 0

    def test_cluster_size_ceiling_rejects_zero(self) -> None:
        """A ceiling below one fragment is nonsensical."""
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            LinkingConfig(cluster_size_ceiling=0)

    def test_eddy_split_eps_step_rejects_one(self) -> None:
        """A full-width step would drive epsilon past its valid range at once."""
        with pytest.raises(ValueError, match="less than 1"):
            LinkingConfig(eddy_split_eps_step=1.0)

    def test_stream_episode_max_gap_hours_rejects_zero(self) -> None:
        """A zero-hour gap would cut an episode at every message."""
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            LinkingConfig(stream_episode_max_gap_hours=0)

    def test_stream_platforms_accepts_a_known_platform(self) -> None:
        """Operators may opt extra conversational platforms into segmentation."""
        cfg = LinkingConfig(stream_platforms=["discord", "chatgpt"])
        assert cfg.stream_platforms == ["discord", "chatgpt"]

    def test_stream_platforms_rejects_an_unknown_platform(self) -> None:
        """A typo must fail loudly rather than silently segmenting nothing."""
        with pytest.raises(ValueError, match="unknown source platform"):
            LinkingConfig(stream_platforms=["discrod"])

    def test_stream_platforms_accepts_an_empty_list(self) -> None:
        """An empty list is the documented way to disable segmentation."""
        cfg = LinkingConfig(stream_platforms=[])
        assert cfg.stream_platforms == []


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
# MiningConfig (issue #340)
# ---------------------------------------------------------------------------


class TestMiningConfig:
    """Tests for ``MiningConfig`` — issue #340 expose mining knobs to YAML."""

    def test_defaults_match_library(self) -> None:
        """Mining defaults mirror the constants in ``creek.generate.mining``."""
        from creek.config import MiningConfig
        from creek.generate.mining import (
            DEFAULT_MIN_CHAIN_LENGTH,
            DEFAULT_MIN_THREAD_FRAGMENTS,
            DEFAULT_SIMILARITY_LIMINAL,
            DEFAULT_SIMILARITY_RESONANCE,
        )

        cfg = MiningConfig()
        assert cfg.min_thread_fragments == DEFAULT_MIN_THREAD_FRAGMENTS
        assert cfg.min_chain_length == DEFAULT_MIN_CHAIN_LENGTH
        assert cfg.similarity_liminal == pytest.approx(DEFAULT_SIMILARITY_LIMINAL)
        assert cfg.similarity_resonance == pytest.approx(DEFAULT_SIMILARITY_RESONANCE)

    def test_custom_values(self) -> None:
        """``MiningConfig`` accepts overrides for every knob."""
        from creek.config import MiningConfig

        cfg = MiningConfig(
            min_thread_fragments=4,
            min_chain_length=2,
            similarity_liminal=0.2,
            similarity_resonance=0.5,
        )
        assert cfg.min_thread_fragments == 4
        assert cfg.min_chain_length == 2
        assert cfg.similarity_liminal == pytest.approx(0.2)
        assert cfg.similarity_resonance == pytest.approx(0.5)

    def test_similarity_floors_reject_out_of_range(self) -> None:
        """Cosine-like similarities must live in ``[0, 1]``."""
        from creek.config import MiningConfig

        with pytest.raises(ValueError, match="less than or equal to 1"):
            MiningConfig(similarity_liminal=1.5)
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            MiningConfig(similarity_resonance=-0.1)

    def test_min_thread_fragments_must_be_positive(self) -> None:
        """A non-positive thread floor would surface every active thread."""
        from creek.config import MiningConfig

        with pytest.raises(ValueError, match="greater than or equal to 1"):
            MiningConfig(min_thread_fragments=0)

    def test_min_chain_length_must_be_at_least_two(self) -> None:
        """Chains of <2 fragments aren't chains — they're singletons."""
        from creek.config import MiningConfig

        with pytest.raises(ValueError, match="greater than or equal to 2"):
            MiningConfig(min_chain_length=1)


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
        # Nested models should exist with their own defaults. ``llm`` is now a
        # per-stage LLMRoutingConfig (#646) whose ``default`` is an LLMConfig.
        assert isinstance(cfg.llm, LLMRoutingConfig)
        assert isinstance(cfg.llm.default, LLMConfig)
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
        assert cfg.llm.default.provider == "anthropic"
        assert cfg.llm.default.model == "claude-3"
        assert cfg.embeddings.similarity_threshold == 0.85
        # Unspecified fields keep defaults
        assert cfg.llm.default.batch_size == 50

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

    def test_loads_cluster_ceiling_override_keeping_sibling_defaults(
        self,
        tmp_path: Path,
    ) -> None:
        """Issue #880: one override applies; unset siblings keep their defaults.

        This is the behaviour-preservation guard for vaults whose
        ``creek_config.yaml`` predates the #880 keys: an operator who tunes a
        single knob must not silently re-tune the other twelve.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "linking": {
                "cluster_max_fraction": 0.05,
                "stream_platforms": ["discord"],
            },
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.linking.cluster_max_fraction == 0.05
        assert cfg.linking.stream_platforms == ["discord"]
        assert cfg.linking.cluster_size_ceiling == 500
        assert cfg.linking.eddy_eps == 0.3
        assert cfg.linking.thread_window_days == 30
        assert cfg.linking.stream_episode_max_gap_hours == 24

    def test_loads_cross_source_aggregation_flag(self, tmp_path: Path) -> None:
        """YAML ``linking.cross_source_aggregation: true`` is honoured."""
        config_file = tmp_path / "creek_config.yaml"
        config_data = {"linking": {"cross_source_aggregation": True}}
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.linking.cross_source_aggregation is True

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


class TestLoadConfigEnvVar:
    """Tests for the ``CREEK_CONFIG`` env-var discovery path (INC-008)."""

    def test_uses_creek_config_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``CREEK_CONFIG`` is honoured when ``config_path`` is None."""
        config_file = tmp_path / "vault-config.yaml"
        config_file.write_text(yaml.dump({"timezone": "UTC"}))
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config_file))
        # Ensure cwd has no fallback creek_config.yaml that could mask the env var.
        monkeypatch.chdir(tmp_path)

        cfg = load_config()
        assert cfg.timezone == "UTC"

    def test_explicit_config_path_overrides_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An explicit ``config_path`` argument wins over the env var."""
        env_file = tmp_path / "from-env.yaml"
        env_file.write_text(yaml.dump({"timezone": "UTC"}))
        cli_file = tmp_path / "from-cli.yaml"
        cli_file.write_text(yaml.dump({"timezone": "Europe/London"}))
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(env_file))

        cfg = load_config(cli_file)
        assert cfg.timezone == "Europe/London"

    def test_env_var_pointing_to_missing_file_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A missing ``CREEK_CONFIG`` target raises rather than falling back."""
        missing = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(missing))

        with pytest.raises(FileNotFoundError, match=CONFIG_PATH_ENV_VAR):
            load_config()

    def test_empty_env_var_falls_through_to_cwd_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An empty (or whitespace) env var behaves as if unset."""
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, "   ")
        monkeypatch.chdir(tmp_path)

        cfg = load_config()
        # No creek_config.yaml in tmp_path → defaults populated.
        assert cfg.timezone == "America/Los_Angeles"

    def test_unset_env_var_uses_cwd_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the env var is unset, behaviour matches the historical contract."""
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(yaml.dump({"timezone": "Asia/Tokyo"}))
        monkeypatch.chdir(tmp_path)

        cfg = load_config()
        assert cfg.timezone == "Asia/Tokyo"


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
        assert cfg.llm.default.provider == "ollama"
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

    def test_generated_config_mentions_anthropic_consent_env(
        self, tmp_path: Path
    ) -> None:
        """Issue #320: the starter config must mention CREEK_ANTHROPIC_CONSENT.

        First-time users who switch ``llm.provider`` to ``anthropic`` should
        discover the consent-env-var requirement from the config file itself
        rather than from a runtime failure mid-classify-run.
        """
        output = tmp_path / "creek_config.yaml"
        generate_default_config(output)

        text = output.read_text(encoding="utf-8")
        # The note lives as a YAML comment near the llm: block, so it must
        # survive a round-trip parse without changing the data shape.
        assert "CREEK_ANTHROPIC_CONSENT" in text
        assert "ANTHROPIC_API_KEY" in text
        assert "anthropic" in text.lower()

        # The note is purely a comment — it must not introduce new keys
        # into the parsed config.
        with output.open() as f:
            data = yaml.safe_load(f)
        assert "vault_path" in data
        assert "llm" in data

    def test_generated_config_round_trips_with_consent_note(
        self, tmp_path: Path
    ) -> None:
        """The added consent comment must not break load_config round-trips."""
        output = tmp_path / "creek_config.yaml"
        generate_default_config(output)

        cfg = load_config(output)
        # The comment is informational — provider default still ollama.
        assert cfg.llm.default.provider == "ollama"


class TestVoiceConfigRoundtrip:
    """Round-trip the epic-551 voice config knobs through YAML."""

    def test_voice_config_roundtrips_through_yaml(self, tmp_path: Path) -> None:
        """Audience weighting + voice-distance target survive dump → load."""
        original = CreekConfig(
            vault_path=tmp_path / "vault",
            voice_audience_weighting=VoiceAudienceWeightingConfig(
                enabled=True,
                privacy_tier_authority={"open": 1.5, "personal": 1.0, "intimate": 0.0},
                representativeness_authority={"self": 1.0, "reference": 0.3},
            ),
            ai_style=AIStyleConfig(
                voice_distance_upper=0.4,
                voice_distance_target=0.2,
            ),
        )
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            yaml.safe_dump(original.model_dump(mode="json")),
            encoding="utf-8",
        )

        loaded = load_config(config_file)

        weighting = loaded.voice_audience_weighting
        assert weighting.enabled is True
        assert weighting.privacy_tier_authority["open"] == 1.5
        assert weighting.privacy_tier_authority["intimate"] == 0.0
        assert weighting.representativeness_authority["reference"] == 0.3
        assert loaded.ai_style.voice_distance_upper == 0.4
        assert loaded.ai_style.voice_distance_target == 0.2

    def test_default_voice_config_has_validated_defaults(self) -> None:
        """The default config exposes sane, validated voice knobs."""
        config = CreekConfig()
        assert config.voice_audience_weighting.enabled is True
        # Target never exceeds the accepted-divergence ceiling.
        assert (
            config.ai_style.voice_distance_target
            <= config.ai_style.voice_distance_upper
        )
