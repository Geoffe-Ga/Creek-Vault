"""Tests for the deterministic-first pipeline (FEAT-005).

Covers:

* The ``no_llm`` flag short-circuits the LLM classifier — Pass 3 is
  skipped entirely.
* ``PipelineResult`` records the per-pass yield counts (deterministic /
  local-model / residue) on every run.
* Even when ``--no-llm`` collides with ``LLMConfig.provider: anthropic``,
  the flag wins (regression guard from the FEAT test plan).
* Each ``Pipeline.run`` writes one JSONL line to
  ``00-Creek-Meta/Processing-Log/run-summary.jsonl``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from creek.config import CreekConfig
from creek.ingest.base import IngestedFragment, IngestResult, ParsedFragment
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.pipeline import Pipeline, PipelineResult

if TYPE_CHECKING:
    from pathlib import Path


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
    """Create a minimal Obsidian vault structure under ``tmp_path``."""
    vault = tmp_path / "vault"
    for d in VAULT_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


@pytest.fixture()
def source_path(tmp_path: Path) -> Path:
    """Create a source directory with sample markdown files."""
    src = tmp_path / "source"
    src.mkdir()
    (src / "note1.md").write_text("# A note\n\nSome content.")
    return src


def _make_ingested(title: str = "x", platform: str = "markdown") -> IngestedFragment:
    """Return a single ``IngestedFragment`` for direct stage testing."""
    fragment = Fragment(
        id=f"frag-{title:>014}"[-19:],
        title=title,
        source=FragmentSource(platform=SourcePlatform(platform)),
    )
    return IngestedFragment(fragment=fragment, body="body text")


class TestNoLLMFlagSkipsLLMClassifier:
    """``no_llm=True`` must prevent any call to ``llm_classifier.classify``."""

    def test_no_llm_skips_llm_even_when_residue_present(
        self,
        vault_path: Path,
    ) -> None:
        """A fragment the rules left unclassified is not sent to the LLM."""
        config = CreekConfig()
        pipeline = Pipeline(config=config, no_llm=True)
        item = _make_ingested()

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

    def test_default_pipeline_still_invokes_llm_for_residue(
        self,
        vault_path: Path,
    ) -> None:
        """Without ``no_llm``, residue still flows to the LLM (no regression)."""
        config = CreekConfig()
        pipeline = Pipeline(config=config)  # no_llm defaults to False
        item = _make_ingested()

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


class TestNoLLMOverridesAnthropicProvider:
    """Regression: ``--no-llm`` wins even with ``LLM_PROVIDER=anthropic``."""

    def test_no_llm_wins_over_anthropic_provider(self, vault_path: Path) -> None:
        """An Anthropic-configured pipeline still skips Pass 3 with no_llm."""
        config = CreekConfig()
        config.llm.default.provider = "anthropic"
        pipeline = Pipeline(config=config, no_llm=True)
        item = _make_ingested()

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


class TestPipelineResultYieldCounts:
    """``PipelineResult`` must expose per-pass counts."""

    def test_yield_counts_default_to_zero(self) -> None:
        """A fresh result records zero on every yield field."""
        result = PipelineResult()
        assert result.deterministic_classified == 0
        assert result.local_model_processed == 0
        assert result.residue == 0

    def test_high_confidence_counts_as_deterministic(
        self,
        vault_path: Path,
    ) -> None:
        """A confidently rule-classified fragment lands in deterministic."""
        config = CreekConfig()
        pipeline = Pipeline(config=config, no_llm=True)
        item = _make_ingested()
        result = PipelineResult()

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
        ):
            pipeline._run_classification([item], vault_path, result)

        assert result.deterministic_classified == 1
        assert result.residue == 0

    def test_low_confidence_counts_as_residue(self, vault_path: Path) -> None:
        """A low-confidence rule-classified fragment is residue."""
        config = CreekConfig()
        pipeline = Pipeline(config=config, no_llm=True)
        item = _make_ingested()
        result = PipelineResult()

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
        ):
            pipeline._run_classification([item], vault_path, result)

        assert result.deterministic_classified == 0
        assert result.residue == 1


def _stub_ingestor_class(ingest_result: IngestResult) -> type:
    """Build a real ingestor *class* whose ``ingest`` returns *ingest_result*.

    A class, not a ``MagicMock()`` standing in for one. ``INGESTOR_REGISTRY``
    has always been declared ``dict[str, type[Ingestor]]``, and #1517's
    ``build_ingestor`` now asks ``issubclass(entry, ImageIngestor)`` of every
    entry so it can hand the OCR block to the one ingestor that reads pixels.
    A mock *instance* cannot answer ``issubclass``.

    Args:
        ingest_result: The canned result every instance returns.

    Returns:
        A zero-argument-constructible ingestor class.
    """

    class _StubIngestor:
        """Minimal ingestor returning one canned result."""

        def ingest(self, source_path: Path) -> IngestResult:
            """Return the canned result, ignoring *source_path*."""
            del source_path
            return ingest_result

    return _StubIngestor


class TestRunSummaryEmitted:
    """``Pipeline.run`` must persist a yield-summary JSONL line."""

    def _make_mock_ingestor_registry(self, source_path: Path) -> dict[str, type]:
        """Build a registry that returns one ``ParsedFragment`` for tests."""
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
        return {"mock": _stub_ingestor_class(ingest_result)}

    def test_run_writes_run_summary_jsonl(
        self,
        vault_path: Path,
        source_path: Path,
    ) -> None:
        """``run()`` appends a single JSONL line to ``run-summary.jsonl``."""
        config = CreekConfig()
        pipeline = Pipeline(config=config, no_llm=True)
        registry = self._make_mock_ingestor_registry(source_path)

        with patch("creek.pipeline.INGESTOR_REGISTRY", registry):
            pipeline.run(source_path=source_path, vault_path=vault_path)

        log = vault_path / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
        assert log.exists()
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["no_llm"] is True
        assert "deterministic_classified" in entry
        assert "local_model_processed" in entry
        assert "residue" in entry


class TestCLINoLLMFlag:
    """``creek process --no-llm`` wires the flag through to the pipeline."""

    def test_cli_no_llm_renders_yield_line(
        self,
        vault_path: Path,
        source_path: Path,
    ) -> None:
        """The CLI prints the documented Deterministic/Local-model/Residue line."""
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
                "--no-llm",
            ],
        )
        assert result.exit_code == 0
        # Rich may soft-wrap long lines, so collapse whitespace before
        # asserting on the documented yield-line wording.
        normalized = " ".join(result.output.split())
        assert "Deterministic:" in normalized
        assert "Local-model:" in normalized
        assert "Residue:" in normalized
        assert "would go to LLM if Pass-3 enabled" in normalized

    def test_cli_no_llm_writes_run_summary(
        self,
        vault_path: Path,
        source_path: Path,
    ) -> None:
        """The CLI records the ``no_llm: true`` flag in the persisted summary."""
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
                "--no-llm",
            ],
        )
        assert result.exit_code == 0
        log = vault_path / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["no_llm"] is True


class TestNoLLMNoNetworkEgress:
    """A ``--no-llm`` pipeline run must not open any outbound socket."""

    @pytest.mark.integration
    def test_no_llm_run_makes_no_outbound_connections(
        self,
        vault_path: Path,
        source_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Patch ``socket.socket.connect`` to fail; ``--no-llm`` must not trip it.

        This is the load-bearing privacy assertion of FEAT-005: with the
        flag set, the pipeline must complete a normal run without ever
        opening a network socket (no Ollama health check, no Anthropic
        call, no Whisper download). Any attempted ``connect`` raises and
        fails the test.
        """
        import socket

        class NetworkBlockedError(RuntimeError):
            """Raised when --no-llm code attempts a network connection."""

        def _blocked(*args: object, **kwargs: object) -> None:
            msg = "network egress blocked under --no-llm"
            raise NetworkBlockedError(msg)

        monkeypatch.setattr(socket.socket, "connect", _blocked)
        monkeypatch.setattr(socket.socket, "connect_ex", _blocked)

        # Even the Anthropic provider should be a no-op under --no-llm:
        # set the provider so a regression that ignores the flag would
        # surface as a NetworkBlockedError on first availability check.
        config = CreekConfig()
        config.llm.default.provider = "anthropic"
        pipeline = Pipeline(config=config, no_llm=True)
        result = pipeline.run(source_path=source_path, vault_path=vault_path)

        # No raise => no socket touched. Sanity-check the run completed.
        assert isinstance(result, PipelineResult)
