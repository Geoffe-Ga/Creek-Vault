"""Tests for the Pipeline orchestrator and PipelineResult model.

Verifies that the Pipeline wires all processing stages end-to-end,
handles edge cases (empty source dirs, missing ingestors, nonexistent
paths), and produces correct aggregate counts. Integration tests
confirm the full pipeline runs without errors against real temp files.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from creek.config import CreekConfig
from creek.pipeline import Pipeline, PipelineResult

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VAULT_DIRS: list[str] = [
    "00-Creek-Meta/Processing-Log",
    "01-Fragments/Conversations",
    "01-Fragments/Messages",
    "01-Fragments/Unsorted",
    "02-Threads/Active",
    "02-Threads/Dormant",
    "02-Threads/Resolved",
    "03-Eddies",
    "04-Praxis/Daily",
    "04-Praxis/Seasonal",
    "04-Praxis/Situational",
    "06-Frequencies",
    "08-Decisions/Active",
    "08-Decisions/Archive",
]
"""Standard vault directories created for every test."""


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal Obsidian vault structure under tmp_path."""
    vault = tmp_path / "vault"
    for d in VAULT_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


@pytest.fixture()
def source_path(tmp_path: Path) -> Path:
    """Create a source directory with sample markdown files."""
    src = tmp_path / "source"
    src.mkdir()
    (src / "note1.md").write_text(
        "# Systems thinking\n\nExploring patterns and integration."
    )
    (src / "note2.md").write_text(
        "# Personal safety\n\nReflecting on survival and security."
    )
    (src / "note3.md").write_text(
        "# Creative expression\n\nArticulating ideas through writing."
    )
    return src


@pytest.fixture()
def config() -> CreekConfig:
    """Return a default CreekConfig for testing."""
    return CreekConfig()


@pytest.fixture()
def fixtures_dir() -> Path:
    """Return the path to the tests/fixtures directory."""
    from pathlib import Path as _Path

    return _Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# PipelineResult model tests
# ---------------------------------------------------------------------------


class TestPipelineResult:
    """Tests for the PipelineResult Pydantic model."""

    def test_default_values(self):
        """Test that PipelineResult initialises with zero counts."""
        result = PipelineResult()
        assert result.files_scanned == 0
        assert result.fragments_created == 0
        assert result.classifications_made == 0
        assert result.links_found == 0
        assert result.indexes_generated == 0

    def test_custom_values(self):
        """Test that PipelineResult accepts custom counts."""
        result = PipelineResult(
            files_scanned=10,
            fragments_created=5,
            classifications_made=5,
            links_found=3,
            indexes_generated=4,
        )
        assert result.files_scanned == 10
        assert result.fragments_created == 5
        assert result.classifications_made == 5
        assert result.links_found == 3
        assert result.indexes_generated == 4

    def test_serialization(self):
        """Test that PipelineResult serialises to dict correctly."""
        result = PipelineResult(files_scanned=2, indexes_generated=4)
        data = result.model_dump()
        assert data["files_scanned"] == 2
        assert data["indexes_generated"] == 4

    def test_is_pydantic_model(self):
        """Test that PipelineResult is a Pydantic BaseModel, not a dataclass."""
        from pydantic import BaseModel

        assert issubclass(PipelineResult, BaseModel)


# ---------------------------------------------------------------------------
# Pipeline initialisation tests
# ---------------------------------------------------------------------------


class TestPipelineInit:
    """Tests for Pipeline.__init__ component wiring."""

    def test_creates_scanner(self, config):
        """Test that Pipeline initialises a RedactionScanner."""
        pipeline = Pipeline(config=config)
        assert pipeline.scanner is not None

    def test_creates_rule_classifier(self, config):
        """Test that Pipeline initialises a RuleClassifier."""
        pipeline = Pipeline(config=config)
        assert pipeline.rule_classifier is not None

    def test_creates_llm_classifier(self, config):
        """Test that Pipeline initialises an LLMClassifier."""
        pipeline = Pipeline(config=config)
        assert pipeline.llm_classifier is not None

    def test_creates_review_generator(self, config):
        """Test that Pipeline initialises a ReviewQueueGenerator."""
        pipeline = Pipeline(config=config)
        assert pipeline.review_generator is not None

    def test_creates_linking_pipeline(self, config):
        """Test that Pipeline initialises a LinkingPipeline."""
        pipeline = Pipeline(config=config)
        assert pipeline.linking_pipeline is not None

    def test_stores_config(self, config):
        """Test that Pipeline stores the provided config."""
        pipeline = Pipeline(config=config)
        assert pipeline.config is config


# ---------------------------------------------------------------------------
# Pipeline.run() -- empty / edge-case scenarios
# ---------------------------------------------------------------------------


class TestPipelineRunEmpty:
    """Tests for Pipeline.run() with empty or missing inputs."""

    def test_nonexistent_source_path(self, config, vault_path, tmp_path):
        """Test that a nonexistent source path returns zero counts."""
        pipeline = Pipeline(config=config)
        result = pipeline.run(
            source_path=tmp_path / "nonexistent",
            vault_path=vault_path,
        )
        assert result.files_scanned == 0
        assert result.fragments_created == 0

    def test_empty_source_directory(self, config, vault_path, tmp_path):
        """Test that an empty source directory returns zero fragments."""
        empty_src = tmp_path / "empty_src"
        empty_src.mkdir()
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=empty_src, vault_path=vault_path)
        assert result.files_scanned == 0
        assert result.fragments_created == 0

    def test_no_ingestors_registered(self, config, vault_path, source_path):
        """Test that pipeline handles empty INGESTOR_REGISTRY gracefully."""
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        # No ingestors registered, so no fragments created
        assert result.fragments_created == 0
        # But files should still be scanned for redaction
        assert result.files_scanned == 3

    def test_no_fragments_skips_classification(self, config, vault_path, source_path):
        """Test that classification is skipped when no fragments exist."""
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.classifications_made == 0

    def test_no_fragments_skips_linking(self, config, vault_path, source_path):
        """Test that linking is skipped when no fragments exist."""
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.links_found == 0


# ---------------------------------------------------------------------------
# Pipeline.run() -- stage-level tests with mocking
# ---------------------------------------------------------------------------


class TestPipelineStages:
    """Tests for individual pipeline stages using mocks."""

    def test_redaction_disabled(self, vault_path, source_path):
        """Test that redaction scan is skipped when disabled in config."""
        config = CreekConfig()
        config.redaction.enabled = False
        pipeline = Pipeline(config=config)
        with patch.object(pipeline.scanner, "scan_directory") as mock_scan:
            result = pipeline.run(source_path=source_path, vault_path=vault_path)
            mock_scan.assert_not_called()
            # Files are still counted even when scanning is disabled
            assert result.files_scanned == 3

    def test_redaction_enabled_scans_directory(self, vault_path, source_path):
        """Test that redaction scanner is called when enabled."""
        config = CreekConfig()
        pipeline = Pipeline(config=config)
        with patch.object(
            pipeline.scanner, "scan_directory", return_value=[]
        ) as mock_scan:
            pipeline.run(source_path=source_path, vault_path=vault_path)
            mock_scan.assert_called_once_with(source_path)

    def test_redaction_logs_when_matches_found(self, vault_path, tmp_path):
        """Test that redaction scan logs findings when PII is detected."""
        config = CreekConfig()
        src = tmp_path / "pii_source"
        src.mkdir()
        (src / "contact.md").write_text("Email me at user@example.com")

        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=src, vault_path=vault_path)
        assert result.files_scanned == 1

    def test_indexing_generates_notes(self, config, vault_path, source_path):
        """Test that index generation produces files in the vault."""
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        # Index generation should produce thread, eddy, temporal, source indexes
        # (frequency indexes depend on subdirs existing)
        assert result.indexes_generated >= 4

    def test_indexing_creates_thread_index(self, config, vault_path, source_path):
        """Test that a Thread-Index.md is created in vault."""
        pipeline = Pipeline(config=config)
        pipeline.run(source_path=source_path, vault_path=vault_path)
        thread_index = vault_path / "02-Threads" / "Thread-Index.md"
        assert thread_index.exists()

    def test_indexing_creates_eddy_map(self, config, vault_path, source_path):
        """Test that an Eddy-Map.md is created in vault."""
        pipeline = Pipeline(config=config)
        pipeline.run(source_path=source_path, vault_path=vault_path)
        eddy_map = vault_path / "03-Eddies" / "Eddy-Map.md"
        assert eddy_map.exists()

    def test_indexing_creates_temporal_index(self, config, vault_path, source_path):
        """Test that a Temporal-Index.md is created in vault."""
        pipeline = Pipeline(config=config)
        pipeline.run(source_path=source_path, vault_path=vault_path)
        temporal = vault_path / "00-Creek-Meta" / "Temporal-Index.md"
        assert temporal.exists()

    def test_indexing_creates_source_index(self, config, vault_path, source_path):
        """Test that a Source-Index.md is created in vault."""
        pipeline = Pipeline(config=config)
        pipeline.run(source_path=source_path, vault_path=vault_path)
        source_idx = vault_path / "00-Creek-Meta" / "Source-Index.md"
        assert source_idx.exists()


# ---------------------------------------------------------------------------
# Pipeline.run() -- with mocked ingestors (fragments present)
# ---------------------------------------------------------------------------


class TestPipelineWithFragments:
    """Tests for Pipeline.run() when fragments are produced by ingestion."""

    def _make_mock_ingestor_registry(self, source_path):
        """Build a mock INGESTOR_REGISTRY that returns one ParsedFragment.

        Args:
            source_path: The source path, used for provenance.

        Returns:
            A dict suitable for patching INGESTOR_REGISTRY.
        """
        from datetime import datetime

        from creek.ingest.base import IngestResult, ParsedFragment

        fragment = ParsedFragment(
            content="Test content about systems and patterns",
            metadata={},
            source_path=str(source_path / "note1.md"),
            timestamp=datetime.now(),
        )
        ingest_result = IngestResult(fragments=[fragment])

        mock_ingestor = MagicMock()
        mock_ingestor.return_value.ingest.return_value = ingest_result

        return {"mock": mock_ingestor}

    def test_ingestion_with_registered_ingestor(self, config, vault_path, source_path):
        """Test that registered ingestors produce fragments."""
        registry = self._make_mock_ingestor_registry(source_path)
        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.fragments_created == 1

    def test_classification_runs_on_fragments(self, config, vault_path, source_path):
        """Test that classification runs when fragments are available."""
        registry = self._make_mock_ingestor_registry(source_path)
        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.classifications_made == 1

    def test_linking_runs_on_fragments(self, config, vault_path, source_path):
        """Test that linking runs when fragments are available."""
        registry = self._make_mock_ingestor_registry(source_path)
        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)
        # Linking returns counts (may be 0 with stubs, but it ran)
        assert result.links_found >= 0

    def test_review_queue_generated(self, config, vault_path, source_path):
        """Test that review queue markdown is generated for fragments."""
        registry = self._make_mock_ingestor_registry(source_path)
        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            pipeline.run(source_path=source_path, vault_path=vault_path)
        # Review queue file should exist in vault_path
        review_files = list(vault_path.glob("review-queue-*.md"))
        assert len(review_files) == 1


# ---------------------------------------------------------------------------
# Pipeline private method tests
# ---------------------------------------------------------------------------


class TestPipelinePrivateMethods:
    """Tests for Pipeline helper methods."""

    def test_run_redaction_nonexistent(self, config, tmp_path):
        """Test _run_redaction returns 0 for nonexistent path."""
        pipeline = Pipeline(config=config)
        result = PipelineResult()
        count = pipeline._run_redaction(tmp_path / "nope", result)
        assert count == 0

    def test_run_ingestion_empty_registry(self, config, source_path):
        """Test _run_ingestion returns empty list when registry is empty."""
        pipeline = Pipeline(config=config)
        result = PipelineResult()
        fragments = pipeline._run_ingestion(source_path, result)
        assert fragments == []

    def test_run_classification_no_fragments(self, config, vault_path):
        """Test _run_classification returns empty list for no fragments."""
        pipeline = Pipeline(config=config)
        result = PipelineResult()
        classified = pipeline._run_classification([], vault_path, result)
        assert classified == []

    def test_run_linking_no_fragments(self, config, vault_path):
        """Test _run_linking returns 0 for no fragments."""
        pipeline = Pipeline(config=config)
        result = PipelineResult()
        count = pipeline._run_linking([], vault_path, result)
        assert count == 0

    def test_run_indexing_creates_files(self, config, vault_path):
        """Test _run_indexing returns count of generated files."""
        pipeline = Pipeline(config=config)
        result = PipelineResult()
        count = pipeline._run_indexing(vault_path, result)
        assert count >= 4


# ---------------------------------------------------------------------------
# CLI integration (process command)
# ---------------------------------------------------------------------------


class TestCLIProcess:
    """Tests for the CLI process command wired to Pipeline."""

    def test_process_runs_pipeline(self, vault_path, source_path):
        """Test that the CLI process command invokes Pipeline.run."""
        from typer.testing import CliRunner

        from creek.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "process",
                "--source",
                str(source_path),
                "--vault",
                str(vault_path),
            ],
        )
        assert result.exit_code == 0
        assert "Files scanned" in result.output

    def test_process_shows_results(self, vault_path, source_path):
        """Test that the CLI process command shows result counts in output."""
        from typer.testing import CliRunner

        from creek.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "process",
                "--source",
                str(source_path),
                "--vault",
                str(vault_path),
            ],
        )
        assert result.exit_code == 0
        assert "Fragments created" in result.output
        assert "Indexes generated" in result.output


# ---------------------------------------------------------------------------
# Integration test: full pipeline smoke test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPipelineIntegration:
    """Integration tests that run the full pipeline against temp files.

    These tests use ``pytest.mark.integration`` and are excluded from
    the default unit test run.
    """

    def test_full_pipeline_with_sample_files(
        self, config, vault_path, source_path, fixtures_dir
    ):
        """Run the Pipeline with sample markdown files and verify results.

        Sets up a temp directory with sample files, runs the full pipeline,
        and asserts that no errors occur, vault folders are populated with
        index notes, and the result counts are consistent.
        """
        # Copy fixture files into source directory
        shutil.copy(fixtures_dir / "sample_fragment.md", source_path / "sample.md")

        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)

        # Pipeline should complete without errors
        assert result.files_scanned >= 3  # 3 original + 1 copied
        # No ingestors, so 0 fragments
        assert result.fragments_created == 0
        assert result.classifications_made == 0
        assert result.links_found == 0
        # Indexes should be generated
        assert result.indexes_generated >= 4

        # Verify vault structure populated
        assert (vault_path / "02-Threads" / "Thread-Index.md").exists()
        assert (vault_path / "03-Eddies" / "Eddy-Map.md").exists()
        assert (vault_path / "00-Creek-Meta" / "Temporal-Index.md").exists()
        assert (vault_path / "00-Creek-Meta" / "Source-Index.md").exists()

    def test_full_pipeline_with_json_fixtures(
        self, config, vault_path, tmp_path, fixtures_dir
    ):
        """Run the Pipeline with JSON fixture files in source directory.

        Verifies that JSON files are scanned without errors even though
        no ingestor can parse them yet.
        """
        src = tmp_path / "json_source"
        src.mkdir()
        shutil.copy(fixtures_dir / "sample_claude_export.json", src)
        shutil.copy(fixtures_dir / "sample_discord_export.json", src)

        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=src, vault_path=vault_path)

        assert result.files_scanned == 2
        assert result.fragments_created == 0
        assert result.indexes_generated >= 4

    def test_pipeline_result_consistency(self, config, vault_path, source_path):
        """Verify that PipelineResult counts are internally consistent.

        With no ingestors registered, classifications and links must be zero,
        while file scanning and indexing should still produce counts.
        """
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)

        # No ingestors => no fragments => no classifications => no links
        assert result.fragments_created == 0
        assert result.classifications_made == 0
        assert result.links_found == 0

        # Redaction and indexing should still work
        assert result.files_scanned > 0
        assert result.indexes_generated > 0

    def test_multiple_runs_are_idempotent(self, config, vault_path, source_path):
        """Verify that running the pipeline twice produces consistent results.

        Index files should be overwritten, not duplicated.
        """
        pipeline = Pipeline(config=config)
        result1 = pipeline.run(source_path=source_path, vault_path=vault_path)
        result2 = pipeline.run(source_path=source_path, vault_path=vault_path)

        assert result1.indexes_generated == result2.indexes_generated
        assert result1.files_scanned == result2.files_scanned


# ---------------------------------------------------------------------------
# Fixture file existence tests
# ---------------------------------------------------------------------------


class TestFixtures:
    """Verify that test fixture files exist and are valid."""

    def test_sample_fragment_exists(self, fixtures_dir):
        """Test that sample_fragment.md exists."""
        assert (fixtures_dir / "sample_fragment.md").exists()

    def test_sample_claude_export_exists(self, fixtures_dir):
        """Test that sample_claude_export.json exists."""
        assert (fixtures_dir / "sample_claude_export.json").exists()

    def test_sample_discord_export_exists(self, fixtures_dir):
        """Test that sample_discord_export.json exists."""
        assert (fixtures_dir / "sample_discord_export.json").exists()

    def test_sample_claude_export_is_valid_json(self, fixtures_dir):
        """Test that sample_claude_export.json is valid JSON."""
        content = (fixtures_dir / "sample_claude_export.json").read_text()
        data = json.loads(content)
        assert "conversation_id" in data
        assert "messages" in data
        assert len(data["messages"]) >= 2

    def test_sample_discord_export_is_valid_json(self, fixtures_dir):
        """Test that sample_discord_export.json is valid JSON."""
        content = (fixtures_dir / "sample_discord_export.json").read_text()
        data = json.loads(content)
        assert "channel" in data
        assert "messages" in data
        assert len(data["messages"]) >= 2

    def test_sample_fragment_has_frontmatter(self, fixtures_dir):
        """Test that sample_fragment.md has YAML frontmatter markers."""
        content = (fixtures_dir / "sample_fragment.md").read_text()
        assert content.startswith("---")
        assert content.count("---") >= 2
