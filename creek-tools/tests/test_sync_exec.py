"""Real Tier-A / Tier-B execution for ``creek sync`` (#678).

Tier A runs, per enabled source, pull -> incremental ingest -> rules classify
and NEVER links/indexes (R6). Tier B runs the global llm-classify -> link ->
index. ``--dry-run`` still only echoes the plan. Because every step is
idempotent, a re-run produces no duplicates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import (
    _sync_classify,
    _sync_index,
    _sync_ingest_source,
    _sync_link,
    _sync_pull,
    app,
)
from creek.config import CreekConfig, SyncConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _fake_config(
    vault: Path,
    source_drive: Path,
    sources: dict[str, bool],
) -> CreekConfig:
    """Build a config with the given vault, source drive, and sync toggles."""
    return CreekConfig(
        vault_path=vault,
        source_drive=source_drive,
        sync=SyncConfig(sources=sources),
    )


def _spy_steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the sync step helpers with recorders; return the call log."""
    calls: list[str] = []
    monkeypatch.setattr(
        "creek.cli._sync_pull", lambda s, _c, _v: calls.append(f"pull:{s}")
    )
    monkeypatch.setattr(
        "creek.cli._sync_ingest_source", lambda s, _c, _v: calls.append(f"ingest:{s}")
    )
    monkeypatch.setattr(
        "creek.cli._sync_classify", lambda _v, _c, m: calls.append(f"classify:{m}")
    )
    monkeypatch.setattr("creek.cli._sync_link", lambda _v, _c: calls.append("link"))
    monkeypatch.setattr("creek.cli._sync_index", lambda _v: calls.append("index"))
    return calls


# ---- Orchestration (spied steps) ---------------------------------------


class TestTierOrchestration:
    """Tier runners call the right steps in order, honouring R6."""

    def test_tier_a_runs_cheap_chain_and_never_links(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier A is pull -> ingest -> rules-classify; no link/index (R6)."""
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        result = runner.invoke(
            app, ["sync", "--tier", "A", "--vault", str(tmp_path / "v")]
        )
        assert result.exit_code == 0, result.output
        assert calls == ["pull:fakesrc", "ingest:fakesrc", "classify:rules"]
        assert "link" not in calls
        assert "index" not in calls

    def test_tier_b_runs_global_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier B is llm-classify -> link -> index."""
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        result = runner.invoke(
            app, ["sync", "--tier", "B", "--vault", str(tmp_path / "v")]
        )
        assert result.exit_code == 0, result.output
        assert calls == ["classify:llm", "link", "index"]

    def test_disabled_source_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A toggled-off source is never pulled or ingested."""
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(
            tmp_path / "v", tmp_path / "src", {"journal": True, "discord": False}
        )
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        runner.invoke(app, ["sync", "--tier", "A", "--vault", str(tmp_path / "v")])
        assert "pull:journal" in calls
        assert "pull:discord" not in calls

    def test_dry_run_does_not_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run echoes the plan and runs no step helpers."""
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        runner.invoke(
            app, ["sync", "--tier", "A", "--dry-run", "--vault", str(tmp_path / "v")]
        )
        assert calls == []


# ---- Step helper delegation --------------------------------------------


class TestStepHelpers:
    """Each step helper delegates to the right engine entry point."""

    def test_classify_delegates_with_method(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_sync_classify calls run_classify with the given method."""
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "creek.classify.classify_engine.run_classify",
            lambda **kw: seen.update(kw),
        )
        _sync_classify(tmp_path, _fake_config(tmp_path, tmp_path, {}), "rules")
        assert seen["method"] == "rules"
        assert seen["force"] is False

    def test_link_delegates_embeddings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_sync_link calls run_link with the embeddings method."""
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "creek.link.link_engine.run_link", lambda **kw: seen.update(kw)
        )
        _sync_link(tmp_path, _fake_config(tmp_path, tmp_path, {}))
        assert seen["method"] == "embeddings"

    def test_index_delegates_generate_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_sync_index builds an IndexGenerator and generates all notes."""
        log: list[object] = []

        class _FakeIG:
            def __init__(self, vault_path: Path) -> None:
                log.append(vault_path)

            def generate_all(self) -> list[Path]:
                log.append("generate_all")
                return []

        monkeypatch.setattr("creek.generate.indexes.IndexGenerator", _FakeIG)
        _sync_index(tmp_path)
        assert "generate_all" in log

    def test_pull_non_gdrive_is_noop(self, tmp_path: Path) -> None:
        """A local source has no pull step."""
        _sync_pull("journal", _fake_config(tmp_path, tmp_path, {}), tmp_path)

    def test_pull_gdrive_unavailable_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gdrive pull skips cleanly when the optional libs are unavailable."""
        from creek.ingest.gdrive import GoogleApiDriveClient

        monkeypatch.setattr(GoogleApiDriveClient, "is_available", lambda _self: False)
        _sync_pull("gdrive", _fake_config(tmp_path, tmp_path, {}), tmp_path)

    def test_ingest_source_gdrive_is_noop(self, tmp_path: Path) -> None:
        """gdrive is a downloader, not an ingestor — ingest step no-ops."""
        _sync_ingest_source("gdrive", _fake_config(tmp_path, tmp_path, {}), tmp_path)

    def test_ingest_source_journal_runs_incremental(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journal source ingests incrementally from its source path."""
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "creek.cli._run_ingest", lambda **kw: seen.update(kw) or (0, [], 0)
        )
        src = tmp_path / "src"
        (src / "personal" / "journal").mkdir(parents=True)
        cfg = _fake_config(tmp_path / "v", src, {"journal": True})
        _sync_ingest_source("journal", cfg, tmp_path / "v")
        assert seen["incremental"] is True
        assert seen["source_type"] == "markdown"


# ---- Real Tier-A idempotency -------------------------------------------


class TestTierAIdempotent:
    """A real Tier-A run (offline: ingest + rules-classify) is idempotent."""

    def test_rerun_produces_no_duplicates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running Tier A on unchanged journal source writes no duplicate."""
        vault = tmp_path / "vault"
        for d in ("00-Creek-Meta/Processing-Log", "01-Fragments/Journal"):
            (vault / d).mkdir(parents=True, exist_ok=True)
        journal = tmp_path / "src" / "personal" / "journal"
        journal.mkdir(parents=True)
        (journal / "day.md").write_text(
            "---\ndate: 2026-06-26\n---\nA journal entry today.\n", encoding="utf-8"
        )
        cfg = _fake_config(vault, tmp_path / "src", {"journal": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        r1 = runner.invoke(app, ["sync", "--tier", "A", "--vault", str(vault)])
        assert r1.exit_code == 0, r1.output
        before = len(list((vault / "01-Fragments").rglob("*.md")))
        assert before == 1

        r2 = runner.invoke(app, ["sync", "--tier", "A", "--vault", str(vault)])
        assert r2.exit_code == 0, r2.output
        after = len(list((vault / "01-Fragments").rglob("*.md")))
        assert after == before  # idempotent self-heal, no duplicates
