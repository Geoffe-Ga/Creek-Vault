"""Real Tier-A / Tier-B execution for ``creek sync`` (#678).

Tier A runs, per enabled source, pull -> incremental ingest -> rules classify
and NEVER links/indexes (R6). Tier B runs the global llm-classify -> link ->
index. ``--dry-run`` still only echoes the plan. Because every step is
idempotent, a re-run produces no duplicates.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import (
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
