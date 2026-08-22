"""Real Tier-A / Tier-B execution for ``creek sync`` (#678).

Tier A runs, per enabled source, pull -> incremental ingest -> rules classify
and NEVER links/indexes (R6). Tier B runs the global llm-classify -> link ->
index. ``--dry-run`` still only echoes the plan. Because every step is
idempotent, a re-run produces no duplicates.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from typer.testing import CliRunner

from creek.cli import (
    SourceRunRecord,
    _sync_classify,
    _sync_index,
    _sync_ingest_source,
    _sync_link,
    _sync_pull,
    _sync_source_input,
    _sync_state_path,
    app,
)
from creek.config import CreekConfig, SyncConfig
from creek.ingest.gdrive import DownloadResult, DriveFile, GoogleApiDriveClient
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# The Unicode Box Drawing block — the glyphs Rich uses to frame the options
# panel in ``--help`` output. Matched as a range so a Rich border-set change
# cannot quietly reintroduce the wrapping brittleness below.
_BOX_DRAWING_RE = re.compile("[─-╿]")

# "3 ... fragment" (or "fragment ... 3") with no other digit in between, so a
# count that lands nowhere near the noun cannot satisfy the assertion.
_COUNT_THEN_NOUN_RE = re.compile(r"\b3\b\D{0,60}?fragments?\b", re.IGNORECASE)
_NOUN_THEN_COUNT_RE = re.compile(r"fragments?\b\D{0,60}?\b3\b", re.IGNORECASE)


def _flatten(text: str) -> str:
    """Return *text* with ANSI codes, Rich panel borders and wrapping removed.

    Rich frames ``--help`` in a box and soft-wraps long cells, so a literal
    phrase under test can arrive split across lines with ``│`` borders and
    padding in between. Dropping the box-drawing range and collapsing runs of
    whitespace rejoins the sentence, which matters most for the *negative*
    assertions here: an un-flattened haystack would let "current default"
    survive in the help simply by wrapping, and the test would pass anyway.

    Args:
        text: Raw captured CLI output.

    Returns:
        The single-spaced, escape-free, border-free form of *text*.
    """
    return " ".join(_BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", text)).split())


def _sync_help_option_cell(option: str) -> str:
    """Return the flattened ``creek sync --help`` help cell for *option*.

    Isolates one row of Rich's options panel — the option name plus any
    wrapped continuation lines, stopping at the next row (which always carries
    a ``--`` flag) — so an assertion about *this* option's wording cannot be
    satisfied or defeated by a neighbouring option's text.

    Args:
        option: The long flag whose help cell to extract (e.g. ``--dry-run``).

    Returns:
        The single-spaced help row, or ``""`` when the option is absent.
    """
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0, result.output
    cleaned = _BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", result.output))
    row: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if row:
            if not stripped or "--" in stripped:
                break
            row.append(stripped)
        elif stripped.startswith(option):
            row.append(stripped)
    return " ".join(" ".join(row).split())


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
        "creek.cli._sync_pull", lambda s, _c, _v: calls.append(f"pull:{s}") or ()
    )
    # ``list.append`` returns ``None``, which ``_sync_run_tier_a`` stored as the
    # ingested count — so under this spy ``last-run.json`` was already writing
    # ``"ingested": null``. Returning the real record shape corrects that.
    monkeypatch.setattr(
        "creek.cli._sync_ingest_source",
        lambda s, _c, _v: calls.append(f"ingest:{s}") or SourceRunRecord(),
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

    def test_explicit_source_overrides_disabled_toggle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit --source runs even when that source is toggled off."""
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"discord": False})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        result = runner.invoke(
            app,
            [
                "sync",
                "--tier",
                "A",
                "--source",
                "discord",
                "--vault",
                str(tmp_path / "v"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == ["pull:discord", "ingest:discord", "classify:rules"]


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
        assert seen["vault_path"] == tmp_path / "v"

    def test_source_input_resolution(self, tmp_path: Path) -> None:
        """_sync_source_input joins source_drive + the configured relative path."""
        src = tmp_path / "src"
        cfg = _fake_config(tmp_path / "v", src, {})
        assert _sync_source_input("journal", cfg) == src / "personal" / "journal"
        # A name that is not a SourcePaths attribute resolves to None.
        assert _sync_source_input("not_a_real_source", cfg) is None


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


# ---- The advertised default must match the real one (#1335) -------------


class TestSyncDefaultTruthfulness:
    """``creek sync --help`` must describe the default the CLI actually has."""

    def test_sync_help_and_default_cannot_drift_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runtime default and the ``--dry-run`` help text move together (#1335).

        Two halves, deliberately in one test so neither can be "fixed" alone:

        * **Behaviour.** A bare ``sync --tier B`` reaches the LLM classifier —
          the same chain ``TestTierOrchestration::test_tier_b_runs_global_chain``
          pins in full; asserted here only as the anchor the help text is
          measured against, not as a second copy of that assertion.
        * **Wording.** The help must not call ``--dry-run`` the default (the
          shipped string said ``(current default)`` while the option defaulted
          to ``False``), and must say the bare run executes.

        A help-string-only assertion would be vacuous: it would still pass if
        someone made ``--dry-run`` default to ``True``, at which point the
        rewritten help would be the lie instead. The behavioural half is what
        catches that inversion, and pairing them means a future edit to either
        the string or the default fails until the other agrees.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)

        run = runner.invoke(
            app, ["sync", "--tier", "B", "--vault", str(tmp_path / "v")]
        )
        assert run.exit_code == 0, run.output
        assert "classify:llm" in calls, "the bare default must really execute"

        help_result = runner.invoke(app, ["sync", "--help"])
        assert help_result.exit_code == 0, help_result.output
        flat = _flatten(help_result.output)
        low = flat.lower()
        assert "(current default)" not in low, flat
        assert "current default" not in low, flat
        assert "executes" in low, flat

        cell = _sync_help_option_cell("--dry-run")
        assert "--dry-run" in cell, "the --dry-run help row was not found"
        assert "default" not in cell.lower(), cell


# ---- Tier-B confirmation gate (#1335) -----------------------------------


class TestTierBConfirmationGate:
    """A real Tier-B pass is confirmed interactively and never in a schedule."""

    def test_interactive_tier_b_declined_executes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answering ``n`` at the Tier-B prompt aborts before any step runs.

        Catches a gate that prompts but ignores the answer, or one placed after
        ``_sync_dispatch`` — either way the LLM classifier would already have
        billed the operator by the time they said no.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app,
            ["sync", "--tier", "B", "--vault", str(tmp_path / "v")],
            input="n\n",
        )

        assert calls == [], result.output
        assert result.exit_code != 0, result.output
        assert "aborted" in _flatten(result.output).lower(), result.output

    def test_interactive_tier_b_confirmed_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answering ``y`` runs the full Tier-B chain, after a cost warning.

        Catches a gate that refuses to proceed on a yes, and a gate that runs
        the chain without ever telling the operator what it costs: the output
        must name the LLM before the chain runs.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app,
            ["sync", "--tier", "B", "--vault", str(tmp_path / "v")],
            input="y\n",
        )

        assert result.exit_code == 0, result.output
        assert calls == ["classify:llm", "link", "index"]
        assert "llm" in _flatten(result.output).lower(), result.output

    def test_non_interactive_tier_b_runs_without_prompting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-TTY Tier-B run executes unprompted — the schedules depend on it.

        ``creek/sync/schedule.py`` renders every launchd plist, systemd unit and
        crontab line as a bare ``creek sync --tier B --vault "<path>"`` with no
        ``--yes`` and no stdin. If the confirmation is not TTY-gated, every one
        of those units blocks on a prompt nobody can answer (here: EOF, a
        non-zero exit and an empty call log), and the product silently stops
        syncing. This test is that contract; it is the one that must keep
        passing when the gate lands.

        The non-TTY premise is asserted by monkeypatching ``_is_interactive``
        rather than inherited from ``CliRunner``: relying on the runner's stdin
        happening not to be a terminal would make the test's whole subject an
        accident of the harness, and a future runner change would turn this
        from a schedule guard into a second copy of the plain execution test.

        The cost line must still appear. A confirmation only reaches an
        operator at a keyboard, but the launchd/systemd log is the one place a
        nightly Tier-B pass is otherwise completely opaque, so the announcement
        is unconditional and the *skipped* confirmation is stated out loud.
        Without that, the TTY fork would be an invisible behavioural
        difference — the same species of undisclosed default this issue exists
        to remove.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: False)

        result = runner.invoke(
            app, ["sync", "--tier", "B", "--vault", str(tmp_path / "v")]
        )

        assert result.exit_code == 0, result.output
        assert calls == ["classify:llm", "link", "index"]
        flat = _flatten(result.output)
        assert "continue?" not in flat.lower(), result.output
        assert "llm" in flat.lower(), result.output
        assert "non-interactive" in flat.lower(), result.output

    def test_yes_flag_skips_the_prompt_when_interactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--yes`` runs Tier B at a TTY with no stdin to answer the prompt.

        No input is supplied, so a gate that ignores ``--yes`` hits EOF and
        exits non-zero with an empty call log.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app, ["sync", "--tier", "B", "--yes", "--vault", str(tmp_path / "v")]
        )

        assert result.exit_code == 0, result.output
        assert calls == ["classify:llm", "link", "index"]

    def test_tier_a_is_not_gated_when_interactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier A is cheap, so it runs at a TTY with no confirmation.

        Catches a gate keyed on ``not dry_run`` alone rather than on the tier:
        with no input supplied, a gated Tier A would EOF instead of running its
        pull -> ingest -> rules chain.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app, ["sync", "--tier", "A", "--vault", str(tmp_path / "v")]
        )

        assert result.exit_code == 0, result.output
        assert calls == ["pull:fakesrc", "ingest:fakesrc", "classify:rules"]
        assert "continue?" not in _flatten(result.output).lower(), result.output

    def test_dry_run_tier_b_is_not_gated_when_interactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Tier-B plan costs nothing, so ``--dry-run`` is never confirmed.

        Catches a gate keyed on the tier alone rather than on ``not dry_run``:
        with no input supplied, a gated dry run would EOF and exit non-zero
        instead of echoing the plan.
        """
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(tmp_path / "v", tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app, ["sync", "--tier", "B", "--dry-run", "--vault", str(tmp_path / "v")]
        )

        assert result.exit_code == 0, result.output
        assert calls == []
        assert "continue?" not in _flatten(result.output).lower(), result.output

    def test_tier_b_prompt_states_the_measured_cost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prompt names how many fragments the run is about to bill for.

        Three fragments are seeded, so a gate that prints a generic warning —
        or that counts something other than the vault's fragments — fails. The
        measured cost is one LLM call per unclassified fragment, so the number
        the operator is shown has to come from the vault they are about to run
        against.
        """
        vault = tmp_path / "v"
        for frag_id in ("frag-a", "frag-b", "frag-c"):
            write_raw_fragment_file(vault, "01-Fragments/Journal", frag_id, frag_id)
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(vault, tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app, ["sync", "--tier", "B", "--vault", str(vault)], input="n\n"
        )

        assert calls == [], result.output
        assert result.exit_code != 0, result.output
        flat = _flatten(result.output)
        names_the_count = bool(
            _COUNT_THEN_NOUN_RE.search(flat) or _NOUN_THEN_COUNT_RE.search(flat)
        )
        assert names_the_count, flat
        assert "llm" in flat.lower(), flat

    def test_declined_tier_b_records_no_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A declined Tier B leaves ``last-run.json`` unwritten (#1335).

        ``_write_sync_state`` runs *after* the dispatch, so aborting at the
        gate skips it. That is currently an accident of statement order rather
        than a decision, and it is worth pinning: ``creek sync --status`` reads
        this file, and a run that was refused before it started must not be
        able to report itself as the vault's most recent sync. A future
        refactor that hoists the state write above the dispatch — a reasonable-
        looking change, since the file also records dry runs — would make the
        status table lie about work that never happened.
        """
        vault = tmp_path / "v"
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(vault, tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

        result = runner.invoke(
            app, ["sync", "--tier", "B", "--vault", str(vault)], input="n\n"
        )

        assert calls == [], result.output
        assert not _sync_state_path(vault).exists(), "a refused run recorded itself"

    def test_the_emitted_schedule_command_runs_unprompted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The argv this product writes into timers still executes Tier B (#1335).

        Every other test here hand-writes its argv, which means they all agree
        with each other and none of them agrees with
        :func:`~creek.sync.schedule._tier_args` — the function whose output is
        baked into the launchd plists, systemd units and crontab lines that
        ``creek sync --install-schedule`` installs on the operator's machine.
        Feeding that exact argv back through the CLI binds the two together, so
        a future change to either the emitted command or the CLI's default
        cannot silently desynchronise them.

        This is the concrete form of why the fix for this issue corrects the
        help text instead of inverting the default: the argv asserted below
        carries no ``--dry-run``, so making dry-run the default would turn
        every installed timer into a no-op that exits 0.
        """
        from creek.sync.schedule import _tier_args

        vault = tmp_path / "v"
        calls = _spy_steps(monkeypatch)
        cfg = _fake_config(vault, tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: False)

        argv = _tier_args(vault, "B")
        assert argv[0] == "creek", argv
        assert "--dry-run" not in argv, argv

        result = runner.invoke(app, argv[1:])

        assert result.exit_code == 0, result.output
        assert calls == ["classify:llm", "link", "index"]

    def test_provider_refusal_aborts_legibly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused LLM provider exits 1 with a message, not a traceback (#1335).

        ``creek classify --method llm`` catches
        :class:`~creek.classify.classify_engine.LLMProviderUnavailableError`
        and prints the provider's own remediation hint; the sync path did not,
        so a Tier-B tick with no cloud consent died with an unhandled traceback
        into a scheduler log. The exception carries the remediation text
        already, so this asserts the detail reaches the operator rather than
        re-checking consent here — restating the hint would be a second copy
        that could drift from the engine's.

        ``last-run.json`` must also stay unwritten: an aborted pass that
        recorded itself would make the next ``--status`` claim Tier B ran.
        """
        from creek.classify.classify_engine import LLMProviderUnavailableError

        vault = tmp_path / "v"
        _spy_steps(monkeypatch)
        cfg = _fake_config(vault, tmp_path / "src", {"fakesrc": True})
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        monkeypatch.setattr("creek.cli._is_interactive", lambda: False)

        def _refuse(_v: Path, _c: CreekConfig, _m: str) -> None:
            raise LLMProviderUnavailableError(
                provider="anthropic", detail="set CREEK_CLOUD_CONSENT=1"
            )

        monkeypatch.setattr("creek.cli._sync_classify", _refuse)

        result = runner.invoke(app, ["sync", "--tier", "B", "--vault", str(vault)])

        assert result.exit_code == 1, result.output
        flat = _flatten(result.output)
        assert "set CREEK_CLOUD_CONSENT=1" in flat, flat
        assert "Traceback" not in result.output, result.output
        assert not _sync_state_path(vault).exists(), "an aborted run recorded itself"


# ---- Tier-A advisory surfacing (#1372) ----------------------------------


def _lines_with(text: str, needle: str) -> list[str]:
    """Return the ANSI-free lines of *text* that contain *needle*.

    Presence somewhere in the whole capture is weaker than these tests need:
    an advisory that names a failure on one line and its source on another is
    unattributable in a scheduler log, and a flattened haystack cannot tell
    that apart from a properly attributed line. ``tests/conftest.py`` pins the
    terminal at 200 columns, comfortably wider than any line asserted through
    this helper, so Rich does not wrap them.

    Args:
        text: Raw captured CLI output.
        needle: Substring the returned lines must contain.

    Returns:
        Every matching line, in order; empty when there is no match.
    """
    return [line for line in _ANSI_RE.sub("", text).splitlines() if needle in line]


def _advisory_vault(tmp_path: Path) -> tuple[Path, Path]:
    """Scaffold the vault a real Tier-A pass writes into, plus its source root.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The ``(vault, source_drive)`` pair. The source drive is deliberately
        *not* created, so a test that needs a source directory makes exactly
        the one it means to — and the missing-path test gets a genuine miss.
    """
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta/Processing-Log", "01-Fragments"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault, tmp_path / "src"


def _drive_file(name: str) -> DriveFile:
    """Build a Drive listing entry named *name*.

    Every field is supplied because :class:`~creek.ingest.gdrive.DriveFile` is
    a frozen dataclass with no defaults; only ``name`` is load-bearing here —
    it is the one thing an operator can act on when a download fails.

    Args:
        name: The file name as Drive reports it.

    Returns:
        A fully populated listing entry.
    """
    return DriveFile(
        id=f"id-{name}",
        name=name,
        mime_type="application/octet-stream",
        modified_time=datetime(2026, 4, 1, tzinfo=UTC),
        size=1024,
        parent_path="",
    )


class _StubDriveDownloader:
    """A ``GoogleDriveDownloader`` stand-in that returns a canned result.

    ``_sync_pull`` imports the downloader *inside* the function body, so the
    patchable seam is the attribute on :mod:`creek.ingest.gdrive` rather than
    a name bound in :mod:`creek.cli`. The canned result lives on the class and
    is swapped per test by ``monkeypatch.setattr``, which restores it.
    """

    result: ClassVar[DownloadResult] = DownloadResult(downloaded=(), skipped=())

    def __init__(self, *, client: object, config: object) -> None:
        """Accept the real downloader's keyword-only construction and ignore it."""
        self._client = client
        self._config = config

    def download_all(self, _staging_dir: Path) -> DownloadResult:
        """Return the canned result instead of calling Drive."""
        return type(self).result


def _install_fake_drive(
    monkeypatch: pytest.MonkeyPatch,
    result: DownloadResult,
) -> None:
    """Make the Tier-A Drive pull yield *result* without touching the network.

    Args:
        monkeypatch: The test's patcher.
        result: The outcome ``download_all`` should report.
    """
    monkeypatch.setattr(GoogleApiDriveClient, "is_available", lambda _self: True)
    monkeypatch.setattr(_StubDriveDownloader, "result", result)
    monkeypatch.setattr(
        "creek.ingest.gdrive.GoogleDriveDownloader", _StubDriveDownloader
    )


def _state_sources(vault: Path) -> dict[str, dict[str, object]]:
    """Return the per-source block recorded in the vault's ``last-run.json``.

    Args:
        vault: Vault root whose sync state to read.

    Returns:
        The ``sources`` mapping, source name -> recorded entry.
    """
    state = json.loads(_sync_state_path(vault).read_text(encoding="utf-8"))
    return dict(state["sources"])


class TestTierAAdvisorySurfacing:
    """A Tier-A tick must name what it failed to fetch or parse (#1372).

    ``creek sync --tier A`` threw away every advisory its own steps produced:
    the :class:`~creek.ingest.gdrive.DownloadResult` from the Drive pull
    (called as a bare expression statement at cli.py:961) and both the error
    list and the discovered count from the ingest (bound to ``_errors`` /
    ``_discovered`` at cli.py:987). The entire visible output of a run that
    fetched nothing and parsed nothing was ``ran tier=A; wrote last-run.json``
    at exit 0 — byte-identical to a healthy tick with no new material.

    Every stdout assertion below reads ``result.stdout`` and never the
    combined capture: ``download_all`` logs a warning per failure
    (gdrive.py:1033) and Python's last-resort handler puts that on stderr, so
    an assertion against the merged stream would pass against the unfixed CLI
    and prove nothing.
    """

    def test_sync_names_the_file_drive_failed_to_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 403'd Drive file is named on stdout, attributed to its source.

        The filename is the payload: a failure *count* tells the operator that
        their vault is incomplete without telling them what is missing from
        it, which is barely better than the silence it replaces. The source
        name has to be on the same line, because a tick runs several sources
        and a bare filename in a scheduler log belongs to none of them.
        """
        vault, src = _advisory_vault(tmp_path)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"gdrive": True}),
        )
        _install_fake_drive(
            monkeypatch,
            DownloadResult(
                downloaded=(),
                skipped=(),
                errors=(
                    (
                        _drive_file("the-important-one.docx"),
                        RuntimeError("HTTP 403 quota exceeded"),
                    ),
                ),
            ),
        )

        result = runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "gdrive", "--vault", str(vault)],
        )

        flat = _flatten(result.stdout)
        assert "the-important-one.docx" in flat, result.stdout
        assert "HTTP 403 quota exceeded" in flat, result.stdout
        # Not ``[sync] gdrive: ...``: ``console`` has Rich markup enabled, so
        # the literal ``[sync]`` prefix every line in this command carries is
        # parsed as a style tag and never reaches stdout — the issue's own
        # capture of the unfixed run is " ran tier=A; wrote last-run.json".
        assert "gdrive: pull failed" in flat, result.stdout
        naming = _lines_with(result.stdout, "the-important-one.docx")
        assert naming, result.stdout
        assert all("gdrive" in line for line in naming), naming

    def test_a_partial_drive_failure_still_exits_zero_and_writes_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One 403 must not abort the tick — this is the regression fence.

        ``download_all`` records each per-file failure and keeps going by
        design (gdrive.py:1007-1012), precisely so a transient quota or
        network blip on file N does not abandon files N+1 onward. The way to
        break that from up here is to copy the abort precedent at
        cli.py:1323-1326, which raises ``typer.Exit`` and therefore skips the
        ``_write_sync_state`` call at cli.py:1419 — leaving ``--status``
        naming the *previous* tick as the most recent one indefinitely while
        the schedule keeps failing.

        This assertion is green both before and after the fix on purpose. It
        constrains the fix rather than demonstrating the bug: making failures
        loud must not also make them fatal.
        """
        vault, src = _advisory_vault(tmp_path)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"gdrive": True}),
        )
        _install_fake_drive(
            monkeypatch,
            DownloadResult(
                downloaded=(),
                skipped=(),
                errors=(
                    (
                        _drive_file("the-important-one.docx"),
                        RuntimeError("HTTP 403 quota exceeded"),
                    ),
                ),
            ),
        )

        result = runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "gdrive", "--vault", str(vault)],
        )

        assert result.exit_code == 0, result.output
        assert _sync_state_path(vault).exists(), result.output

    def test_the_state_file_records_the_drive_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``last-run.json`` carries the failure, not merely an ``ingested: 0``.

        Stdout belongs to whoever was watching the tick, and a launchd or
        systemd run has nobody. ``last-run.json`` is the only durable record,
        and ``{"ingested": 0}`` in it is indistinguishable from "nothing new
        arrived" — which is exactly how a Drive folder that 403s every thirty
        minutes reads today.
        """
        vault, src = _advisory_vault(tmp_path)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"gdrive": True}),
        )
        _install_fake_drive(
            monkeypatch,
            DownloadResult(
                downloaded=(),
                skipped=(),
                errors=(
                    (
                        _drive_file("the-important-one.docx"),
                        RuntimeError("HTTP 403 quota exceeded"),
                    ),
                ),
            ),
        )

        runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "gdrive", "--vault", str(vault)],
        )

        entry = _state_sources(vault)["gdrive"]
        assert entry["failure_count"] == 1, entry
        raw = _sync_state_path(vault).read_text(encoding="utf-8")
        assert "the-important-one.docx" in raw, raw

    def test_a_clean_rerun_clears_the_recorded_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A healed source stops reporting the failure it had last tick.

        ``_write_sync_state`` merges per-source entries across runs, so a
        failure list that is appended to rather than replaced would make one
        bad tick permanent: every later ``--status`` would keep showing a file
        that has long since downloaded fine, and the operator would learn to
        ignore the column.
        """
        vault, src = _advisory_vault(tmp_path)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"gdrive": True}),
        )
        argv = ["sync", "--tier", "A", "--source", "gdrive", "--vault", str(vault)]

        _install_fake_drive(
            monkeypatch,
            DownloadResult(
                downloaded=(),
                skipped=(),
                errors=((_drive_file("flaky.docx"), RuntimeError("HTTP 503")),),
            ),
        )
        first = runner.invoke(app, argv)
        assert first.exit_code == 0, first.output
        assert _state_sources(vault)["gdrive"]["failure_count"] == 1

        _install_fake_drive(monkeypatch, DownloadResult(downloaded=(), skipped=()))
        second = runner.invoke(app, argv)

        assert second.exit_code == 0, second.output
        entry = _state_sources(vault)["gdrive"]
        assert entry["failure_count"] == 0, entry
        assert entry["failures"] == [], entry

    def test_recorded_failures_are_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twelve failures are counted exactly and stored at most five.

        A revoked token or a quota wall fails *every* file in the folder, and
        this state file is rewritten inside the operator's vault every thirty
        minutes. The count stays exact so the number is never a lie; the
        stored lines are a sample, because hundreds of strings per tick is a
        different problem from the silence this issue is fixing.
        """
        vault, src = _advisory_vault(tmp_path)
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"gdrive": True}),
        )
        _install_fake_drive(
            monkeypatch,
            DownloadResult(
                downloaded=(),
                skipped=(),
                errors=tuple(
                    (_drive_file(f"doomed-{i:02d}.docx"), RuntimeError(f"err {i}"))
                    for i in range(12)
                ),
            ),
        )

        result = runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "gdrive", "--vault", str(vault)],
        )

        assert result.exit_code == 0, result.output
        entry = _state_sources(vault)["gdrive"]
        assert entry["failure_count"] == 12, entry
        failures = entry["failures"]
        assert isinstance(failures, list), entry
        assert len(failures) == 5, failures

    def test_sync_surfaces_the_unrecognized_export_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #595 guard fires from ``sync``, not only from ``creek ingest``.

        ``creek ingest --type chatgpt`` prints "the export format may be
        unrecognized" on a discovered-but-empty run and exits 1 under
        ``--strict``. ``creek sync`` calls the very same ``_run_ingest`` and
        drops the discovered count on the floor (cli.py:987), so the scheduled
        path — the one that actually runs every day, unattended — was the one
        place that guard never reached.

        A real ChatGPT ingestor against a real file, deliberately: stubbing
        ``_run_ingest`` here would check the guard's wiring against a
        discovered count this test invented, and #595 exists precisely because
        real discovery and real parsing disagreed.
        """
        vault, src = _advisory_vault(tmp_path)
        export = src / "chatbot-exports" / "chatgpt"
        export.mkdir(parents=True)
        (export / "conversations.json").write_text(
            json.dumps([{"nope": 1}]), encoding="utf-8"
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"chatgpt": True}),
        )

        result = runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "chatgpt", "--vault", str(vault)],
        )

        assert result.exit_code == 0, result.output
        flat = _flatten(result.stdout)
        assert "the export format may be unrecognized" in flat, result.stdout

    def test_the_sync_guard_names_the_source_not_the_ingestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard line says ``essays`` — the name the operator configured.

        ``creek/pipeline.py:112`` maps ``essays -> substack`` (and
        ``journal -> markdown``), so a guard line built from ``source_type``
        alone announces "discovered 2 substack input(s)": a word that appears
        nowhere in the operator's ``sync.sources`` toggles and nowhere in the
        argv the scheduler ran. With several sources in one tick, that line is
        unattributable to any of them.

        The equivalent test for ``--source chatgpt`` would be vacuous, since
        chatgpt maps to itself and the label and the ingestor type are the
        same string — it would pass against the unfixed code.
        """
        vault, src = _advisory_vault(tmp_path)
        (src / "writing" / "substack").mkdir(parents=True)
        monkeypatch.setattr("creek.cli._run_ingest", lambda **_kw: (0, [], 2))
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"essays": True}),
        )

        result = runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "essays", "--vault", str(vault)],
        )

        guard = _lines_with(result.stdout, "may be unrecognized")
        assert guard, result.stdout
        assert any("essays" in line for line in guard), guard

    def test_sync_renders_per_source_ingest_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-file ingest error reaches stdout, alongside its source name.

        ``_run_ingest`` returns the ingestor's error list and ``creek ingest``
        prints it; sync bound it to ``_errors`` and dropped it (cli.py:987).
        The source name has to travel with the errors because the strings
        themselves are prefixed with the *ingestor* type (``[markdown]``),
        which is not the name the operator toggled on.
        """
        vault, src = _advisory_vault(tmp_path)
        (src / "personal" / "journal").mkdir(parents=True)
        monkeypatch.setattr(
            "creek.cli._run_ingest", lambda **_kw: (0, ["[markdown] boom"], 1)
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"journal": True}),
        )

        result = runner.invoke(
            app,
            ["sync", "--tier", "A", "--source", "journal", "--vault", str(vault)],
        )

        flat = _flatten(result.stdout)
        # The ``[markdown]`` prefix itself is unassertable: Rich parses it as
        # a style tag and renders it away, which is a second reason the source
        # name has to be supplied by the caller rather than read off the error.
        assert "boom" in flat, result.stdout
        assert "journal" in flat, result.stdout

    def test_a_missing_source_path_is_recorded_as_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source whose directory has vanished is recorded, not only printed.

        The skip already prints a yellow line (cli.py:981-984) and logs
        ``reason=path-missing``, but it returned a bare ``0`` and the state
        file recorded ``ingested: 0`` — identical to a healthy tick with
        nothing new. A renamed folder or an unmounted source drive is the most
        common way a schedule silently stops ingesting, so the durable record
        is where it has to land.
        """
        vault, src = _advisory_vault(tmp_path)
        # The journal directory is deliberately never created.
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault",
            lambda _v: _fake_config(vault, src, {"journal": True}),
        )

        result = runner.invoke(app, ["sync", "--tier", "A", "--vault", str(vault)])

        assert result.exit_code == 0, result.output
        entry = _state_sources(vault)["journal"]
        assert entry["failure_count"] == 1, entry
        failures = entry["failures"]
        assert isinstance(failures, list), entry
        assert any("source path not found" in str(line) for line in failures), failures
