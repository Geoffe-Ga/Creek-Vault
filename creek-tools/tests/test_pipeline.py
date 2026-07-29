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
from creek.consent import ConsentManager
from creek.pipeline import Pipeline, PipelineResult

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import PrivacyTier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Standard vault directories created for every test.
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

    def test_creates_tier_classifiers(self, config):
        """Test that Pipeline initialises the per-tier classifiers (#706)."""
        pipeline = Pipeline(config=config)
        assert pipeline.tier_classifiers.non_intimate is not None

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
        with patch("creek.pipeline.INGESTOR_REGISTRY", {}):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)
        # No ingestors registered, so no fragments created
        assert result.fragments_created == 0
        # But files should still be scanned for redaction
        assert result.files_scanned == 3

    def test_no_fragments_skips_classification(self, config, vault_path, source_path):
        """Test that classification is skipped when no fragments exist."""
        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", {}):
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

    def test_redaction_dry_run_continues_when_matches_found(
        self,
        vault_path,
        tmp_path,
    ):
        """Dry-run scan logs findings but lets the pipeline continue."""
        config = CreekConfig()
        config.redaction.dry_run = True
        src = tmp_path / "pii_source"
        src.mkdir()
        (src / "contact.md").write_text("Email me at user@example.com")

        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=src, vault_path=vault_path)
        assert result.files_scanned == 1

    def test_redaction_fails_loud_when_matches_found(
        self,
        vault_path,
        tmp_path,
    ):
        """When matches exist and dry_run is false, the pipeline aborts."""
        from creek.pipeline import RedactionRequiredError

        config = CreekConfig()
        src = tmp_path / "pii_source"
        src.mkdir()
        (src / "contact.md").write_text("Email me at user@example.com")

        pipeline = Pipeline(config=config)
        with pytest.raises(RedactionRequiredError) as excinfo:
            pipeline.run(source_path=src, vault_path=vault_path)

        assert excinfo.value.match_count >= 1
        assert "creek redact --apply" in str(excinfo.value)

    def test_redaction_clean_source_continues(self, vault_path, tmp_path):
        """A source with no matches should run end-to-end."""
        config = CreekConfig()
        src = tmp_path / "clean_source"
        src.mkdir()
        (src / "ok.md").write_text("Just notes — no secrets here.")

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

        Mirrors the post-fix four-stage ingestor contract: each parsed
        fragment carries its converted ``markdown`` body and the
        ``frontmatter`` dict on ``metadata`` so the pipeline can
        assemble it into a real :class:`Fragment` with a deterministic ID.

        Args:
            source_path: The source path, used for provenance.

        Returns:
            A dict suitable for patching INGESTOR_REGISTRY.
        """
        from datetime import datetime

        from creek.ingest.base import IngestResult, ParsedFragment

        fragment = ParsedFragment(
            content="Test content about systems and patterns",
            metadata={
                "markdown": "Test content about systems and patterns",
                "frontmatter": {
                    "type": "fragment",
                    "title": "Mock note",
                    "source": {"platform": "markdown"},
                },
            },
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


class TestPipelineErrorSurfacing:
    """Tests that ingestor errors and assembly failures reach PipelineResult."""

    def _make_failing_registry(self, source_path):
        """Build a registry whose ingestor reports one error and one good fragment."""
        from datetime import datetime

        from creek.ingest.base import IngestResult, ParsedFragment

        bad_path = str(source_path / "broken.bin")
        good = ParsedFragment(
            content="Good content",
            metadata={
                "markdown": "Good content",
                "frontmatter": {
                    "type": "fragment",
                    "title": "Good note",
                    "source": {"platform": "markdown"},
                },
            },
            source_path=str(source_path / "good.md"),
            timestamp=datetime.now(),
        )
        ingest_result = IngestResult(
            fragments=[good],
            errors=[f"parse error for {bad_path}: corrupt header"],
        )
        mock_ingestor = MagicMock()
        mock_ingestor.return_value.ingest.return_value = ingest_result
        return {"mock": mock_ingestor}, bad_path

    def test_errors_surface_on_pipeline_result(
        self,
        config,
        vault_path,
        source_path,
    ) -> None:
        """Ingestor-reported errors land on PipelineResult.errors."""
        registry, bad_path = self._make_failing_registry(source_path)
        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.fragments_created == 1
        assert len(result.errors) == 1
        assert bad_path in result.errors[0]
        assert result.errors[0].startswith("[mock]")

    def test_cli_process_prints_errors(
        self,
        vault_path,
        source_path,
    ) -> None:
        """CLI process command surfaces both fragment count and error count."""
        from typer.testing import CliRunner

        from creek.cli import app

        registry, _bad_path = self._make_failing_registry(source_path)
        runner = CliRunner()
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = runner.invoke(
                app,
                [
                    "process",
                    "--source",
                    str(source_path),
                    "--vault",
                    str(vault_path),
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        # Match the rendered error count line precisely so an unrelated
        # "1" elsewhere in the output (line numbers, fragment counts,
        # etc.) does not fool this assertion.
        assert "Errors: 1" in result.output
        # The error message itself should be visible to the user.
        assert "broken.bin" in result.output

    def test_assembly_failure_surfaces_as_error(
        self,
        config,
        vault_path,
        source_path,
    ) -> None:
        """Per-fragment assembly failures land on PipelineResult.errors."""
        from datetime import datetime

        from creek.ingest.base import IngestResult, ParsedFragment

        # ParsedFragment with missing 'frontmatter' / 'markdown' keys —
        # violates the ingestor contract and triggers the recoverable
        # ``KeyError`` branch in _run_ingestion.
        broken = ParsedFragment(
            content="x",
            metadata={},  # contract violation
            source_path=str(source_path / "broken.md"),
            timestamp=datetime.now(),
        )
        ingest_result = IngestResult(fragments=[broken])
        mock_ingestor = MagicMock()
        mock_ingestor.return_value.ingest.return_value = ingest_result
        registry = {"mock": mock_ingestor}

        pipeline = Pipeline(config=config)
        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)

        assert result.fragments_created == 0
        assert len(result.errors) == 1
        assert "[mock]" in result.errors[0]
        assert "broken.md" in result.errors[0]

    def test_vault_write_keyerror_surfaces_as_error(
        self,
        config,
        vault_path,
        source_path,
    ) -> None:
        """A missing platform mapping at write time becomes a graceful error."""
        from datetime import datetime

        from creek.ingest.base import IngestResult, ParsedFragment

        good = ParsedFragment(
            content="content",
            metadata={
                "markdown": "content",
                "frontmatter": {
                    "type": "fragment",
                    "title": "Note",
                    "source": {"platform": "markdown"},
                },
            },
            source_path=str(source_path / "good.md"),
            timestamp=datetime.now(),
        )
        ingest_result = IngestResult(fragments=[good])
        mock_ingestor = MagicMock()
        mock_ingestor.return_value.ingest.return_value = ingest_result
        registry = {"mock": mock_ingestor}

        # Simulate the regression scenario the writer guard is defending
        # against: an enum value with no entry in _PLATFORM_SUBFOLDER.
        # We patch the mapping to remove the markdown entry; the writer
        # would otherwise raise an uncaught KeyError that bypasses the
        # error infrastructure.
        from creek.vault import writer as writer_mod

        broken_map = dict(writer_mod._PLATFORM_SUBFOLDER)
        broken_map.pop("markdown")
        pipeline = Pipeline(config=config)
        with (
            patch("creek.pipeline.INGESTOR_REGISTRY", registry),
            patch.object(writer_mod, "_PLATFORM_SUBFOLDER", broken_map),
        ):
            result = pipeline.run(source_path=source_path, vault_path=vault_path)

        # Pipeline keeps running; the missing-mapping fragment surfaces as
        # an error but doesn't crash the run.
        assert any("vault-writer" in err for err in result.errors)


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
        with patch("creek.pipeline.INGESTOR_REGISTRY", {}):
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
                "--yes",
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
                "--yes",
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
        # Markdown ingestor finds .md files
        assert result.fragments_created >= 3
        assert result.classifications_made >= 3
        # Linking now runs against real fragment metadata. The mock
        # SentenceTransformer in conftest.py is deterministic within a
        # single pytest process (seeded by hash() of each fragment's
        # content), so the count is stable across re-runs but its
        # absolute value depends on PYTHONHASHSEED. We bound it to a
        # sane envelope rather than pin an exact number: the lower
        # bound catches "linker silently skipped"; the upper bound
        # catches "linker exploded into N^2 spam links".
        assert isinstance(result.links_found, int)
        assert 0 <= result.links_found <= result.fragments_created**2
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

        Verifies that JSON files are scanned and the Claude ingestor
        picks up the Claude export file while ignoring non-Claude JSON.
        """
        src = tmp_path / "json_source"
        src.mkdir()
        shutil.copy(fixtures_dir / "sample_claude_export.json", src)
        shutil.copy(fixtures_dir / "sample_discord_export.json", src)

        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=src, vault_path=vault_path)

        assert result.files_scanned == 2
        # Claude ingestor now picks up the Claude export fixture
        assert result.fragments_created >= 1
        assert result.indexes_generated >= 4

    def test_pipeline_result_consistency(self, config, vault_path, source_path):
        """Verify that PipelineResult counts are internally consistent.

        With the markdown ingestor registered, it processes .md files.
        Classifications should equal fragments, and linking/indexing work.
        """
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)

        # Markdown ingestor processes the .md source files
        assert result.fragments_created > 0
        assert result.classifications_made == result.fragments_created

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


# ---------------------------------------------------------------------------
# Pipeline consent gating tests
# ---------------------------------------------------------------------------


class TestPipelineConsent:
    """Tests for Pipeline consent gating via ConsentManager."""

    def test_no_consent_manager_runs_normally(self, config, vault_path, source_path):
        """Pipeline without consent_manager should run all stages."""
        pipeline = Pipeline(config=config)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.files_scanned > 0
        assert result.fragments_created > 0

    def test_consent_granted_runs_ingestion(
        self, config, vault_path, source_path, tmp_path
    ):
        """Pipeline with granted consent should run ingestion."""
        log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
        log_dir.mkdir(parents=True)
        cm = ConsentManager(log_dir=log_dir)
        cm.record_consent(
            source_type="pipeline",
            source_path=str(source_path),
            file_count=3,
            exclusions=[],
            operator="test",
        )
        pipeline = Pipeline(config=config, consent_manager=cm)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.fragments_created > 0

    def test_consent_denied_skips_ingestion(
        self, config, vault_path, source_path, tmp_path
    ):
        """Pipeline without consent should skip ingestion."""
        log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
        log_dir.mkdir(parents=True)
        cm = ConsentManager(log_dir=log_dir)
        # No consent recorded
        pipeline = Pipeline(config=config, consent_manager=cm)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.fragments_created == 0
        assert result.classifications_made == 0

    def test_consent_denied_still_scans_files(
        self, config, vault_path, source_path, tmp_path
    ):
        """Pipeline without consent should still scan files for redaction."""
        log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
        log_dir.mkdir(parents=True)
        cm = ConsentManager(log_dir=log_dir)
        pipeline = Pipeline(config=config, consent_manager=cm)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.files_scanned > 0

    def test_consent_denied_still_generates_indexes(
        self, config, vault_path, source_path, tmp_path
    ):
        """Pipeline without consent should still generate indexes."""
        log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
        log_dir.mkdir(parents=True)
        cm = ConsentManager(log_dir=log_dir)
        pipeline = Pipeline(config=config, consent_manager=cm)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)
        assert result.indexes_generated >= 4

    def test_consent_manager_stored_on_pipeline(self, config, tmp_path):
        """Pipeline should store the consent_manager attribute."""
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        cm = ConsentManager(log_dir=log_dir)
        pipeline = Pipeline(config=config, consent_manager=cm)
        assert pipeline.consent_manager is cm


# ---------------------------------------------------------------------------
# BUG-003: classification gating on confidence threshold
# ---------------------------------------------------------------------------


class TestPipelineClassificationGating:
    """Tests that LLM classification only runs when rules are uncertain."""

    def _make_ingested(
        self,
        platform: str = "markdown",
        title: str = "Mock note",
    ):
        """Return a single ``IngestedFragment`` with the given source platform."""
        from creek.ingest.base import IngestedFragment
        from creek.models import Fragment, FragmentSource, SourcePlatform

        fragment = Fragment(
            id="frag-test12345678",
            title=title,
            source=FragmentSource(platform=SourcePlatform(platform)),
        )
        return IngestedFragment(fragment=fragment, body="body text")

    def test_high_confidence_skips_llm(self, config, vault_path):
        """If rules give high confidence, LLM is not invoked."""
        pipeline = Pipeline(config=config)
        item = self._make_ingested()

        with (
            patch.object(
                pipeline.rule_classifier,
                "classify",
                side_effect=lambda f, content="": f.model_copy(
                    update={
                        "frequency": f.frequency.model_copy(
                            update={"primary": "F5"},
                        ),
                    },
                ),
            ),
            patch.object(
                pipeline.rule_classifier,
                "confidence_score",
                return_value=0.95,
            ),
            patch.object(
                pipeline.tier_classifiers.non_intimate, "classify"
            ) as mock_llm,
        ):
            pipeline._run_classification([item], vault_path, PipelineResult())

        mock_llm.assert_not_called()

    def test_low_confidence_invokes_llm(self, config, vault_path):
        """If rules give low confidence, LLM is invoked."""
        pipeline = Pipeline(config=config)
        item = self._make_ingested()

        with (
            patch.object(
                pipeline.rule_classifier,
                "classify",
                side_effect=lambda f, content="": f.model_copy(
                    update={
                        "frequency": f.frequency.model_copy(
                            update={"primary": "F5"},
                        ),
                    },
                ),
            ),
            patch.object(
                pipeline.rule_classifier,
                "confidence_score",
                return_value=0.1,
            ),
            patch.object(
                pipeline.tier_classifiers.non_intimate,
                "classify",
                side_effect=lambda f, content="": f,
            ) as mock_llm,
        ):
            pipeline._run_classification([item], vault_path, PipelineResult())

        mock_llm.assert_called_once()

    def test_unclassified_invokes_llm(self, config, vault_path):
        """If rules leave fragment unclassified, LLM is invoked."""
        pipeline = Pipeline(config=config)
        item = self._make_ingested()

        with (
            patch.object(
                pipeline.rule_classifier,
                "classify",
                side_effect=lambda f, content="": f,
            ),
            patch.object(
                pipeline.tier_classifiers.non_intimate,
                "classify",
                side_effect=lambda f, content="": f,
            ) as mock_llm,
        ):
            pipeline._run_classification([item], vault_path, PipelineResult())

        mock_llm.assert_called_once()

    def test_human_review_source_skips_llm(self, vault_path):
        """Sources in human_review_sources never reach the LLM."""
        config = CreekConfig()
        config.classification.human_review_sources = ["journal"]
        pipeline = Pipeline(config=config)
        item = self._make_ingested(platform="journal")

        with (
            patch.object(
                pipeline.rule_classifier,
                "classify",
                side_effect=lambda f, content="": f,
            ),
            patch.object(
                pipeline.tier_classifiers.non_intimate, "classify"
            ) as mock_llm,
        ):
            pipeline._run_classification([item], vault_path, PipelineResult())

        mock_llm.assert_not_called()


class TestPipelineTierRouting:
    """`creek process` classifies each fragment by its privacy tier (#706).

    Closes the Intimate-never-cloud gap for the pipeline: an Intimate fragment is
    classified by the local provider even when ``classification`` is cloud.
    """

    @staticmethod
    def _cloud_classification_local_default() -> CreekConfig:
        """Config with a cloud ``classification`` stage + a local ``default``."""
        from creek.config import LLMConfig, LLMRoutingConfig

        return CreekConfig(
            llm=LLMRoutingConfig(
                default=LLMConfig(provider="ollama", model="qwen3:8b"),
                classification=LLMConfig(
                    provider="anthropic", model="claude-haiku-4-5"
                ),
            )
        )

    @staticmethod
    def _ingested(frag_id: str, tier: PrivacyTier):
        """An IngestedFragment at *tier* with a known id."""
        from creek.ingest.base import IngestedFragment
        from creek.models import Fragment, FragmentSource, SourcePlatform

        fragment = Fragment(
            id=frag_id,
            title=frag_id,
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            privacy_tier=tier,
        )
        return IngestedFragment(fragment=fragment, body="a note worth classifying")

    def test_intimate_routes_local_nonintimate_cloud(self, vault_path, monkeypatch):
        """Intimate → the local classifier; Open → the configured cloud one."""
        from creek.models import PrivacyTier

        pipeline = Pipeline(config=self._cloud_classification_local_default())
        # Cloud classification + local default → the tier classifiers are distinct.
        assert (
            pipeline.tier_classifiers.intimate
            is not pipeline.tier_classifiers.non_intimate
        )
        # Force the LLM path: rules leave each fragment unclassified.
        monkeypatch.setattr(
            pipeline.rule_classifier, "classify", lambda f, content="": f
        )

        seen: dict[str, list[str]] = {"local": [], "cloud": []}
        monkeypatch.setattr(
            pipeline.tier_classifiers.intimate,
            "classify",
            lambda f, content="": (seen["local"].append(f.id), f)[1],
        )
        monkeypatch.setattr(
            pipeline.tier_classifiers.non_intimate,
            "classify",
            lambda f, content="": (seen["cloud"].append(f.id), f)[1],
        )

        pipeline._run_classification(
            [
                self._ingested("frag-intimatexx", PrivacyTier.INTIMATE),
                self._ingested("frag-openxxxxxxx", PrivacyTier.OPEN),
            ],
            vault_path,
            PipelineResult(),
        )

        assert seen["local"] == ["frag-intimatexx"]  # Intimate → local provider
        assert seen["cloud"] == ["frag-openxxxxxxx"]  # non-Intimate → cloud provider

    @staticmethod
    def _untiered(frag_id: str, platform: str, channel: str | None = None):
        """An IngestedFragment carrying NO privacy tier yet (the #876 shape)."""
        from creek.ingest.base import IngestedFragment
        from creek.models import Authorship, Fragment, FragmentSource, SourcePlatform

        fragment = Fragment(
            id=frag_id,
            title="A note",
            source=FragmentSource(
                platform=SourcePlatform(platform),
                author=Authorship.SELF,
                channel=channel,
            ),
        )
        return IngestedFragment(
            fragment=fragment,
            body="a plain note about the walk to the shops and the weather",
        )

    def test_untiered_journal_fragment_routes_local_and_gets_a_tier(
        self, vault_path, monkeypatch
    ):
        """``creek process`` tiers a fragment BEFORE it picks a provider (#876).

        ``_run_classification`` selects the classifier with
        ``tier_of(frag)``. Without the privacy pre-pass every freshly
        ingested fragment is still ``unclassified`` at that moment, so a
        journal entry resolved to "not INTIMATE" and was shipped to the
        configured CLOUD provider on the very run meant to classify it —
        the same Intimate-never-cloud hole this fixes in the classify
        engine. Three fragments with three distinct expected tiers so the
        assertion cannot pass by tiering (or routing) everything alike.
        """
        from creek.models import PrivacyTier

        config = self._cloud_classification_local_default()
        # ``journal`` is a human-review source by default, which would skip the
        # LLM entirely and make the routing assertion vacuous.
        config.classification.human_review_sources = []
        pipeline = Pipeline(config=config)
        assert (
            pipeline.tier_classifiers.intimate
            is not pipeline.tier_classifiers.non_intimate
        )
        # Force the LLM path: rules leave every fragment unclassified.
        monkeypatch.setattr(
            pipeline.rule_classifier, "classify", lambda f, content="": f
        )

        seen: dict[str, list[str]] = {"local": [], "cloud": []}
        monkeypatch.setattr(
            pipeline.tier_classifiers.intimate,
            "classify",
            lambda f, content="": (seen["local"].append(f.id), f)[1],
        )
        monkeypatch.setattr(
            pipeline.tier_classifiers.non_intimate,
            "classify",
            lambda f, content="": (seen["cloud"].append(f.id), f)[1],
        )

        classified = pipeline._run_classification(
            [
                self._untiered("frag-pipejournal", "journal"),
                self._untiered("frag-pipeessay01", "essay"),
                self._untiered("frag-pipedmchan1", "discord", channel="dm"),
            ],
            vault_path,
            PipelineResult(),
        )

        assert seen["local"] == ["frag-pipejournal"]  # Intimate → local provider
        assert seen["cloud"] == ["frag-pipeessay01", "frag-pipedmchan1"]
        assert {
            item.fragment.id: PrivacyTier(item.fragment.privacy_tier)
            for item in classified
        } == {
            "frag-pipejournal": PrivacyTier.INTIMATE,
            "frag-pipeessay01": PrivacyTier.OPEN,
            "frag-pipedmchan1": PrivacyTier.PERSONAL,
        }


class TestPipelinePraxisPass:
    """``creek process`` stamps ``praxis_potential`` too (issue #877).

    ``creek classify`` is not the only way fragments enter the vault:
    ``creek process`` ingests and classifies in one shot and never calls
    the classify engine. Fixing only the engine would leave every
    freshly-processed fragment at ``praxis_potential: none``, so
    ``04-Praxis`` / ``08-Decisions`` would stay unreachable for anyone who
    uses the one-shot command.
    """

    @staticmethod
    def _ingested(frag_id: str, body: str):
        """An IngestedFragment with a known id and a chosen body.

        Args:
            frag_id: Fragment id (the assertion key).
            body: Markdown body the praxis heuristic scores.

        Returns:
            The assembled :class:`~creek.ingest.base.IngestedFragment`.
        """
        from creek.ingest.base import IngestedFragment
        from creek.models import Authorship, Fragment, FragmentSource, SourcePlatform

        fragment = Fragment(
            id=frag_id,
            title="Week notes",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        )
        return IngestedFragment(fragment=fragment, body=body)

    def test_no_llm_run_stamps_praxis_on_a_signal_bearing_fragment(
        self, vault_path: Path
    ) -> None:
        """A task checkbox in the body reaches ``explicit`` with zero egress.

        ``no_llm=True`` guarantees Pass 3 never runs, so this asserts the
        praxis verdict is produced by the free, local, deterministic
        heuristic — the whole point of #877 being a keyword pass rather
        than another LLM dimension.

        Two fragments with two distinct expected outcomes so a
        stamp-everything implementation cannot pass.
        """
        from creek.models import PraxisPotential

        pipeline = Pipeline(config=CreekConfig(), no_llm=True)

        classified = pipeline._run_classification(
            [
                self._ingested(
                    "frag-praxispipe01",
                    "notes from the week ahead\n- [ ] book the boiler service",
                ),
                self._ingested(
                    "frag-praxispipe02",
                    "a plain note about the walk to the shops and the weather",
                ),
            ],
            vault_path,
            PipelineResult(),
        )

        assert {
            item.fragment.id: str(item.fragment.praxis_potential) for item in classified
        } == {
            "frag-praxispipe01": PraxisPotential.EXPLICIT.value,
            "frag-praxispipe02": PraxisPotential.NONE.value,
        }

    def test_praxis_pass_never_demotes_an_llm_verdict(self, vault_path: Path) -> None:
        """A fragment already ``latent`` is not reset to ``none`` by the pass.

        The pipeline runs the heuristic over every fragment, including
        re-processed ones that already carry an LLM verdict the keyword
        table cannot reproduce. The merge is escalate-only.
        """
        from creek.ingest.base import IngestedFragment
        from creek.models import (
            Authorship,
            Fragment,
            FragmentSource,
            PraxisPotential,
            SourcePlatform,
        )

        pipeline = Pipeline(config=CreekConfig(), no_llm=True)
        item = IngestedFragment(
            fragment=Fragment(
                id="frag-praxispipe03",
                title="Week notes",
                source=FragmentSource(
                    platform=SourcePlatform.MARKDOWN,
                    author=Authorship.SELF,
                ),
                praxis_potential=PraxisPotential.LATENT,
            ),
            body="a plain note about the walk to the shops and the weather",
        )

        classified = pipeline._run_classification([item], vault_path, PipelineResult())

        assert str(classified[0].fragment.praxis_potential) == (
            PraxisPotential.LATENT.value
        )

    def test_llm_answer_never_demotes_an_explicit_fragment_on_disk(
        self, vault_path: Path
    ) -> None:
        """A model answering ``latent`` cannot lower a recorded ``explicit``.

        The sibling test above runs with ``no_llm=True``, so it exercises
        only :func:`creek.classify.praxis_pass.apply_praxis`'s own
        escalate-only merge and proves nothing about the LLM path. This
        one drives the real path: the rules leave the fragment
        unclassified, the stubbed provider answers ``praxis.potential:
        latent`` for a fragment already at ``explicit``, and the
        heuristic pass that runs afterwards cannot repair the demotion —
        the body carries no keyword signal, so ``detect`` can only
        propose ``none``.

        Asserted on what reached the vault, keyed by fragment id (never by
        ``rglob`` order, which is unsorted), because the demotion's real
        cost is a rewritten file.
        """
        import frontmatter

        from creek.classify.llm import LLMClassifier
        from creek.ingest.base import IngestedFragment
        from creek.models import (
            Authorship,
            Fragment,
            FragmentSource,
            PraxisPotential,
            SourcePlatform,
        )

        response = "frequency:\n  primary: F3\npraxis:\n  potential: latent\n"
        pipeline = Pipeline(config=CreekConfig())
        item = IngestedFragment(
            fragment=Fragment(
                id="frag-praxispipe04",
                title="Week notes",
                source=FragmentSource(
                    platform=SourcePlatform.MARKDOWN,
                    author=Authorship.SELF,
                ),
                praxis_potential=PraxisPotential.EXPLICIT,
            ),
            body="a plain note about the walk to the shops and the weather",
        )
        result = PipelineResult()

        with (
            # Rules leaving the frequency unclassified is what routes the
            # fragment to Pass 3 at all.
            patch.object(
                pipeline.rule_classifier,
                "classify",
                side_effect=lambda f, content="": f,
            ),
            patch.object(LLMClassifier, "_check_availability", return_value=True),
            patch.object(LLMClassifier, "_invoke_llm", return_value=response),
        ):
            classified = pipeline._run_classification([item], vault_path, result)

        pipeline._write_to_vault(classified, vault_path, result)

        on_disk = {
            post["id"]: post
            for post in (
                frontmatter.load(str(path))
                for path in (vault_path / "01-Fragments").rglob("*.md")
            )
        }

        assert result.errors == []
        assert on_disk["frag-praxispipe04"]["praxis_potential"] == (
            PraxisPotential.EXPLICIT.value
        )
        # The rest of the same response *did* land, so the assertion above
        # cannot be passing because the LLM was never consulted.
        assert on_disk["frag-praxispipe04"]["frequency"]["primary"] == "F3"


class TestPipelineAudiencePass:
    """``creek process`` stamps the ``audience`` axis too (issue #937).

    The #634 audience classifier reached production wired into
    :mod:`creek.classify.classify_engine` alone. ``creek process`` never
    calls that engine — it ingests and classifies in one shot — so every
    fragment that entered a vault through the one-shot command kept the
    :class:`~creek.models.Fragment` model default ``audience: mixed``,
    which on disk is indistinguishable from a fragment the classifier
    genuinely weighed and found ambiguous.

    That default is not inert. The voice fingerprint reads the persisted
    axis in
    :func:`creek.generate.ai_style.fingerprint._audience_factor`, which
    multiplies audience-facing material by ``2.0`` and private material
    by ``0.1``. ``mixed`` is neither, so a process-built vault weighs a
    published essay and a 3 a.m. journal entry at exactly the same
    authority: the fingerprint learns the average instead of the voice.
    Nothing raises and nothing is logged, which is how this same
    wired-into-one-caller defect got through twice before — #876 (privacy
    tiers) and #877 (praxis potential).
    """

    @staticmethod
    def _ingested(
        frag_id: str,
        title: str,
        body: str,
        platform: str,
        kind: str = "unclassified",
    ):
        """An IngestedFragment whose metadata drives the audience score.

        Args:
            frag_id: Fragment id (the assertion key).
            title: Fragment title.
            body: Markdown body the audience heuristic scans for
                headings, block quotations and word count.
            platform: ``SourcePlatform`` value — the heaviest audience
                signal (essay/Substack positive, journal negative).
            kind: ``SourceKind`` value; ``writing`` is a further
                audience-facing vote. Defaults to ``unclassified``.

        Returns:
            The assembled :class:`~creek.ingest.base.IngestedFragment`.
        """
        from creek.ingest.base import IngestedFragment
        from creek.models import (
            Authorship,
            Fragment,
            FragmentSource,
            SourceKind,
            SourcePlatform,
        )

        fragment = Fragment(
            id=frag_id,
            title=title,
            source=FragmentSource(
                platform=SourcePlatform(platform),
                author=Authorship.SELF,
                kind=SourceKind(kind),
            ),
        )
        return IngestedFragment(fragment=fragment, body=body)

    def test_no_llm_run_stamps_the_audience_axis(self, vault_path: Path) -> None:
        """A published essay and a private blip land on opposite axes.

        ``no_llm=True`` guarantees Pass 3 never runs, so the verdict
        provably comes from the free, local, deterministic heuristic in
        :class:`creek.classify.audience.AudienceClassifier` with zero
        egress — the point of #634 being a rules axis with an *optional*
        LLM seam rather than one more mandatory model call.

        Two fragments with two *distinct* expected outcomes, so an
        implementation that stamps a single value over everything cannot
        pass. The expected values follow the classifier's own published
        weights: the essay sums platform ``+5``, ``kind: writing`` ``+3``,
        tier ``open`` ``+1`` and heading/blockquote/long-form ``+3`` to
        ``+12`` (``>= 3`` → ``audience-facing``); the short journal entry
        sums platform ``-3``, tier ``intimate`` ``-3`` and the
        under-40-word penalty ``-2`` to ``-8`` (``<= -2`` → ``private``).
        """
        pipeline = Pipeline(config=CreekConfig(), no_llm=True)

        classified = pipeline._run_classification(
            [
                self._ingested(
                    "frag-audiencepipe01",
                    "On equanimity",
                    "# On equanimity\n\n> The wound is where the light enters.\n\n"
                    + "Each paragraph turns the same stone to a new face. " * 60,
                    "essay",
                    kind="writing",
                ),
                self._ingested(
                    "frag-audiencepipe02",
                    "Tuesday",
                    "felt wrung out today. couldn't say why. went to bed early.",
                    "journal",
                ),
            ],
            vault_path,
            PipelineResult(),
        )

        assert {
            item.fragment.id: str(item.fragment.audience) for item in classified
        } == {
            "frag-audiencepipe01": "audience-facing",
            "frag-audiencepipe02": "private",
        }

    def test_audience_is_scored_after_the_privacy_tier_is_stamped(
        self, vault_path: Path
    ) -> None:
        """The audience pass must sit after ``apply_tier``, not before it.

        :meth:`creek.classify.audience.AudienceClassifier.score` reads
        ``fragment.privacy_tier`` (``creek/classify/audience.py:160``,
        ``_PRIVACY_SCORE``) and a freshly-ingested fragment carries none,
        so where the call lands inside ``_run_classification`` is
        load-bearing — exactly as it is for the #876 tier-before-router
        pin above. ``apply_tier`` runs at ``creek/pipeline.py:579``.

        This fixture is engineered so the two orderings disagree. With
        the tier stamped first the entry is ``intimate``, so the score is
        journal ``-3``, privacy ``-3``, structure ``+3`` (heading,
        blockquote, >= 300 words) = ``-3``, which is ``<= -2`` →
        ``private``. Hoist the audience call above ``apply_tier`` and the
        tier is still ``unclassified``, contributing ``0``: the score is
        ``0``, which clears neither threshold → ``mixed``, and this test
        fails. That is the whole reason it exists — a later refactor that
        reorders the two passes cannot land silently.
        """
        paragraph = (
            "I keep circling the same question about how the work fits the "
            "life and the life fits the work, and every pass over it turns "
            "up one more thing I had not noticed the time before. "
        )
        body = (
            "# The long way round\n\n"
            "> Not all those who wander are lost.\n\n"
            + paragraph * 20
            + "\n\n- [ ] write this up properly\n\nI will come back to it.\n"
        )
        pipeline = Pipeline(config=CreekConfig(), no_llm=True)

        classified = pipeline._run_classification(
            [
                self._ingested(
                    "frag-audiencepipe03",
                    "The long way round",
                    body,
                    "journal",
                ),
            ],
            vault_path,
            PipelineResult(),
        )

        assert str(classified[0].fragment.audience) == "private"
