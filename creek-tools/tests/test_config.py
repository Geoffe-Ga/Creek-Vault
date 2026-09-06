"""Tests for creek.config module — configuration loader with Pydantic Settings."""

import inspect
import itertools
import logging
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Final, get_args

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from creek.clean.filters.chatbot import ChatbotFilterConfig
from creek.clean.filters.discord import DiscordFilterConfig
from creek.clean.filters.google_drive import GoogleDriveFilter
from creek.clean.filters.markdown import MarkdownFilter
from creek.clean.hygiene import OrphanScanner, StaleReviewScanner
from creek.clean.quality import QualityScorer
from creek.clean.validator import FragmentValidator
from creek.config import (
    CONFIG_PATH_ENV_VAR,
    AIStyleConfig,
    AuthorConfig,
    ClassificationConfig,
    CleaningConfig,
    CreekConfig,
    DedupCleaningConfig,
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
    _default_authorship_weights,
    generate_default_config,
    load_config,
)
from creek.models import PrivacyTier

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
        """Only ``auto`` / ``split`` are accepted."""
        with pytest.raises(ValueError, match="reatomize_direction"):
            ClassificationConfig(reatomize_direction="sideways")

    def test_reatomize_direction_rejects_retired_aggregate_value(self) -> None:
        """``aggregate`` is refused outright, never silently rewritten (#1342).

        The FEAT-022 zoom-out aggregator had no production caller, so
        ``reatomize_direction: aggregate`` in a vault's YAML did nothing
        at all. ADR-0011 retires the operator. Coercing the stale value
        to ``auto`` would preserve the original sin — the config would
        keep claiming a behaviour it does not have — so the loader must
        raise, and the message must name the retirement and the issue.
        """
        with pytest.raises(ValueError, match=r"(?is)retired.*1342|1342.*retired"):
            ClassificationConfig(reatomize_direction="aggregate")


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


class TestDiscordFilterConfig:
    """Tests for the ``cleaning.discord`` model (``DiscordFilterConfig``).

    Renamed from ``TestDiscordCleaningConfig`` by #1519, which collapsed the
    config-side twin onto the class the filter actually runs on. Every field
    the old twin had is still asserted, under the surviving name.
    """

    def test_defaults(self) -> None:
        """``DiscordFilterConfig`` should have sensible defaults."""
        cfg = DiscordFilterConfig()
        assert cfg.skip_bots is True
        assert cfg.strip_emoji is False
        assert cfg.skip_commands is True
        assert cfg.min_length == 3
        assert cfg.skip_emoji_only is True
        assert cfg.skip_media_only is True
        assert cfg.skip_below_min_length is True
        assert cfg.flag_link_dumps is True
        assert cfg.command_prefixes == ["/", "!", "."]

    def test_custom_values(self) -> None:
        """``DiscordFilterConfig`` should accept custom values."""
        cfg = DiscordFilterConfig(
            skip_bots=False,
            strip_emoji=True,
            min_length=50,
            command_prefixes=["?"],
        )
        assert cfg.skip_bots is False
        assert cfg.strip_emoji is True
        assert cfg.min_length == 50
        assert cfg.command_prefixes == ["?"]

    def test_strip_emoji_is_not_skip_emoji_only(self) -> None:
        """The two emoji knobs are separate operations with opposite defaults.

        #1519 kept both rather than merging them: ``skip_emoji_only`` drops a
        message that is nothing but emoji, while ``strip_emoji`` would edit a
        message Creek keeps.
        """
        cfg = DiscordFilterConfig()
        assert cfg.strip_emoji is not cfg.skip_emoji_only


class TestChatbotFilterConfig:
    """Tests for the ``cleaning.chatbot`` model (``ChatbotFilterConfig``).

    Renamed from ``TestChatbotCleaningConfig`` by #1519.
    """

    def test_defaults(self) -> None:
        """``ChatbotFilterConfig`` should have sensible defaults."""
        cfg = ChatbotFilterConfig()
        assert cfg.skip_system_prompts is True
        assert cfg.skip_tool_outputs is True
        assert cfg.collapse_regenerations is True
        assert cfg.min_human_turn_length == 20
        assert cfg.code_block_threshold == 0.9
        assert cfg.max_abandoned_turns == 2

    def test_custom_values(self) -> None:
        """``ChatbotFilterConfig`` should accept custom values."""
        cfg = ChatbotFilterConfig(skip_system_prompts=False)
        assert cfg.skip_system_prompts is False

    def test_bounds_are_enforced(self) -> None:
        """The surviving class carries bounds the config twin lacked.

        A tightening, not a loosening: values the deleted
        ``ChatbotCleaningConfig`` would have accepted silently are now
        rejected loudly.
        """
        with pytest.raises(ValidationError):
            ChatbotFilterConfig(code_block_threshold=1.5)
        with pytest.raises(ValidationError):
            ChatbotFilterConfig(min_human_turn_length=-1)


class TestMarkdownCleaningConfig:
    """Tests for MarkdownCleaningConfig model."""

    def test_defaults(self) -> None:
        """MarkdownCleaningConfig should have sensible defaults."""
        cfg = MarkdownCleaningConfig()
        assert cfg.skip_empty_files is True
        assert cfg.min_body_length == 10

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
        assert cfg.multi_author_threshold == 0.5

    def test_custom_values(self) -> None:
        """GoogleDriveCleaningConfig should accept custom values."""
        cfg = GoogleDriveCleaningConfig(multi_author_threshold=0.8)
        assert cfg.multi_author_threshold == 0.8


class TestValidationConfig:
    """Tests for ValidationConfig model.

    #1519 moved ``min_words`` and ``max_stop_word_ratio`` out of this block:
    ``FragmentValidator`` runs no word-count and no stop-word check, so the
    two knobs were describing ``QualityScorer``, in a different block. They
    are asserted by :class:`TestQualityConfig` below.
    """

    def test_defaults(self) -> None:
        """ValidationConfig should have sensible defaults."""
        cfg = ValidationConfig()
        assert cfg.min_content_length == 20
        assert cfg.require_metadata is True

    def test_custom_values(self) -> None:
        """ValidationConfig should accept custom values."""
        cfg = ValidationConfig(min_content_length=50, require_metadata=False)
        assert cfg.min_content_length == 50
        assert cfg.require_metadata is False

    def test_the_relocated_knobs_are_gone_from_this_block(self) -> None:
        """The two knobs #1519 relocated must not linger here as well."""
        fields = set(ValidationConfig.model_fields)
        assert "min_words" not in fields
        assert "max_stop_word_ratio" not in fields


class TestQualityConfig:
    """Tests for QualityConfig model."""

    def test_defaults(self) -> None:
        """QualityConfig should have sensible defaults."""
        cfg = QualityConfig()
        assert cfg.accept_threshold == 0.6
        assert cfg.review_threshold == 0.3
        assert cfg.min_words == 10
        assert cfg.stop_word_threshold == 0.7

    def test_custom_values(self) -> None:
        """QualityConfig should accept custom values."""
        cfg = QualityConfig(
            accept_threshold=0.8,
            review_threshold=0.2,
            min_words=3,
            stop_word_threshold=0.5,
        )
        assert cfg.accept_threshold == 0.8
        assert cfg.review_threshold == 0.2
        assert cfg.min_words == 3
        assert cfg.stop_word_threshold == 0.5


class TestDedupCleaningConfig:
    """Tests for DedupCleaningConfig model.

    Renamed from ``DeduplicationConfig`` by #1519 because
    ``creek.clean.semantic_dedup`` defines a class of that name with a
    disjoint field set. Both YAML leaf paths are unchanged.
    """

    def test_defaults(self) -> None:
        """DedupCleaningConfig should have sensible defaults."""
        cfg = DedupCleaningConfig()
        assert cfg.strategy == "fuzzy"
        assert cfg.similarity_threshold == 0.85

    def test_custom_values(self) -> None:
        """DedupCleaningConfig should accept custom values."""
        cfg = DedupCleaningConfig(strategy="exact", similarity_threshold=1.0)
        assert cfg.strategy == "exact"
        assert cfg.similarity_threshold == 1.0

    def test_it_is_not_the_semantic_deduplicator_model(self) -> None:
        """The rename must leave two distinct classes, not one merged model.

        They describe different subsystems that collided on a name: this one
        configures the hash-based ``Deduplicator``, the other cosine
        thresholds over embeddings.
        """
        from creek.clean.semantic_dedup import (
            DeduplicationConfig as SemanticDeduplicationConfig,
        )

        assert DedupCleaningConfig is not SemanticDeduplicationConfig
        assert set(DedupCleaningConfig.model_fields).isdisjoint(
            SemanticDeduplicationConfig.model_fields,
        )


class TestHygieneConfig:
    """Tests for HygieneConfig model.

    #1519 split ``staleness_days`` (one knob at 90) into the two values the
    two scanners actually run at.
    """

    def test_defaults(self) -> None:
        """HygieneConfig should have sensible defaults."""
        cfg = HygieneConfig()
        assert cfg.track_orphans is True
        assert cfg.orphan_age_days == 30
        assert cfg.stale_review_days == 14

    def test_custom_values(self) -> None:
        """HygieneConfig should accept custom values."""
        cfg = HygieneConfig(
            track_orphans=False,
            orphan_age_days=7,
            stale_review_days=3,
        )
        assert cfg.track_orphans is False
        assert cfg.orphan_age_days == 7
        assert cfg.stale_review_days == 3


class TestCleaningConfig:
    """Tests for top-level CleaningConfig model."""

    def test_defaults(self) -> None:
        """CleaningConfig should compose all sub-configs with defaults."""
        cfg = CleaningConfig()
        assert isinstance(cfg.discord, DiscordFilterConfig)
        assert isinstance(cfg.chatbot, ChatbotFilterConfig)
        assert isinstance(cfg.markdown, MarkdownCleaningConfig)
        assert isinstance(cfg.google_drive, GoogleDriveCleaningConfig)
        assert isinstance(cfg.validation, ValidationConfig)
        assert isinstance(cfg.quality, QualityConfig)
        assert isinstance(cfg.deduplication, DedupCleaningConfig)
        assert isinstance(cfg.hygiene, HygieneConfig)

    def test_partial_override(self) -> None:
        """CleaningConfig should accept partial overrides."""
        cfg = CleaningConfig(
            discord=DiscordFilterConfig(min_length=25),
            quality=QualityConfig(accept_threshold=0.9),
        )
        assert cfg.discord.min_length == 25
        assert cfg.quality.accept_threshold == 0.9
        # Other sub-configs keep defaults
        assert cfg.chatbot.skip_system_prompts is True


class TestLegacyCleaningKeys:
    """The pre-#1519 cleaning keys migrate rather than vanish.

    Every ``creek_config.yaml`` ``creek init`` has ever written carries all
    of this block's old key names *and* old values, because
    ``generate_default_config`` dumps the whole model with no hand-written
    key list. The shim therefore has to do two different things: move a key
    an operator deliberately set, and *drop* one that merely carries the old
    default — because re-installing that value would revert the drift
    resolution on every existing vault.
    """

    def test_a_deliberate_legacy_value_is_carried_to_the_new_name(self) -> None:
        """A value that is not the old default was typed on purpose."""
        cfg = CleaningConfig.model_validate(
            {"discord": {"min_message_length": 25}},
        )
        assert cfg.discord.min_length == 25

    def test_a_legacy_key_holding_the_old_default_adopts_the_new_one(
        self,
    ) -> None:
        """The old default was written by ``creek init``, not chosen.

        This is the arm that stops the migration re-installing
        ``min_length: 10`` on every installed vault — which would arm a live
        Discord data-retention change the day #1041 wires the block.
        """
        cfg = CleaningConfig.model_validate(
            {"discord": {"min_message_length": 10}},
        )
        assert cfg.discord.min_length == 3

    def test_a_same_named_key_holding_the_old_default_is_dropped(self) -> None:
        """Two drifted keys kept their names; the value still must not survive.

        ``quality.accept_threshold`` and ``markdown.min_body_length`` are
        spelled identically before and after #1519, so a migration keyed on
        renames alone would let the drifted value straight through.
        """
        cfg = CleaningConfig.model_validate(
            {"quality": {"accept_threshold": 0.7}, "markdown": {"min_body_length": 50}},
        )
        assert cfg.quality.accept_threshold == 0.6
        assert cfg.markdown.min_body_length == 10

    def test_a_same_named_key_holding_a_chosen_value_is_kept(self) -> None:
        """A deliberate value under an unchanged name passes through."""
        cfg = CleaningConfig.model_validate({"quality": {"accept_threshold": 0.95}})
        assert cfg.quality.accept_threshold == 0.95

    def test_a_relocated_key_lands_in_the_other_block(self) -> None:
        """``validation.min_words`` belongs to ``quality``, whose consumer owns it."""
        cfg = CleaningConfig.model_validate({"validation": {"min_words": 4}})
        assert cfg.quality.min_words == 4
        assert "min_words" not in ValidationConfig.model_fields

    def test_the_new_spelling_is_left_alone(self) -> None:
        """A config already written the new way is not touched."""
        cfg = CleaningConfig.model_validate({"discord": {"min_length": 7}})
        assert cfg.discord.min_length == 7

    def test_a_non_mapping_passes_straight_through(self) -> None:
        """Pydantic must raise its own error, not an AttributeError from the shim."""
        with pytest.raises(ValidationError):
            CleaningConfig.model_validate(["not", "a", "mapping"])

    def test_an_absent_block_is_untouched(self) -> None:
        """An empty cleaning block yields plain defaults."""
        cfg = CleaningConfig.model_validate({})
        assert cfg.discord.min_length == 3
        assert cfg.hygiene.orphan_age_days == 30

    def test_staleness_days_fans_out_to_both_replacements(self) -> None:
        """One deliberate knob becomes two, because it described two scanners."""
        cfg = CleaningConfig.model_validate({"hygiene": {"staleness_days": 45}})
        assert cfg.hygiene.orphan_age_days == 45
        assert cfg.hygiene.stale_review_days == 45

    def test_staleness_days_does_not_overwrite_an_explicit_replacement(
        self,
    ) -> None:
        """A config mid-migration keeps whichever new key it already sets."""
        cfg = CleaningConfig.model_validate(
            {"hygiene": {"staleness_days": 45, "orphan_age_days": 8}},
        )
        assert cfg.hygiene.orphan_age_days == 8
        assert cfg.hygiene.stale_review_days == 45

    def test_staleness_days_at_the_old_default_adopts_both_new_ones(self) -> None:
        """Ninety matched neither scanner, so it was never a real choice."""
        cfg = CleaningConfig.model_validate({"hygiene": {"staleness_days": 90}})
        assert cfg.hygiene.orphan_age_days == 30
        assert cfg.hygiene.stale_review_days == 14

    def test_the_migration_warns_rather_than_migrating_silently(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An operator must be told which key to edit in their own file.

        Args:
            caplog: Pytest log capture fixture.
        """
        with caplog.at_level(logging.WARNING, logger="creek.config"):
            CleaningConfig.model_validate({"discord": {"min_message_length": 25}})

        assert any("min_length" in record.message for record in caplog.records)

    def test_the_migration_does_not_mutate_the_callers_dict(self) -> None:
        """The shim copies; an in-place edit would corrupt the parsed YAML."""
        raw = {"discord": {"min_message_length": 25}}
        CleaningConfig.model_validate(raw)
        assert raw == {"discord": {"min_message_length": 25}}


# ---------------------------------------------------------------------------
# One model per cleaning block, one literal per knob (#1519)
# ---------------------------------------------------------------------------


_COLLAPSING_BLOCKS: Final[tuple[tuple[str, type[BaseModel]], ...]] = (
    ("discord", DiscordFilterConfig),
    ("chatbot", ChatbotFilterConfig),
)
"""The cleaning blocks that have a filter-side model to collapse into.

Two rows, not eight, and deliberately so: ``markdown``, ``google_drive``,
``validation``, ``quality``, ``deduplication`` and ``hygiene`` have no
filter-side config class at all. Their consumers take bare scalar keywords —
``MarkdownFilter``, ``GoogleDriveFilter``, ``FragmentValidator``,
``QualityScorer``, ``OrphanScanner``, ``StaleReviewScanner`` — and
``Deduplicator.__init__`` takes no arguments whatsoever. There is nothing to
collapse those blocks into, so they are pinned by
:class:`TestCleaningDefaultsMatchTheLiveConsumer` below instead (#1519).
"""


def test_the_collapsing_block_table_is_not_empty() -> None:
    """Emptying the table above would make the identity test vanish green."""
    assert len(_COLLAPSING_BLOCKS) == 2


class TestCleaningBlocksAreOneModel:
    """A cleaning block and its filter must share ONE class object (#1519)."""

    @pytest.mark.parametrize(("block", "expected"), _COLLAPSING_BLOCKS)
    def test_block_model_is_the_live_filter_config(
        self,
        block: str,
        expected: type[BaseModel],
    ) -> None:
        """The block's model IS the filter's own class, not a twin of it.

        Identity, not equality: an equal-but-distinct model would still let
        the two declarations drift apart, which is the defect. Before #1519
        ``cleaning.discord`` was a ``DiscordCleaningConfig`` saying
        ``min_message_length = 10`` while the filter ran on a
        ``DiscordFilterConfig`` saying ``min_length = 3``, and nothing
        connected the two.

        Args:
            block: The ``cleaning`` sub-block name.
            expected: The filter-side config class it must be.
        """
        assert type(getattr(CleaningConfig(), block)) is expected


def _signature_default(func: Callable[..., object], parameter: str) -> object:
    """Return a callable's declared default for one keyword parameter.

    Derived from the live signature rather than retyped, so this test cannot
    agree with a stale copy of the value.

    Args:
        func: The callable to inspect.
        parameter: The keyword parameter name.

    Returns:
        The parameter's default value.
    """
    return inspect.signature(func).parameters[parameter].default


def _resolve_leaf(root: object, path: str) -> object:
    """Walk a dotted config path, failing loudly on a missing field.

    Args:
        root: The config object to walk from.
        path: A dotted leaf path such as ``quality.min_words``.

    Returns:
        The value at the leaf.
    """
    node = root
    walked: list[str] = []
    for part in path.split("."):
        assert hasattr(node, part), (
            f"cleaning.{'.'.join([*walked, part])} does not exist on the "
            f"model; #1519 requires this leaf path"
        )
        walked.append(part)
        node = getattr(node, part)
    return node


_DRIFTED_KNOBS: Final[tuple[tuple[str, str, str], ...]] = (
    ("markdown.min_body_length", "MarkdownFilter", "min_body_length"),
    (
        "google_drive.multi_author_threshold",
        "GoogleDriveFilter",
        "multi_author_threshold",
    ),
    ("validation.min_content_length", "FragmentValidator", "min_content_length"),
    ("quality.accept_threshold", "QualityScorer", "accept_threshold"),
    ("quality.review_threshold", "QualityScorer", "review_threshold"),
    ("quality.min_words", "QualityScorer", "min_words"),
    ("quality.stop_word_threshold", "QualityScorer", "stop_word_threshold"),
    ("hygiene.orphan_age_days", "OrphanScanner", "age_days"),
    ("hygiene.stale_review_days", "StaleReviewScanner", "age_days"),
)
"""Every cleaning knob whose value is also written down in a live consumer.

The rule #1519 applies uniformly is **the live value wins**: the consumer's
default is what currently executes, and the config value is dormant, so a
disagreement is the config being wrong. ``discord.min_length`` is absent
because after the collapse the filter's class *is* the config model, leaving
exactly one literal with nothing to compare against.
"""

_LIVE_CONSUMERS: Final[dict[str, Callable[..., object]]] = {
    "MarkdownFilter": MarkdownFilter.__init__,
    "GoogleDriveFilter": GoogleDriveFilter.__init__,
    "FragmentValidator": FragmentValidator.__init__,
    "QualityScorer": QualityScorer.__init__,
    "OrphanScanner": OrphanScanner.__init__,
    "StaleReviewScanner": StaleReviewScanner.__init__,
}
"""Constructor of each live consumer named in :data:`_DRIFTED_KNOBS`."""


def test_the_drifted_knob_table_is_not_empty() -> None:
    """Emptying the table would silently retire the whole drift check."""
    assert len(_DRIFTED_KNOBS) == 9


class TestCleaningDefaultsMatchTheLiveConsumer:
    """Each cleaning knob agrees with the code that actually runs (#1519)."""

    @pytest.mark.parametrize(("leaf", "consumer", "parameter"), _DRIFTED_KNOBS)
    def test_config_default_equals_the_live_default(
        self,
        leaf: str,
        consumer: str,
        parameter: str,
    ) -> None:
        """The config's value is the consumer's value, derived not retyped.

        Args:
            leaf: Dotted path under ``cleaning``.
            consumer: Name of the class that actually reads this knob.
            parameter: The consumer's constructor keyword holding the twin.
        """
        expected = _signature_default(_LIVE_CONSUMERS[consumer], parameter)
        actual = _resolve_leaf(CleaningConfig(), leaf)

        assert actual == expected, (
            f"cleaning.{leaf} is {actual!r} but {consumer}.{parameter} — the "
            f"value that actually runs — is {expected!r}"
        )


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

    def test_env_var_override_vault_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CREEK_VAULT_PATH env var should override the default."""
        monkeypatch.setenv("CREEK_VAULT_PATH", "/tmp/my-vault")  # nosec B108
        cfg = CreekConfig()
        assert cfg.vault_path == Path("/tmp/my-vault")  # nosec B108

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

    def test_loads_yaml_file(self, tmp_path: Path) -> None:
        """load_config() should load values from a YAML file."""
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "vault_path": "/home/user/vault",
            "llm": {"provider": "anthropic", "model": "claude-3"},
            "embeddings": {"similarity_threshold": 0.85},
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.vault_path == Path("/home/user/vault")
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

    def test_load_config_ignores_retired_linking_aggregation_keys(
        self,
        tmp_path: Path,
    ) -> None:
        """A legacy vault carrying the retired FEAT-022 knobs still loads (#1342).

        ADR-0011 deletes four ``linking:`` fields that only ever fed the
        zoom-out aggregator. Every vault ``creek init`` has ever scaffolded
        carries them, so the loader must ignore them rather than reject the
        file — an operator should not have to hand-edit YAML to run
        ``creek link`` after upgrading. ``LinkingConfig`` declares no
        ``model_config``, so pydantic's default ``extra='ignore'`` does
        exactly that.

        The surviving-neighbour assertion is load-bearing: without it this
        test would pass just as happily if the entire ``linking:`` block
        were being discarded, which is the failure mode "unknown keys are
        tolerated" most easily degrades into.
        """
        config_file = tmp_path / "creek_config.yaml"
        retired = (
            "exchange_max_gap_minutes",
            "burst_similarity_threshold",
            "session_max_gap_minutes",
            "cross_source_aggregation",
        )
        config_data = {
            "linking": {
                # All four at non-default values, so a loader that silently
                # kept them would be caught by the hasattr sweep below
                # rather than by a value that happens to match the default.
                "exchange_max_gap_minutes": 99,
                "burst_similarity_threshold": 0.99,
                "session_max_gap_minutes": 999,
                "cross_source_aggregation": True,
                # A surviving key, also non-default.
                "temporal_window_hours": 42,
            },
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)

        for name in retired:
            assert not hasattr(cfg.linking, name), (
                f"linking.{name} was retired by ADR-0011 (#1342) but "
                "LinkingConfig still exposes it"
            )
        assert cfg.linking.temporal_window_hours == 42
        assert cfg.linking.hierarchy_sibling_skip_window == 2

    def test_loads_cleaning_section(self, tmp_path: Path) -> None:
        """load_config() should load cleaning section from YAML."""
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "cleaning": {
                "discord": {"min_length": 25},
                "quality": {"accept_threshold": 0.9},
                "deduplication": {"strategy": "exact"},
            },
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)
        assert cfg.cleaning.discord.min_length == 25
        assert cfg.cleaning.quality.accept_threshold == 0.9
        assert cfg.cleaning.deduplication.strategy == "exact"
        # Unspecified sub-configs keep defaults
        assert cfg.cleaning.chatbot.skip_system_prompts is True
        assert cfg.cleaning.hygiene.orphan_age_days == 30
        assert cfg.cleaning.hygiene.stale_review_days == 14

    def test_loads_a_pre_1519_cleaning_section(self, tmp_path: Path) -> None:
        """A YAML written before #1519 still loads, through the migration.

        The same round-trip as above, spelled the old way: every key that
        ``creek init`` wrote before #1519 is either carried to its new name
        or, when it merely holds the old default, dropped so the corrected
        default applies.

        Args:
            tmp_path: Pytest temporary directory.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_data = {
            "cleaning": {
                "discord": {"min_message_length": 25, "filter_bot_messages": False},
                "quality": {"accept_threshold": 0.7, "skip_threshold": 0.4},
                "validation": {"min_characters": 33, "min_words": 4},
                "google_drive": {"max_collaboration_ratio": 0.75},
                "hygiene": {"staleness_days": 90},
            },
        }
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(config_file)

        # Deliberate values are carried to the surviving names.
        assert cfg.cleaning.discord.min_length == 25
        assert cfg.cleaning.discord.skip_bots is False
        assert cfg.cleaning.quality.review_threshold == 0.4
        assert cfg.cleaning.validation.min_content_length == 33
        assert cfg.cleaning.quality.min_words == 4
        assert cfg.cleaning.google_drive.multi_author_threshold == 0.75
        # Values that merely held the old default adopt the corrected one.
        assert cfg.cleaning.quality.accept_threshold == 0.6
        assert cfg.cleaning.hygiene.orphan_age_days == 30
        assert cfg.cleaning.hygiene.stale_review_days == 14


class TestLoadConfigEnvVar:
    """Tests for the ``CREEK_CONFIG`` env-var discovery path (INC-008)."""

    def test_uses_creek_config_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``CREEK_CONFIG`` is honoured when ``config_path`` is None."""
        config_file = tmp_path / "vault-config.yaml"
        config_file.write_text(yaml.dump({"vault_path": "/vaults/from-env"}))
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config_file))
        # Ensure cwd has no fallback creek_config.yaml that could mask the env var.
        monkeypatch.chdir(tmp_path)

        cfg = load_config()
        assert cfg.vault_path == Path("/vaults/from-env")

    def test_explicit_config_path_overrides_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An explicit ``config_path`` argument wins over the env var."""
        env_file = tmp_path / "from-env.yaml"
        env_file.write_text(yaml.dump({"vault_path": "/vaults/from-env"}))
        cli_file = tmp_path / "from-cli.yaml"
        cli_file.write_text(yaml.dump({"vault_path": "/vaults/from-cli"}))
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(env_file))

        cfg = load_config(cli_file)
        assert cfg.vault_path == Path("/vaults/from-cli")

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
        # A stray CREEK_VAULT_PATH in the ambient environment would otherwise
        # override the built-in default this test is asserting.
        monkeypatch.delenv("CREEK_VAULT_PATH", raising=False)
        monkeypatch.chdir(tmp_path)

        cfg = load_config()
        # No creek_config.yaml in tmp_path → defaults populated.
        assert cfg.vault_path == Path(".")

    def test_unset_env_var_uses_cwd_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the env var is unset, behaviour matches the historical contract."""
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(yaml.dump({"vault_path": "/vaults/from-cwd"}))
        monkeypatch.chdir(tmp_path)

        cfg = load_config()
        assert cfg.vault_path == Path("/vaults/from-cwd")


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
        # Issue #1339: the dead ``timezone`` knob was deleted, so freshly
        # generated configs must stop shipping it — otherwise every new vault
        # is born with a key the loader only tolerates for backwards
        # compatibility.
        assert "timezone" not in data

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Generated config should round-trip back through load_config."""
        output = tmp_path / "creek_config.yaml"
        generate_default_config(output)

        cfg = load_config(output)
        assert cfg.vault_path == Path(".")
        assert cfg.llm.default.provider == "ollama"
        assert cfg.embeddings.model == "all-MiniLM-L6-v2"
        assert cfg.google_drive.scopes == [
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        # Cleaning section should round-trip with defaults
        assert isinstance(cfg.cleaning, CleaningConfig)
        assert cfg.cleaning.discord.skip_bots is True
        assert cfg.cleaning.quality.accept_threshold == 0.6
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


# ---------------------------------------------------------------------------
# Issue #1412: the voice-audience authority maps took any float at all
# ---------------------------------------------------------------------------

_AUTHORITY_MAP_KEYS: tuple[tuple[str, str], ...] = (
    ("privacy_tier_authority", "open"),
    ("representativeness_authority", "self"),
    ("platform_authority", "substack"),
    ("audience_authority", "audience-facing"),
)
"""Each unvalidated authority map, paired with a real key of that map."""

# ``.nan`` / ``.inf`` are how these reach a vault: YAML spells them, so an
# operator config carries them without anything looking unusual.
_REJECTED_AUTHORITIES: tuple[tuple[str, float], ...] = (
    ("nan", float("nan")),
    ("inf", float("inf")),
    ("-inf", float("-inf")),
    ("negative", -1.0),
)
"""Authority values a ranking multiplier must never be handed."""


class TestVoiceAudienceAuthorityValues:
    """The four authority maps must refuse values ranking cannot order.

    Issue #1412. ``VoiceAudienceWeightingConfig``'s four maps were bare
    ``dict[str, float]``, so a vault's ``creek_config.yaml`` could set any
    float. Two kinds are not merely odd, they break the exemplar ranking
    at ``creek/generate/voice.py`` in different ways: a non-finite
    authority destroys the total order the ``sorted`` key depends on, so
    the chosen exemplars vary with input order; a negative one is
    deterministic but inverts rank, promoting exactly the fragments the
    weighting exists to demote.

    This is robustness, not a privacy control: admission is gated by
    ``within_ceiling`` *before* ranking, so an authority value can only
    reorder an already-admitted set, never widen it.
    """

    @pytest.mark.parametrize(("field_name", "key"), _AUTHORITY_MAP_KEYS)
    @pytest.mark.parametrize(("label", "value"), _REJECTED_AUTHORITIES)
    def test_an_unusable_authority_is_refused_at_load(
        self,
        tmp_path: Path,
        field_name: str,
        key: str,
        label: str,
        value: float,
    ) -> None:
        """Every map rejects NaN, both infinities, and negatives.

        Driven through ``load_config`` on a real YAML file rather than
        through the model constructor, because the operator's vault config
        is the path these values actually arrive by.

        Args:
            tmp_path: Directory for the throwaway vault config.
            field_name: The authority map under test.
            key: A real key of that map, so the value is reachable.
            label: Human-readable name of the rejected value.
            value: The value the map must refuse.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            yaml.safe_dump(
                {"voice_audience_weighting": {field_name: {key: value}}},
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValidationError) as excinfo:
            load_config(config_file)

        message = str(excinfo.value)
        assert key in message, (
            f"the {label} rejection for {field_name} does not name the "
            f"offending key {key!r}, so an operator cannot find it: {message}"
        )

    def test_the_rejected_value_table_is_not_empty(self) -> None:
        """The parametrised table above must actually carry cases.

        A parametrise list that empties out produces zero tests and reads
        as a fast pass, which is how a security- or robustness-shaped gate
        silently stops gating.
        """
        expected_maps = 4
        expected_values = 4
        assert len(_AUTHORITY_MAP_KEYS) == expected_maps
        assert len(_REJECTED_AUTHORITIES) == expected_values

    @pytest.mark.parametrize(("field_name", "key"), _AUTHORITY_MAP_KEYS)
    def test_a_zero_authority_is_still_legal(
        self,
        field_name: str,
        key: str,
    ) -> None:
        """Zero is a designed value, not an edge case to be validated away.

        ``creek/generate/voice.py`` documents that keyless or
        undeclared-tier content deliberately scores ``0.0`` via the
        ``intimate`` authority and warns against carving an exception
        there, and ``_default_privacy_tier_authority`` ships
        ``intimate: 0.0``. A validator that rejected ``<= 0.0`` instead of
        ``< 0.0`` would break that design while looking stricter.

        Args:
            field_name: The authority map under test.
            key: A real key of that map.
        """
        config = VoiceAudienceWeightingConfig(**{field_name: {key: 0.0}})

        assert getattr(config, field_name)[key] == 0.0

    def test_the_shipped_defaults_survive_their_own_validator(self) -> None:
        """The defaults must not be rejected by the rule added for #1412.

        ``_default_privacy_tier_authority`` ships a ``0.0``, so a validator
        written a hair too strictly would reject the values this package
        itself ships.

        Pydantic does not run field validators over defaults unless
        ``validate_default`` is set, so simply constructing the model would
        never exercise the rule -- the assertion would hold no matter what
        the validator said. The defaults are therefore read off a default
        instance and then fed back in *explicitly*, which is the only way
        this test can see the validator at all.
        """
        defaults = VoiceAudienceWeightingConfig()
        shipped = {
            field_name: getattr(defaults, field_name)
            for field_name, _ in _AUTHORITY_MAP_KEYS
        }
        assert shipped["privacy_tier_authority"]["intimate"] == 0.0, (
            "the 0.0 this test exists to protect is no longer in the "
            f"defaults: {shipped['privacy_tier_authority']!r}"
        )

        revalidated = VoiceAudienceWeightingConfig(**shipped)

        assert all(
            value >= 0.0
            for field_name, _ in _AUTHORITY_MAP_KEYS
            for value in getattr(revalidated, field_name).values()
        )

    def test_a_nan_authority_cannot_reach_the_exemplar_ranking(
        self,
        tmp_path: Path,
    ) -> None:
        """The premise, then the guarantee: NaN is unorderable, so it is refused.

        The first half establishes that a NaN score genuinely destroys the
        ``sorted(..., key=lambda f: (-score(f), f.id))`` total order that
        ``rank_exemplars`` relies on -- the same four fragments yield
        different top-two sets depending only on the order they arrive in.
        The second half is what makes that unreachable: the config that
        would have supplied the NaN never loads.

        Without the first half this would just be another "invalid input
        raises" test; the point of the issue is *why* it has to.

        Args:
            tmp_path: Directory for the throwaway vault config.
        """
        nan = float("nan")
        scores = {"a": nan, "b": 3.0, "c": 2.0, "d": 1.0}
        top_twos = {
            tuple(
                sorted(order, key=lambda fid: (-scores[fid], fid))[:2],
            )
            for order in itertools.permutations(scores)
        }
        assert len(top_twos) > 1, (
            "the premise no longer holds: a NaN score no longer makes the "
            f"exemplar sort order-dependent, so this test's reason is stale: "
            f"{top_twos!r}"
        )

        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            yaml.safe_dump(
                {
                    "voice_audience_weighting": {
                        "platform_authority": {"substack": nan},
                    },
                },
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValidationError):
            load_config(config_file)


# ---------------------------------------------------------------------------
# Deletion of the dormant ``timezone`` field (issue #1339)
# ---------------------------------------------------------------------------


class TestTimezoneFieldDeleted:
    """The dormant ``timezone`` knob is gone, behind a migration shim (#1339).

    ``CreekConfig.timezone`` had zero production readers. Wiring it would have
    made every fragment id a function of the setting — ``generate_fragment_id``
    (``creek/ingest/base.py``) hashes ``timestamp.isoformat()``, whose rendered
    UTC offset changes with the zone, so one instant yields a different
    ``frag-…`` id per configured timezone. That is exactly the id-derivation
    bug #1329 had to migrate vaults out of, so the field was **deleted**
    instead of wired: Creek's anchor is America/Los_Angeles by ontology
    mandate §8.3 (:data:`creek.time.LA_TZ`), not an operator knob.

    Deletion needs a shim because ``CreekConfig`` forbids extra keys and
    ``generate_default_config`` dumps the whole model — so every
    ``creek_config.yaml`` ever written by ``creek init`` carries a
    ``timezone:`` line. Without the shim, deleting the field would turn 100%
    of existing operator configs into a hard ``ValidationError`` at load.
    """

    def test_timezone_is_not_a_config_field(self) -> None:
        """``timezone`` is no longer a field on ``CreekConfig`` (#1339).

        Named explicitly so a revert fails with the reason attached rather
        than as a puzzling knock-on elsewhere: re-adding the field either
        re-introduces a dead knob (caught by ``test_config_contract.py``) or,
        if wired, makes fragment ids config-dependent again.
        """
        assert "timezone" not in CreekConfig.model_fields

    def test_stale_timezone_key_still_loads(self, tmp_path: Path) -> None:
        """A pre-#1339 config carrying ``timezone:`` still loads (#1339).

        The migration path for every vault already on disk: the stale key is
        dropped, and the rest of the file survives untouched.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "timezone": "America/Los_Angeles",
                    "vault_path": "/vaults/legacy",
                },
            ),
        )

        cfg = load_config(config_file)

        assert not hasattr(cfg, "timezone")
        assert "timezone" not in cfg.model_dump()
        # The shim drops one key; it must not swallow the rest of the config.
        assert cfg.vault_path == Path("/vaults/legacy")

    def test_stale_timezone_key_warns_it_is_ignored(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Dropping the stale key is announced, not silent (#1339).

        An operator who set ``timezone: Europe/London`` believed it did
        something. Discarding that quietly would leave them believing it
        still does, so the load emits a WARNING naming the key.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(yaml.dump({"timezone": "Europe/London"}))

        with caplog.at_level(logging.WARNING, logger="creek.config"):
            load_config(config_file)

        messages = [record.getMessage() for record in caplog.records]
        timezone_warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and "timezone" in record.getMessage()
        ]
        assert timezone_warnings, (
            f"no WARNING naming 'timezone'; records were {messages}"
        )
        # Substance, not prose: the operator must learn the key is dead.
        assert any(
            phrase in message.lower()
            for message in timezone_warnings
            for phrase in ("ignored", "obsolete", "no longer", "removed")
        ), f"WARNING never says the key is ignored/obsolete: {timezone_warnings}"

    def test_any_stale_timezone_value_is_tolerated(self, tmp_path: Path) -> None:
        """A nonsense ``timezone:`` value now loads instead of raising (#1339).

        Pins that the validator was *removed* rather than left half-alive: a
        value the old ``validate_timezone`` rejected must sail through, because
        nothing validates a key nothing reads.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            yaml.dump({"timezone": "Not/A/Timezone", "vault_path": "/vaults/legacy"}),
        )

        cfg = load_config(config_file)

        assert not hasattr(cfg, "timezone")
        assert cfg.vault_path == Path("/vaults/legacy")

    def test_genuinely_unknown_key_is_still_rejected(self, tmp_path: Path) -> None:
        """The shim must not be implemented as a blanket ``extra='ignore'``.

        The one-way ratchet: dropping *one* named, historically-generated key
        is a migration; accepting *any* unknown key turns every config typo
        into a silent no-op. ``redction:`` is a plausible ``redaction:`` typo.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(yaml.dump({"redction": {"enabled": True}}))

        with pytest.raises(ValidationError) as excinfo:
            load_config(config_file)

        errors = excinfo.value.errors()
        assert [error["type"] for error in errors] == ["extra_forbidden"]
        assert [error["loc"] for error in errors] == [("redction",)]

    def test_unknown_key_rejected_even_beside_a_stale_timezone(
        self,
        tmp_path: Path,
    ) -> None:
        """Popping ``timezone`` must not amnesty its neighbours (#1339).

        Guards the shim shape: it removes one key by name, rather than
        clearing whatever the model would otherwise reject.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            yaml.dump({"timezone": "UTC", "redction": {"enabled": True}}),
        )

        with pytest.raises(ValidationError) as excinfo:
            load_config(config_file)

        assert [error["loc"] for error in excinfo.value.errors()] == [("redction",)]

    def test_stray_creek_timezone_env_var_is_inert(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A leftover ``CREEK_TIMEZONE`` export must not break startup (#1339).

        pydantic-settings silently drops env vars that match no field, so the
        operator whose shell profile still exports it gets defaults rather
        than a crash. Pinned here so it cannot regress into a hard failure.
        """
        monkeypatch.setenv("CREEK_TIMEZONE", "Europe/London")

        cfg = CreekConfig()

        assert not hasattr(cfg, "timezone")
        assert "timezone" not in cfg.model_dump()

    def test_modern_config_loads_silently(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A config without the stale key warns about nothing (#1339).

        The deprecation notice must be conditional on the key being present.
        Hoisting the ``logger.warning`` above the shim's guard would make
        every single load noisy forever, and every other test in this class
        would still pass — so the silence is pinned explicitly.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(yaml.dump({"vault_path": "/vaults/modern"}))

        with caplog.at_level(logging.WARNING, logger="creek.config"):
            cfg = load_config(config_file)

        assert cfg.vault_path == Path("/vaults/modern")
        assert [record.getMessage() for record in caplog.records] == []

    @pytest.mark.parametrize("payload", [None, 42], ids=["none", "int"])
    def test_non_mapping_input_still_raises_a_validation_error(
        self,
        payload: object,
    ) -> None:
        """A non-``dict`` payload fails as pydantic's error, not the shim's.

        The shim runs before pydantic has coerced anything, so it must not
        assume it was handed a container. Dropping its ``isinstance`` guard
        makes ``_OBSOLETE_TIMEZONE_KEY not in data`` raise ``TypeError:
        argument of type 'int' is not iterable`` from *inside* validation,
        instead of the ``ValidationError`` callers are written to handle.

        Both payloads are deliberately non-iterable: a ``list`` or ``str``
        would support ``in`` and so pass even without the guard (#1339).

        Args:
            payload: A non-mapping value handed straight to validation.
        """
        with pytest.raises(ValidationError):
            CreekConfig.model_validate(payload)


class TestAuthorMaxReproducedTier:
    """Tests for ``AuthorConfig.max_reproduced_tier`` (#1354).

    The privacy ceiling the Writing Desk's HARD leak gate enforces used to come
    from the medium contract alone — and a medium contract is authored *inside
    the vault*, so one edited YAML line disarmed the gate. This field moves the
    permission out of the vault and into the operator's ``creek_config.yaml``,
    where the effective ceiling becomes the more restrictive of the two.

    Every invalid value therefore has to fail **closed**, at ``open``: this is
    the input to a security gate, and the alternative — raising — turns a typo
    into a crashed review, which is the pressure that gets checks skipped.
    """

    def test_defaults_to_the_strictest_tier(self) -> None:
        """An operator who never touched the key gets the strictest ceiling.

        The default is what every existing vault gets on upgrade, so it must
        be the value that can only *narrow* the gate relative to today.
        """
        assert AuthorConfig().max_reproduced_tier == "open"

    def test_a_garbage_value_fails_closed_to_open(self) -> None:
        """An unrecognised tier string is ignored, not raised on.

        Validated through ``model_validate`` rather than the keyword
        constructor because that is the path a real config takes: the string
        arrives from ``yaml.safe_load`` inside :func:`load_config`, where no
        static type ever constrained it.
        """
        cfg = AuthorConfig.model_validate({"max_reproduced_tier": "banana"})

        assert cfg.max_reproduced_tier == "open"

    def test_an_explicit_null_fails_closed_to_open(self) -> None:
        """``max_reproduced_tier:`` with no value parses as ``None``.

        A bare key with an empty value is the single most likely hand-edit
        mistake, and YAML hands it over as ``None`` rather than as a missing
        key — so the field default never fires and the validator has to.
        """
        cfg = AuthorConfig.model_validate({"max_reproduced_tier": None})

        assert cfg.max_reproduced_tier == "open"

    def test_a_valid_tier_is_accepted(self) -> None:
        """A deliberate operator choice survives validation unchanged.

        The fail-closed cases above are all satisfied by a validator that
        hard-codes ``"open"``; this is what stops that.
        """
        cfg = AuthorConfig(max_reproduced_tier="intimate")

        assert cfg.max_reproduced_tier == "intimate"

    def test_the_legacy_public_alias_maps_to_open(self) -> None:
        """The pre-INC-003 spelling ``"public"`` still means ``open``.

        :meth:`creek.models.PrivacyTier._missing_` maps the legacy value and
        emits a :class:`DeprecationWarning`. An older vault's config must land
        on ``open`` — the same tier — rather than fall through the alias into
        the garbage path, which would also give ``open`` but by accident.

        The warning is suppressed rather than asserted on: whether the
        validator routes through the enum (and so re-emits it) is an
        implementation detail; that ``"public"`` means ``open`` is not.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cfg = AuthorConfig.model_validate({"max_reproduced_tier": "public"})

        assert cfg.max_reproduced_tier == "open"

    def test_the_literal_mirrors_every_privacy_tier(self) -> None:
        """``PrivacyTierName`` must list exactly the tiers the enum defines.

        ``creek/config.py`` cannot import :mod:`creek.models` at module level:
        ``creek/models.py`` imports back from ``creek.config`` at the bottom of
        the file (the ``# noqa: E402`` import at ``creek/models.py:1153``), so
        the pair is only acyclic in one direction. The ``Literal`` is therefore
        a **hand-maintained mirror** of
        :class:`~creek.models.PrivacyTier`, and nothing but this test keeps the
        two honest.

        The drift matters in the dangerous direction: add a fifth tier to the
        enum, forget the mirror, and an operator who configures the new tier
        gets it silently rejected and replaced by the fail-closed default —
        or, worse, the leak gate compares a name the ceiling table has never
        heard of.

        The import is local so this file still collects (and its ~200 other
        tests still run) on a tree where the alias does not exist yet.
        """
        from creek.config import PrivacyTierName

        assert set(get_args(PrivacyTierName)) == {tier.value for tier in PrivacyTier}

    def test_generate_default_config_seeds_the_key(self, tmp_path: Path) -> None:
        """``creek init`` writes the key out, at the safe default.

        A field that only exists in Python is a field operators never discover.
        ``generate_default_config`` is what ``creek init`` calls, so the
        generated ``creek_config.yaml`` is where an operator learns the
        ceiling is theirs to set — and it has to arrive at ``open``, not at
        whatever the last vault-authored contract happened to declare.

        Args:
            tmp_path: Destination directory for the generated config.
        """
        output = tmp_path / "creek_config.yaml"

        generate_default_config(output)

        data = yaml.safe_load(output.read_text(encoding="utf-8"))
        assert data["author"]["max_reproduced_tier"] == "open"


class TestAIStyleWeightsAtLoad:
    """The weight guard must bite on the real YAML path (#1615)."""

    def test_a_non_finite_weight_is_refused_at_load(self, tmp_path: Path) -> None:
        """``ai_style.authorship_weights`` with ``.nan`` fails to load.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            "ai_style:\n  authorship_weights:\n    journal: .nan\n",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError):
            load_config(config_file)

    def test_the_issues_path_would_have_been_vacuous(self, tmp_path: Path) -> None:
        """``author.authorship_weights`` is silently discarded, not validated.

        #1615's own Premise names ``author.authorship_weights``, but the
        field lives on :class:`AIStyleConfig`. Nested models do not forbid
        extras — only top-level ``CreekConfig`` does — so this YAML loads
        **clean** and the key vanishes. Pinned so a future reader does not
        rediscover it by writing a test that passes for the wrong reason.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config_file = tmp_path / "creek_config.yaml"
        config_file.write_text(
            "author:\n  authorship_weights:\n    journal: .nan\n",
            encoding="utf-8",
        )

        config = load_config(config_file)

        assert not hasattr(config.author, "authorship_weights"), (
            "author.authorship_weights now exists; #1615's premise was "
            "right after all and this pin should become a real assertion"
        )
        assert config.ai_style.authorship_weights == _default_authorship_weights()
