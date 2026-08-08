"""CLI tests for the ``creek fill`` umbrella command (#720).

``creek fill`` is pure orchestration over already-merged steps, so these tests
mock each underlying step and assert: the steps run in dependency order, a step
failure is non-fatal, ``--dry-run`` runs nothing, ``--with-compost`` appends the
compost step, and the summary reports real per-folder counts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from typer.testing import CliRunner

import creek.cli as cli_mod
import creek.time as creek_time
from creek.cli import app
from creek.generate import compost as compost_mod
from creek.generate import indexes as indexes_mod
from creek.link import link_engine

if TYPE_CHECKING:
    from datetime import tzinfo
    from pathlib import Path

    import pytest

runner = CliRunner()

_EXPECTED_ORDER = [
    "link/embeddings",
    "link/temporal",
    "link/eddies",
    "link/threads",
    "report/decisions",
    "report/unnamed",
    "report/paradox",
    "report/synchronicity",
    "report/mode-profiles",
    # #879: ``creek fill`` is the "make my vault prod-ready" command, so the
    # voice report belongs in it — without this step the register samples are
    # only ever written by an operator who knows to run
    # ``creek report --type voice`` by hand.
    "report/voice",
    "report/wavelength",
    "index",
]


def _install_recorders(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    fail: str | None = None,
) -> None:
    """Replace every underlying step with a recorder appending its label."""

    def rec(label: str) -> object:
        def _step(*_a: object, **_k: object) -> None:
            calls.append(label)
            if label == fail:
                msg = f"boom in {label}"
                raise RuntimeError(msg)

        return _step

    monkeypatch.setattr(
        link_engine,
        "run_link",
        lambda **k: rec(f"link/{k['method']}")(),
    )
    for name, label in (
        ("_report_decisions", "report/decisions"),
        ("_report_unnamed", "report/unnamed"),
        ("_report_paradox", "report/paradox"),
        ("_report_synchronicity", "report/synchronicity"),
        ("_report_mode_profiles", "report/mode-profiles"),
        ("_report_voice", "report/voice"),
    ):
        monkeypatch.setattr(cli_mod, name, rec(label))
    monkeypatch.setattr(
        cli_mod,
        "_report_wavelength",
        lambda _vault, _period: rec("report/wavelength")(),
    )

    class _FakeIndex:
        def __init__(self, *, vault_path: Path) -> None:
            self._vault_path = vault_path

        def generate_all(self) -> list[Path]:
            rec("index")()
            return []

    monkeypatch.setattr(indexes_mod, "IndexGenerator", _FakeIndex)

    class _FakeCompost:
        def generate_compost_report(self, vault_path: Path) -> Path:
            rec("compost/report")()
            return vault_path

    monkeypatch.setattr(compost_mod, "CompostTracker", _FakeCompost)


def test_fill_runs_all_steps_in_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``creek fill`` runs every step once, in the documented order."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert calls == _EXPECTED_ORDER


def test_fill_voice_step_states_the_unfiltered_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``report/voice`` step declares ``PrivacyTierOverride.ALL`` (#879).

    ``creek fill`` has no ``--include-tier`` of its own, and on the report
    surface an absent flag means *unfiltered*, so the step has to say
    ``ALL`` out loud rather than inherit a default nobody typed — the same
    contract every other step in ``_build_fill_steps`` already keeps. The
    step is fetched from the plan and invoked directly rather than through
    ``creek fill`` so the assertion is about the argument, not about the
    command happening to reach it.

    Args:
        tmp_path: pytest's per-test temporary directory.
        monkeypatch: Fixture used to capture the override the step passes.
    """
    from creek.classify.privacy_filter import PrivacyTierOverride

    seen: list[object] = []
    monkeypatch.setattr(
        cli_mod,
        "_report_voice",
        lambda _vault, override: seen.append(override),
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    steps = dict(
        cli_mod._build_fill_steps(
            vault,
            cli_mod._load_config_for_vault(vault),
            with_compost=False,
        )
    )

    assert "report/voice" in steps
    steps["report/voice"]()

    assert seen == [PrivacyTierOverride.ALL]


def test_fill_step_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing step is logged and the remaining steps still run."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls, fail="report/decisions")

    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    # Every step was still attempted despite the failure mid-sequence.
    assert calls == _EXPECTED_ORDER
    assert "report/decisions failed" in result.output


def test_fill_dry_run_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` prints the plan and invokes no step."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "dry-run" in result.output.lower()
    for label in _EXPECTED_ORDER:
        assert label in result.output


def test_fill_with_compost_appends_compost_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--with-compost`` adds the compost overview step after the core sequence."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault), "--with-compost"])

    assert result.exit_code == 0, result.output
    assert calls == [*_EXPECTED_ORDER, "compost/report"]


# ---- Issue #938: the compost report is dated on the LA calendar -----------

_LA_AHEAD_UTC_INSTANT = datetime(2026, 7, 29, 6, 0, tzinfo=UTC)
"""One absolute instant whose UTC and LA calendar dates disagree.

06:00 UTC on 2026-07-29 is 23:00 PDT on 2026-07-28. A clock reading the UTC
calendar stamps ``2026-07-29``; a clock reading LA stamps ``2026-07-28``.
Freezing here turns issue #938's date bug from "true for about seven hours a
day" into a deterministic assertion.
"""


class _FrozenDatetime(datetime):
    """A ``datetime`` frozen at :data:`_LA_AHEAD_UTC_INSTANT`.

    Deliberately timezone-agnostic, unlike the narrower stub in
    ``tests/test_time.py``: it answers ``now(tz=...)`` with the one fixed
    instant expressed in whatever zone it is handed, and asserts nothing
    about which zone that is. The buggy implementation asks for UTC and the
    fixed one asks for America/Los_Angeles — both have to run through this
    stub, or the test would be measuring the patch instead of the behaviour.
    """

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        """Return the frozen instant, expressed in *tz*.

        Args:
            tz: Target timezone. ``None`` yields the naive UTC reading,
                matching :meth:`datetime.now`'s own default.

        Returns:
            The single frozen moment, converted into *tz*.
        """
        if tz is None:
            return _LA_AHEAD_UTC_INSTANT.replace(tzinfo=None)
        return _LA_AHEAD_UTC_INSTANT.astimezone(tz)


def test_fill_with_compost_dates_the_report_on_the_la_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``creek fill --with-compost`` stamps the LA date, never the UTC one.

    Runs the **real** ``CompostTracker`` — every other test in this module
    stubs it out — against a clock frozen at 23:00 PDT on 2026-07-28, which
    is already 2026-07-29 in UTC. Both module-level ``datetime`` names are
    replaced with the same stub: ``creek.generate.compost.datetime``, which
    the buggy ``datetime.now(tz=UTC).replace(tzinfo=None)`` calls, and
    ``creek.time.datetime``, which ``now_la()`` calls. Patching both is what
    makes the failure a genuine date mismatch rather than an artefact of
    which module happened to be frozen.

    Args:
        tmp_path: Pytest temporary directory used as the vault root.
        monkeypatch: Fixture used to stub the steps and freeze the clock.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []

    # ``_install_recorders`` swaps in a fake tracker; capture the real class
    # first and put it back, so this test exercises production compost code
    # while every other fill step stays stubbed.
    real_tracker = compost_mod.CompostTracker
    _install_recorders(monkeypatch, calls)
    monkeypatch.setattr(compost_mod, "CompostTracker", real_tracker)
    # Both clocks are frozen to the same instant, and both with raising=True:
    # the RED failure has to be a real date mismatch, not a typo in a module
    # path silently tolerated. Contract for the fix: ``creek.generate.compost``
    # must keep ``datetime`` bound at module scope (not moved under
    # ``TYPE_CHECKING``) so this patch point survives.
    monkeypatch.setattr(compost_mod, "datetime", _FrozenDatetime, raising=True)
    monkeypatch.setattr(creek_time, "datetime", _FrozenDatetime, raising=True)

    result = runner.invoke(app, ["fill", "--vault", str(vault), "--with-compost"])

    assert result.exit_code == 0, result.output
    report = vault / "10-Liminal" / "Compost" / "_Compost-Report.md"
    text = report.read_text(encoding="utf-8")
    generated = [ln for ln in text.splitlines() if ln.startswith("generated:")]
    assert generated == ["generated: 2026-07-28"]


# ---- Issue #876: the untiered-fragment hint -------------------------------


def _seed_fragment(vault: Path, frag_id: str, *, tier: str | None) -> None:
    """Write a minimal fragment file, optionally carrying a ``privacy_tier``.

    Built from a literal frontmatter template (not ``Fragment.model_dump``)
    so ``tier=None`` produces a file with the ``privacy_tier`` key genuinely
    **absent** — the legacy/hand-edited shape, which is distinct from an
    explicit ``privacy_tier: unclassified``.

    Args:
        vault: Vault root.
        frag_id: Fragment id (also the file stem).
        tier: Tier string to stamp, or ``None`` to omit the key entirely.
    """
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    tier_line = f"privacy_tier: {tier}\n" if tier else ""
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "A note"\n'
        f"source:\n  platform: journal\n  author: self\n{tier_line}---\nbody\n",
        encoding="utf-8",
    )


def test_count_untiered_fragments_counts_absent_and_unclassified(
    tmp_path: Path,
) -> None:
    """The helper counts both the absent key and an explicit ``unclassified``.

    Those are the two shapes a fragment that has never been through a
    privacy pass can take, and both are what the operator needs told
    about. Four fragments spanning four distinct tier states so the count
    cannot be right by accident.
    """
    from creek.cli import _count_untiered_fragments

    vault = tmp_path / "vault"
    _seed_fragment(vault, "frag-absent", tier=None)
    _seed_fragment(vault, "frag-unclass", tier="unclassified")
    _seed_fragment(vault, "frag-open", tier="open")
    _seed_fragment(vault, "frag-intimate", tier="intimate")

    assert _count_untiered_fragments(vault) == 2


def test_count_untiered_fragments_is_zero_without_a_fragments_dir(
    tmp_path: Path,
) -> None:
    """A vault with no ``01-Fragments`` counts zero rather than exploding."""
    from creek.cli import _count_untiered_fragments

    assert _count_untiered_fragments(tmp_path / "empty-vault") == 0


def test_fill_hints_when_untiered_fragments_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``creek fill`` tells the operator how many fragments carry no tier.

    Printed even when there is no classify-upgrade offer, so the hint has
    to sit ahead of the ``offer is None`` early return — otherwise it never
    fires on exactly the vault that needs it (rules-classified, no LLM
    reachable, every fragment untiered).
    """
    from creek.cli import _maybe_upgrade_classification

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    _seed_fragment(vault, "frag-absent", tier=None)
    _seed_fragment(vault, "frag-unclass", tier="unclassified")
    _seed_fragment(vault, "frag-open", tier="open")

    _maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    out = capsys.readouterr().out
    assert "untiered" in out
    assert "2" in out


def test_fill_hint_is_silent_when_every_fragment_is_tiered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fully-tiered vault gets no hint — the nag must not be permanent."""
    from creek.cli import _maybe_upgrade_classification

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    _seed_fragment(vault, "frag-open", tier="open")
    _seed_fragment(vault, "frag-personal", tier="personal")
    _seed_fragment(vault, "frag-intimate", tier="intimate")

    _maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    assert "untiered" not in capsys.readouterr().out


def test_untiered_hint_failure_never_crashes_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken untiered count is swallowed by the same best-effort guard.

    The hint is advisory; an unreadable fragment must not abort ``creek
    fill`` before any step runs, exactly as the classify-upgrade probe
    already behaves (#736).
    """
    from creek.cli import _maybe_upgrade_classification

    def _boom(*_a: object) -> int:
        raise OSError("unreadable fragment")

    monkeypatch.setattr(cli_mod, "_count_untiered_fragments", _boom)
    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)

    # Must not raise.
    _maybe_upgrade_classification(
        tmp_path, cli_mod._load_config_for_vault(tmp_path), upgrade=False
    )


def test_fill_summary_reports_folder_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final summary counts the markdown actually present per folder."""
    vault = tmp_path / "vault"
    (vault / "02-Threads" / "Active").mkdir(parents=True)
    (vault / "02-Threads" / "Active" / "a.md").write_text("x", encoding="utf-8")
    (vault / "02-Threads" / "Active" / "b.md").write_text("x", encoding="utf-8")
    # A hidden index file must not be counted.
    (vault / "02-Threads" / "Active" / ".id-index.jsonl").write_text(
        "{}", encoding="utf-8"
    )
    (vault / "08-Decisions").mkdir(parents=True)
    (vault / "08-Decisions" / "d.md").write_text("x", encoding="utf-8")

    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "02-Threads 2" in result.output
    assert "08-Decisions 1" in result.output


# ---- Issue #877: the praxis-backfill hint ---------------------------------
#
# The gap detector cannot be "the ``praxis_potential`` key is absent"
# (``Fragment.model_dump`` always writes it) and cannot be "the value is
# ``none``" (most fragments genuinely ARE none, so that nag would be
# permanent and instantly ignored). It is the one shape that proves work is
# outstanding: **the free keyword heuristic says ``explicit`` but the disk
# says ``none``.** Self-limiting, costs zero tokens to compute, and goes
# quiet the moment the operator acts on it.

_PRAXIS_SIGNAL_BODY = "notes from the week ahead\n- [ ] book the boiler service"
"""A body carrying one strong praxis marker (a task checkbox)."""

_PRAXIS_QUIET_BODY = "a plain note about the walk to the shops and the weather"
"""A body carrying no praxis marker at all."""


def _seed_praxis_fragment(
    vault: Path,
    frag_id: str,
    *,
    body: str,
    praxis: str,
) -> None:
    """Write a fragment with a chosen body and ``praxis_potential`` value.

    Built from a literal frontmatter template so the on-disk value is
    exactly what the test names, independent of ``Fragment`` defaults. A
    real ``privacy_tier`` is stamped so the sibling #876 untiered hint
    stays silent and cannot pollute the captured output.

    Args:
        vault: Vault root.
        frag_id: Fragment id (also the file stem).
        body: Markdown body the praxis heuristic scores.
        praxis: The ``praxis_potential`` value already on disk.
    """
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "A note"\n'
        f"source:\n  platform: markdown\n  author: self\n"
        f"privacy_tier: open\npraxis_potential: {praxis}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_count_praxis_backfillable_counts_only_provable_gaps(
    tmp_path: Path,
) -> None:
    """Only "signals present but disk says none" counts as backfillable.

    Four fragments spanning four distinct states so the count cannot be
    right by accident:

    * signals + ``none`` — the real gap, the only one counted;
    * signals + ``explicit`` — already backfilled, nothing owed;
    * signals + ``latent`` — a verdict is on record, so not a gap;
    * no signals + ``none`` — genuinely has no praxis potential.

    Counting the last two would make the hint permanent, which is the
    failure mode this detector was designed around.
    """
    from creek.cli import _count_praxis_backfillable_fragments

    vault = tmp_path / "vault"
    _seed_praxis_fragment(vault, "frag-gap", body=_PRAXIS_SIGNAL_BODY, praxis="none")
    _seed_praxis_fragment(
        vault, "frag-done", body=_PRAXIS_SIGNAL_BODY, praxis="explicit"
    )
    _seed_praxis_fragment(
        vault, "frag-latent", body=_PRAXIS_SIGNAL_BODY, praxis="latent"
    )
    _seed_praxis_fragment(vault, "frag-quiet", body=_PRAXIS_QUIET_BODY, praxis="none")

    assert _count_praxis_backfillable_fragments(vault) == 1


def test_count_praxis_backfillable_is_zero_without_a_fragments_dir(
    tmp_path: Path,
) -> None:
    """A vault with no ``01-Fragments`` counts zero rather than exploding."""
    from creek.cli import _count_praxis_backfillable_fragments

    assert _count_praxis_backfillable_fragments(tmp_path / "empty-vault") == 0


def test_fill_hints_when_praxis_can_be_backfilled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The hint reports the count, and names the command and its cost.

    Printed ahead of the ``offer is None`` early return, like the #876
    untiered hint it sits beside, so it still fires on the vault that most
    needs it (fully classified, no LLM upgrade to offer).

    It reports a **count only**. Naming a fragment id or quoting a body
    excerpt would turn an advisory line into an unaudited disclosure of
    vault content through stdout, which the config-oracle rule (#846 /
    #848) forbids — so both are asserted absent.
    """
    from creek.cli import _maybe_upgrade_classification

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    _seed_praxis_fragment(vault, "frag-gap", body=_PRAXIS_SIGNAL_BODY, praxis="none")
    _seed_praxis_fragment(vault, "frag-quiet", body=_PRAXIS_QUIET_BODY, praxis="none")

    _maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    out = capsys.readouterr().out
    assert "praxis" in out.lower()
    assert "1" in out
    # The remedy is named, and so is its price: --force re-sends those
    # fragments to the configured provider and bills for the tokens.
    assert "--force" in out
    assert "token" in out.lower()
    # …and nothing about the fragment itself leaks.
    assert "frag-gap" not in out
    assert "boiler" not in out


def test_praxis_hint_is_silent_when_every_signal_is_already_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A backfilled vault gets no hint — the nag must not be permanent.

    The vault here is the steady state after one ``creek classify`` run:
    every fragment the heuristic can prove is already ``explicit``, and
    the rest genuinely are ``none``. Any implementation that keys off
    "value == none" would print here forever and train the operator to
    ignore the line.
    """
    from creek.cli import _maybe_upgrade_classification

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    _seed_praxis_fragment(
        vault, "frag-done", body=_PRAXIS_SIGNAL_BODY, praxis="explicit"
    )
    _seed_praxis_fragment(vault, "frag-quiet", body=_PRAXIS_QUIET_BODY, praxis="none")

    _maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    assert "praxis" not in capsys.readouterr().out.lower()


def test_praxis_hint_failure_never_crashes_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken praxis count is swallowed by its own best-effort guard.

    The hint is advisory; an unreadable fragment must not abort ``creek
    fill`` before any step runs, exactly as the classify-upgrade probe
    (#736) and the untiered hint (#876) already behave.

    Contract note for the implementation: the hint must call the
    module-level ``_count_praxis_backfillable_fragments`` (this test
    patches it there) and log its own WARNING naming ``praxis``, rather
    than sharing the untiered hint's guard — otherwise one failing scan
    silently suppresses the other hint too.
    """
    from creek.cli import _maybe_upgrade_classification

    def _boom(*_a: object) -> int:
        raise OSError("unreadable fragment")

    monkeypatch.setattr(cli_mod, "_count_praxis_backfillable_fragments", _boom)
    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)

    with caplog.at_level(logging.WARNING):
        # Must not raise.
        _maybe_upgrade_classification(
            tmp_path, cli_mod._load_config_for_vault(tmp_path), upgrade=False
        )

    assert any("praxis" in record.getMessage().lower() for record in caplog.records)
