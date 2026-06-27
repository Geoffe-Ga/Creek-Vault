"""Skeleton ``creek sync`` command — dry-run plan + state round-trip (#676).

Tier A is the cheap per-source pass (pull → incremental ingest → rules
classify); Tier B is the nightly global pass (LLM classify → link → index).
This skeleton only echoes the ordered plan and round-trips
``00-Creek-Meta/State/sync/last-run.json`` — no real execution.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app
from creek.config import SyncConfig
from creek.pipeline import resolve_tier_a_plan, resolve_tier_b_plan

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a vault root with the meta dir."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    return vault


def _state(vault: Path) -> dict[str, object]:
    """Load the sync last-run state file."""
    path = vault / "00-Creek-Meta" / "State" / "sync" / "last-run.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---- Plan resolvers (unit) ---------------------------------------------


class TestPlanResolvers:
    """The tier plans are ordered and route sources to ingest types."""

    def test_tier_a_plan_order_and_routing(self) -> None:
        """Tier A is pull -> ingest --type <t> --since -> rules classify."""
        plan = resolve_tier_a_plan("journal")
        assert plan[0] == "pull"
        assert plan[1] == "ingest --type markdown --since <ledger>"
        assert plan[2] == "classify --method rules"
        assert "link" not in " ".join(plan)

    def test_tier_a_unknown_source_routes_to_itself(self) -> None:
        """An unmapped source name is used as its own ingest type."""
        assert "ingest --type custom --since <ledger>" in resolve_tier_a_plan("custom")

    def test_tier_b_plan_is_global_order(self) -> None:
        """Tier B is the nightly global llm-classify -> link -> index."""
        assert resolve_tier_b_plan() == ["classify --method llm", "link", "index"]


# ---- SyncConfig ---------------------------------------------------------


class TestSyncConfig:
    """Per-source toggles + cadence."""

    def test_enabled_sources_sorted_and_filtered(self) -> None:
        """Only enabled sources are returned, in sorted order."""
        cfg = SyncConfig(sources={"journal": True, "gdrive": False, "discord": True})
        assert cfg.enabled_sources() == ["discord", "journal"]

    def test_default_cadence(self) -> None:
        """The default cadence matches decision #5 (30 min / 03:00)."""
        cfg = SyncConfig()
        assert cfg.tier_a_interval_minutes == 30
        assert cfg.tier_b_hour == 3
        assert cfg.sources["journal"] is True
        assert cfg.sources["discord"] is False


# ---- CLI: creek sync ----------------------------------------------------


class TestSyncCommand:
    """The dry-run command echoes the plan and round-trips run state."""

    def test_tier_a_plan_and_state_roundtrip(self, tmp_path: Path) -> None:
        """Tier A resolves the cheap per-source sequence; state records tier A."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(
            app, ["sync", "--tier", "A", "--dry-run", "--vault", str(vault)]
        )
        assert result.exit_code == 0, result.output
        assert "classify --method rules" in result.output
        assert "ingest --type markdown" in result.output  # journal -> markdown
        assert "link" not in result.output
        assert "index" not in result.output
        assert _state(vault)["tier"] == "A"

    def test_tier_b_global_plan(self, tmp_path: Path) -> None:
        """Tier B prints the global passes and records tier B."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(
            app, ["sync", "--tier", "B", "--dry-run", "--vault", str(vault)]
        )
        assert result.exit_code == 0, result.output
        assert "classify --method llm" in result.output
        assert "link" in result.output
        assert "index" in result.output
        assert _state(vault)["tier"] == "B"

    def test_tier_a_lists_enabled_sources_only(self, tmp_path: Path) -> None:
        """Default-enabled journal+gdrive appear; off-by-default discord does not."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(
            app, ["sync", "--tier", "A", "--dry-run", "--vault", str(vault)]
        )
        assert result.exit_code == 0, result.output
        assert "journal" in result.output
        assert "gdrive" in result.output
        assert "ingest --type discord" not in result.output

    def test_source_filter_limits_to_one(self, tmp_path: Path) -> None:
        """--source limits Tier A to a single source."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(
            app,
            [
                "sync",
                "--tier",
                "A",
                "--source",
                "journal",
                "--dry-run",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "ingest --type markdown" in result.output
        assert "ingest --type gdrive" not in result.output

    def test_invalid_tier_errors(self, tmp_path: Path) -> None:
        """An unknown tier exits non-zero."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(app, ["sync", "--tier", "Z", "--vault", str(vault)])
        assert result.exit_code != 0
        assert "tier" in result.output.lower()

    def test_unknown_source_errors(self, tmp_path: Path) -> None:
        """An unknown --source exits non-zero with a source-specific message."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(
            app, ["sync", "--tier", "A", "--source", "nope", "--vault", str(vault)]
        )
        assert result.exit_code != 0
        assert "source" in result.output.lower()

    def test_tier_b_with_source_errors(self, tmp_path: Path) -> None:
        """--source is rejected with Tier B (it is a Tier-A concept)."""
        vault = _make_vault(tmp_path)
        result = runner.invoke(
            app,
            ["sync", "--tier", "B", "--source", "journal", "--vault", str(vault)],
        )
        assert result.exit_code != 0
        assert "source" in result.output.lower()

    def test_second_run_reports_previous(self, tmp_path: Path) -> None:
        """A second run echoes the prior recorded run."""
        vault = _make_vault(tmp_path)
        runner.invoke(app, ["sync", "--tier", "A", "--dry-run", "--vault", str(vault)])
        result = runner.invoke(
            app, ["sync", "--tier", "B", "--dry-run", "--vault", str(vault)]
        )
        assert result.exit_code == 0, result.output
        assert "previous run" in result.output.lower()
        assert _state(vault)["tier"] == "B"
