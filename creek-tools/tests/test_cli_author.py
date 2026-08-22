"""CLI tests for ``creek author`` (FEAT-041 Writing Desk)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest import mock

import pytest
import typer
import yaml
from typer.testing import CliRunner

from creek import cli as creek_cli
from creek.author.client import AuthorLLMClient
from creek.author.contracts import CHAT_MAX_CHARS, MEDIUMS_TEMPLATE_DIR
from creek.classify.llm.completion import Completion
from creek.cli import _compose_author_query, app
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.models import PrivacyTier

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# The Unicode Box Drawing block — the ``│``/``─``/``╭`` glyphs Rich uses to
# frame a panel. Matched as a range so a Rich style change to a different
# border set cannot quietly reintroduce the wrapping brittleness above.
_BOX_DRAWING_RE = re.compile("[─-╿]")


def _strip_ansi(text: str) -> str:
    """Return *text* with ANSI SGR colour escape codes removed.

    typer/rich style option names per-character under a colour-forcing
    terminal, which splits a literal like ``--work`` across escape sequences;
    stripping them rejoins the token so substring assertions are environment
    agnostic.
    """
    return _ANSI_RE.sub("", text)


def _flatten_panel(text: str) -> str:
    """Return *text* with ANSI codes, Rich panel borders and wrapping removed.

    Rich renders typer's errors inside a box and hard-wraps the body to the
    terminal width, so a message that embeds a filesystem path can break
    *mid-phrase*: ``'…/on-leverage.md' is a`` on one line and ``file.`` on the
    next, with ``│`` borders and padding in between. Whether that happens is a
    function of how long the path is, which makes any plain substring
    assertion a coin flip on the length of ``tmp_path``.

    That is not hypothetical. It is exactly what surfaced when the unit lane
    moved to pytest-xdist (issue #1141): xdist inserts a ``popen-gwN/``
    segment into ``tmp_path``, the path grew, and the wrap landed inside the
    phrase under test. The assertion was brittle before the move — any change
    to the temp-path layout would have tripped it — so the fix belongs here
    rather than in the runner.

    Dropping the box-drawing range and collapsing runs of whitespace rejoins
    the sentence, leaving assertions about *wording* independent of *width*.
    """
    return " ".join(_BOX_DRAWING_RE.sub(" ", _strip_ansi(text)).split())


def _collapse(text: str) -> str:
    """Return *text* with ANSI codes stripped and whitespace runs collapsed.

    The narrower sibling of :func:`_flatten_panel`: it does *not* touch the
    box-drawing range, because the outputs it is used on
    (``console.print`` from the ``author`` command) are never framed in a Rich
    panel. Rich still soft-wraps them at the non-tty 80-column default, so a
    leaked sentence arrives split across lines; collapsing whitespace on both
    the haystack and the needle makes "is the protected text on stdout?"
    independent of where the wrap landed (#1310).

    Args:
        text: Raw captured CLI output, or the needle to look for in it.

    Returns:
        The single-spaced, escape-free form of *text*.
    """
    return " ".join(_strip_ansi(text).split())


def _vault(tmp_path: Path) -> Path:
    """Create and return an empty vault directory under *tmp_path*."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_author_dry_run_prints_plan_and_evidence(tmp_path: Path) -> None:
    """``--dry-run`` prints the pipeline plan and a (real) evidence summary."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "What is F6 Pluralism?",
            "--vault",
            str(_vault(tmp_path)),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PLAN:" in result.output
    assert "graph" in result.output
    assert "reflect" in result.output
    assert "EVIDENCE:" in result.output  # real evidence, not a stub (#712)
    assert "(stub)" not in result.output
    assert "claims" in result.output
    assert "source_fragments" in result.output


def test_author_command_surface_is_not_stale_stub_text() -> None:
    """The author surface no longer mislabels the live desk as an all-stub (#712).

    Graph/Retrieval/Ontology specialists + Reflection are live deterministic work;
    Voice is live-when-a-provider-is-available. The command docstring + the
    author package docstrings must reflect that, not the original #455 scaffold.
    """
    import creek.author as author_pkg
    from creek.author import client as author_client
    from creek.author.conductor import Conductor, build_default_conductor
    from creek.cli import author as author_cmd

    surfaces = (
        author_cmd.__doc__,
        author_pkg.__doc__,
        author_client.__doc__,
        Conductor.__doc__,
        build_default_conductor.__doc__,
    )
    for doc in surfaces:
        assert doc is not None
        low = doc.lower()
        assert "stub skeleton" not in low
        assert "stub specialist" not in low
        assert "pure stubs" not in low
        assert "typed stubs" not in low
        assert "stub collaborators" not in low
    # The command docstring states the desk does real, deterministic work.
    assert "real, deterministic" in author_cmd.__doc__.lower()


def test_author_help_does_not_call_the_evidence_summary_a_stub() -> None:
    """``--dry-run``'s help must not call the evidence summary a stub (#1335).

    The sibling of the ``creek sync`` help fix. ``author --dry-run`` calls the
    conductor's real ``gather_evidence`` and prints the real claim and
    source-fragment counts (asserted by
    :func:`test_author_dry_run_prints_plan_and_evidence`), but the option's
    help string still advertised a "stub evidence summary" — telling the
    operator the numbers are fake when they are not, which is exactly the
    class of drift #1335 is about. The docstring at ``cli.py`` already says
    the desk does real work; the option help has to agree.
    """
    result = runner.invoke(app, ["author", "--help"])

    assert result.exit_code == 0, result.output
    flat = _flatten_panel(result.output)
    # Guards the negative assertion below against a vacuous pass on empty help.
    assert "--dry-run" in flat, flat
    assert "stub evidence" not in flat.lower(), flat


def test_author_run_prints_verdict(tmp_path: Path) -> None:
    """A full author run prints the verdict and a body."""
    result = runner.invoke(
        app,
        ["author", "--query", "q", "--vault", str(_vault(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    assert "verdict=" in result.output


def test_author_rejects_unknown_medium(tmp_path: Path) -> None:
    """An unsupported medium exits non-zero with a clear message."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "not-a-medium",
            "--query",
            "q",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    assert result.exit_code != 0
    assert "not-a-medium" in result.output


def test_author_book_report_requires_work(tmp_path: Path) -> None:
    """``--medium book-report`` without ``--work`` exits 2 with a clear error."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "--work" in result.output


def test_author_non_book_report_requires_query(tmp_path: Path) -> None:
    """A non-book-report medium without ``--query`` exits 2 with a clear error."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "essay",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "--query" in result.output


def test_author_book_report_runs_from_work_without_query(tmp_path: Path) -> None:
    """``--medium book-report --work <path>`` (no ``--query``) prints a draft."""
    vault = _vault(tmp_path)
    work = vault / "11-Other-Authors" / "naval-ravikant" / "on-leverage"
    work.mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--work",
            str(work),
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "verdict=" in result.output


def test_author_book_report_rejects_missing_work(tmp_path: Path) -> None:
    """``--work`` pointing at a non-existent path fails fast with a typer error."""
    vault = _vault(tmp_path)
    missing = vault / "11-Other-Authors" / "nobody" / "no-such-work"
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--work",
            str(missing),
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 2, result.output
    # typer renders the option name with per-character ANSI styling under a
    # colour-forcing terminal (CI), which splits the literal "--work"; strip
    # escape codes before matching so the assertion holds in every environment.
    plain = _strip_ansi(result.output)
    assert "--work" in plain
    assert "does not exist" in plain


def test_author_book_report_rejects_file_work(tmp_path: Path) -> None:
    """``--work`` pointing at a file (not a work directory) is rejected."""
    vault = _vault(tmp_path)
    work_file = vault / "11-Other-Authors" / "naval-ravikant" / "on-leverage.md"
    work_file.parent.mkdir(parents=True)
    work_file.write_text("not a directory", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--work",
            str(work_file),
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 2, result.output
    plain = _flatten_panel(result.output)
    assert "--work" in plain
    assert "is a file" in plain


def test_compose_author_query_requires_query_or_work() -> None:
    """The composer makes its upstream-validated invariant explicit.

    ``_validate_author_inputs`` guarantees a non-None query for every
    non-book-report medium (and book-report always carries ``--work``), so
    reaching the composer with both ``None`` is a programming error — it raises
    rather than silently authoring from an empty query.
    """
    with pytest.raises(ValueError, match="--query or --work"):
        _compose_author_query(None, None)


def test_author_max_rounds_out_of_range(tmp_path: Path) -> None:
    """``--max-rounds`` outside [1, 10] is rejected by the CLI."""
    result = runner.invoke(
        app,
        [
            "author",
            "--query",
            "q",
            "--vault",
            str(_vault(tmp_path)),
            "--max-rounds",
            "0",
        ],
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# #1310 — ``creek author`` must not print protected text
#
# The command built the desk via ``build_default_conductor(...).run(...)`` with
# **no medium contract** and never routed through ``creek.author.run_author``.
# ``check_privacy_compliance`` short-circuits on ``contract is None``, so the
# HARD privacy gate never ran on the CLI path, and the chat ceiling — applied
# by ``run_author``, not by ``Conductor.run`` — was skipped too.
# ---------------------------------------------------------------------------

_INTIMATE_SENTENCE = (
    "The night I finally admitted to myself that I was terrified of becoming "
    "my father, I wept in the parked car for an hour."
)
"""The protected text that must never reach stdout (#1310).

Deliberately plain English: no legacy taxonomy alias (``origins``, ``solo``,
``pitch``, …) and no bespoke ontology term, so the *only* reflection dimension
this corpus can trip is ``privacy_compliance``. That is what makes the
pre-fix verdict a genuine ``PASS`` rather than an incidental ``REVISE``.
"""

_LEAKED_PROSE = (
    "Here is what I keep coming back to when I sit with this question. "
    f"{_INTIMATE_SENTENCE} "
    "That is the whole shape of it, and I have nothing tidier to offer."
)
"""What the injected voice double returns — the realistic hazard.

The model is handed protected evidence and reproduces it verbatim inside
otherwise-innocuous prose. Nothing in the fragment's *title* gives the leak
away, so only a contract-bearing privacy gate can catch it.
"""


class _LeakyVoiceDouble:
    """Provider double whose completion reproduces protected fragment text.

    Shaped after ``tests/test_wiring_contract.py``'s ``_AuthorVoiceDouble``:
    it satisfies :class:`~creek.classify.llm.base.LLMProvider` structurally so
    :class:`~creek.author.client.AuthorLLMClient` can wrap it, and it never
    leaves the process.
    """

    is_cloud = False
    """The double never leaves the process, so the cloud-consent gate is moot."""

    def __init__(self, spoken: str) -> None:
        """Store the text every completion returns.

        Args:
            spoken: The prose the double voices, regardless of the prompt.
        """
        self._spoken = spoken

    @property
    def model(self) -> str:
        """Return the double's model identifier.

        Returns:
            A name no real backend answers to.
        """
        return "leaky-voice-double"

    @property
    def available(self) -> bool:
        """Report the double as ready so the desk takes the live-voicing path.

        Returns:
            Always ``True``.
        """
        return True

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Return the fixed prose this double was constructed with.

        ``AuthorLLMClient.complete_with_usage`` — the method
        :class:`~creek.author.voice.VoiceAgent` actually calls — delegates
        straight to this, so one method covers the whole live seam.

        Args:
            prompt: The dynamic voice prompt; ignored.
            max_tokens: Ignored; the double generates nothing to truncate.
            system: The static prompt prefix; ignored.

        Returns:
            A :class:`Completion` carrying the configured prose.
        """
        del prompt, max_tokens, system
        return Completion(text=self._spoken)


def _leaky_voice_factory(
    spoken: str,
) -> Callable[[PrivacyTier | None], AuthorLLMClient]:
    """Build the ``author_llm_factory`` double that voices *spoken*.

    Args:
        spoken: The prose the injected client returns for every render.

    Returns:
        A tier-ignoring factory matching the ``VoiceClientFactory`` shape the
        #1254 seam at ``creek/cli.py`` expects.
    """

    def _factory(_tier: PrivacyTier | None) -> AuthorLLMClient:
        """Return the wrapped double, ignoring the run's content tier.

        Args:
            _tier: The run's content tier; the double does not route on it.

        Returns:
            An :class:`AuthorLLMClient` wrapping :class:`_LeakyVoiceDouble`.
        """
        return AuthorLLMClient(_LeakyVoiceDouble(spoken))

    return _factory


def test_author_withholds_intimate_text_leaked_by_the_deterministic_render(
    tmp_path: Path,
) -> None:
    """``creek author`` must not print an intimate fragment's protected text (#1310).

    One fragment, ``privacy_tier: intimate``, whose ``title`` **equals** its
    body: the deterministic renderer emits titles as claims while
    ``check_privacy_compliance`` matches bodies, so title == body is what makes
    the leak reachable with no LLM at all — this is the CI default path.

    Pre-fix reality, measured on this worktree: stdout reads
    ``medium=research verdict=PASS rounds=1 provenance=5`` followed by a bullet
    carrying :data:`_INTIMATE_SENTENCE` verbatim, exit code ``0``, zero
    findings — because the CLI built the desk with no medium contract and
    ``check_privacy_compliance`` returns ``[]`` on ``contract is None``.

    Post-fix the CLI routes through ``run_author`` (contract-bearing) *and*
    withholds the body when any finding carries the ``privacy_compliance``
    dimension, while still printing the summary line and naming the dimension
    it refused on.
    """
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-int",
        _INTIMATE_SENTENCE,
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "father",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
        ],
    )

    plain = _collapse(result.output)
    assert _collapse(_INTIMATE_SENTENCE) not in plain, plain
    assert "verdict=ESCALATE" in plain, plain
    assert "privacy_compliance" in plain, plain
    assert result.exit_code != 0, plain


@pytest.mark.parametrize(
    "subdir",
    [
        pytest.param("09-Reference/External-Sources", id="09-Reference"),
        pytest.param("11-Other-Authors/quoted-friend", id="11-Other-Authors"),
    ],
)
def test_author_withholds_intimate_text_leaked_from_a_non_fragments_subtree(
    tmp_path: Path,
    subdir: str,
) -> None:
    """The same refusal must hold for the other two corpus subtrees (#1341).

    The twin of
    :func:`test_author_withholds_intimate_text_leaked_by_the_deterministic_render`
    with one thing changed: where the fragment lives. The desk's specialists
    gather from ``01-Fragments``, ``09-Reference`` and ``11-Other-Authors``
    alike (``creek/author/agents.py``), so all three can put protected text in
    a draft — but ``check_privacy_compliance`` resolves cited tiers out of
    ``01-Fragments`` only, and an unresolvable id is skipped rather than
    guessed. The result is a leak the HARD gate cannot see.

    ``title == body`` for the same reason as the original: the deterministic
    renderer emits fragment *titles* as claims while ``_is_verbatim_leak``
    matches *bodies*, so a differing title renders nothing leakable and the
    test would pass vacuously.

    The fragment stays self-authored with no ``author_slug`` in both cases —
    including under ``11-Other-Authors`` — so ``privacy_compliance`` remains
    the only dimension this corpus can raise. Adding a slug would add an
    ``attribution_correctness`` finding and muddy which gate produced the
    refusal, and the loader keys on the folder, not on the slug.

    Measured today: stdout carries the protected sentence verbatim under
    ``verdict=PASS`` with exit code ``0``.

    Args:
        tmp_path: Root for the synthetic vault.
        subdir: The corpus subtree (and leaf) the fragment is seeded into.
    """
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        subdir,
        "frag-int",
        _INTIMATE_SENTENCE,
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "father",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
        ],
    )

    plain = _collapse(result.output)
    assert _collapse(_INTIMATE_SENTENCE) not in plain, plain
    assert "verdict=ESCALATE" in plain, plain
    assert "privacy_compliance" in plain, plain
    assert result.exit_code != 0, plain


def test_author_withholds_intimate_text_leaked_by_the_live_voice_client(
    tmp_path: Path,
) -> None:
    """The same refusal holds when a live model reproduces the protected body (#1310).

    Here the fragment's *title* is an innocuous summary and only its **body**
    carries :data:`_INTIMATE_SENTENCE`, so nothing in the evidence summary
    exposes the leak — the injected voice client does, by reproducing the
    protected body verbatim. The client arrives through the #1254 seam
    (``creek.cli.author_llm_factory``, documented at ``cli.py:4860-4877``),
    which is the only way a hermetic test can reach past the desk.

    Pre-fix reality: the leaked prose is printed verbatim with exit code ``0``,
    because the contract-less desk never runs the HARD privacy gate.
    """
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-int",
        "A quiet note about my father",
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )

    with mock.patch.object(
        creek_cli,
        "author_llm_factory",
        _leaky_voice_factory(_LEAKED_PROSE),
    ):
        result = runner.invoke(
            app,
            [
                "author",
                "--medium",
                "research",
                "--query",
                "father",
                "--vault",
                str(vault),
                "--include-tier",
                "intimate",
            ],
        )

    plain = _collapse(result.output)
    assert _collapse(_INTIMATE_SENTENCE) not in plain, plain
    assert "verdict=ESCALATE" in plain, plain
    assert "privacy_compliance" in plain, plain
    assert result.exit_code != 0, plain


def test_author_cli_enforces_the_chat_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek author --medium chat`` truncates to ``CHAT_MAX_CHARS`` (#1310).

    The CLI twin of ``test_run_author_enforces_chat_ceiling`` in
    ``tests/test_chat_medium.py``. ``_enforce_chat_ceiling`` lives in
    ``run_author``, not in ``Conductor.run``, so the CLI's direct
    ``build_default_conductor(...).run(...)`` skipped it entirely: measured
    pre-fix, this run puts 2500 ``x`` characters on stdout.

    The count is asserted rather than ``len(result.output)`` because Rich
    soft-wraps and inserts newlines, which would inflate any length check.
    """
    from creek.author import voice

    monkeypatch.setattr(
        voice.VoiceAgent,
        "render",
        lambda self, q, e, *a, **kw: "x" * (CHAT_MAX_CHARS + 500),
    )

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "chat",
            "--query",
            "q",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    plain = _strip_ansi(result.output)
    assert result.exit_code == 0, plain
    assert plain.count("x") <= CHAT_MAX_CHARS
    assert "…" in plain  # the truncation marker _enforce_chat_ceiling appends


def test_author_refuses_when_the_medium_contract_is_unusable(
    tmp_path: Path,
) -> None:
    """An unloadable medium contract fails closed with no draft on stdout (#1310).

    ``creek/author/contracts.py:100-117`` prefers the *deployed*
    ``00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md`` over the packaged
    template, so a vault can shadow a shipped contract with a malformed one.
    A contract is the privacy ceiling; authoring without one is exactly the
    #1310 hole, so an unusable contract must refuse rather than degrade.

    Pre-fix reality: the CLI never loads a contract at all, so this ships a
    full draft with exit ``0``.

    ``FileNotFoundError`` is *not* exercised here because it is unreachable
    from this surface: ``_validate_author_inputs`` rejects any medium outside
    ``SUPPORTED_MEDIUMS`` with exit ``2`` first, and all six supported mediums
    ship a packaged template under ``creek/templates/skills/mediums/``, so
    ``_resolve_contract_path`` can never return ``None`` for a medium that got
    this far. The refusal must still catch ``OSError`` for the unreadable-file
    case, which is not portably forceable in this lane.
    """
    marker = "zzunmistakabledraftbodyzz"
    vault = _vault(tmp_path)
    deployed = vault / "00-Creek-Meta" / "Skills" / "mediums"
    deployed.mkdir(parents=True)
    (deployed / "research.MEDIUM.md").write_text(
        "---\nmedium: research\ndefault_privacy_tier: not-a-real-tier\n---\n\n"
        "# Shadowed by a malformed contract on purpose.\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["author", "--medium", "research", "--query", marker, "--vault", str(vault)],
    )

    plain = _collapse(result.output)
    assert result.exit_code != 0, plain
    assert marker not in plain, plain  # no draft body shipped
    assert "research" in plain, plain  # the refusal names the medium


def test_author_refusal_survives_markup_in_a_fragment_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The privacy refusal renders a bracketed fragment id literally (#1310).

    ``_emit_author_result`` echoes each finding's message, and those messages
    interpolate a fragment id straight from the vault (``Cited fragment
    'frag-a' is 'intimate' …``). Rich reads square brackets in a printed string
    as console markup, so an id carrying them corrupts the one output that must
    stay trustworthy — the refusal that tells an operator *which* fragment
    leaked:

    * ``frag[bold]x`` renders as ``fragx`` — the operator is handed the **wrong
      fragment id** on a privacy incident, silently;
    * ``frag[/red]`` raises ``rich.errors.MarkupError`` — the refusal becomes a
      traceback and the actionable message is lost.

    Neither is a leak (the body is still withheld and the exit is still
    non-zero), but a HARD gate's refusal has to survive its own input. Both ids
    must therefore reach stdout verbatim.
    """
    from creek.author.models import AuthoredDraft, ReflectionFinding
    from creek.cli import _emit_author_result

    hostile_ids = ("frag[bold]x", "frag[/red]")
    draft = AuthoredDraft(
        medium="research",
        query="q",
        body="THE-BODY-THAT-MUST-NOT-SHIP",
        verdict="ESCALATE",
        rounds=1,
        findings=[
            ReflectionFinding(
                dimension="privacy_compliance",
                severity="HIGH",
                message=f"Cited fragment {frag_id!r} is 'intimate' (above the "
                "'open' default) yet its protected text appears in the draft.",
            )
            for frag_id in hostile_ids
        ],
    )

    with pytest.raises(typer.Exit):
        _emit_author_result(draft)

    plain = _collapse(capsys.readouterr().out)
    assert "THE-BODY-THAT-MUST-NOT-SHIP" not in plain, plain
    for frag_id in hostile_ids:
        assert frag_id in plain, plain


# ---------------------------------------------------------------------------
# #1354 — a contract deployed *inside the vault* must not raise the ceiling
#
# ``check_privacy_compliance`` takes its ceiling from exactly one place: the
# medium contract's ``default_privacy_tier``. And ``_resolve_contract_path``
# (``creek/author/contracts.py:111-117``) prefers the vault-deployed
# ``00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md`` over the packaged
# template. So one YAML line, edited inside the vault, disarms a HARD gate on
# the very content that vault exists to protect — and #1310's refusal, pinned
# above, silently stops firing.
#
# The fix keeps ``check_privacy_compliance``'s signature unchanged and folds in
# a new ``author.max_reproduced_tier`` (default ``open``) read from
# ``<vault>/00-Creek-Meta/creek_config.yaml``: the effective ceiling is the
# MORE restrictive of the configured tier and the contract's declared tier. A
# vault-authored skill file can then only ever *narrow* the gate; widening it
# takes a deliberate operator edit to the config.
#
# These are the CLI end of that claim. Their MCP twins live in
# ``tests/test_mcp_author_privacy.py``; the two surfaces are asserted
# separately because "one read covers both" is the thing being proven, not an
# assumption either file may make about the other.
# ---------------------------------------------------------------------------


def _deploy_tampered_contract(vault: Path, medium: str, tier: str) -> None:
    """Deploy *medium*'s packaged contract into *vault*, widened to *tier*.

    Copies ``MEDIUMS_TEMPLATE_DIR / f"{medium}.MEDIUM.md"`` to the deployed
    path the loader prefers and rewrites the single
    ``default_privacy_tier: open`` line. **Everything else about the contract
    stays byte-for-byte the shipped template** — structure, specialist weights
    and the full reflection rubric — so the contract remains conformant,
    ``_preflight_medium_contract`` (``creek/cli.py:5862``) loads it without
    complaint, and it cannot be what refuses.

    That distinction is the whole reason this helper copies rather than
    hand-writes: the malformed-contract path
    (``default_privacy_tier: not-a-real-tier``) is already pinned by
    :func:`test_author_refuses_when_the_medium_contract_is_unusable`, which
    must stay green, and a tampered contract that merely failed to parse would
    prove nothing at all about the privacy ceiling. The refusals asserted below
    have to come from the leak gate, not from the preflight.

    Args:
        vault: Vault root; ``00-Creek-Meta/Skills/mediums`` is created if absent.
        medium: Medium slug whose packaged contract is copied and tampered with.
        tier: The ``default_privacy_tier`` value declared in place of ``open``.
    """
    packaged = (MEDIUMS_TEMPLATE_DIR / f"{medium}.MEDIUM.md").read_text(
        encoding="utf-8",
    )
    tampered = packaged.replace(
        "default_privacy_tier: open",
        f"default_privacy_tier: {tier}",
    )
    # Guard against a template edit turning every test below vacuous: if the
    # packaged contract stops declaring ``open``, the "tampering" is a no-op
    # and the assertions would pass against an untouched ceiling.
    assert tampered != packaged, f"{medium} template no longer declares 'open'"
    deployed = vault / "00-Creek-Meta" / "Skills" / "mediums"
    deployed.mkdir(parents=True, exist_ok=True)
    (deployed / f"{medium}.MEDIUM.md").write_text(tampered, encoding="utf-8")


def _write_vault_config(vault: Path, tier: str) -> None:
    """Write ``<vault>/00-Creek-Meta/creek_config.yaml`` declaring the ceiling.

    Serialized with ``yaml.safe_dump`` so the on-disk shape is whatever
    :func:`creek.config.load_config` would itself round-trip, rather than a
    hand-formatted string that only happens to parse.

    Args:
        vault: Vault root; ``00-Creek-Meta`` is created if absent.
        tier: Value written to ``author.max_reproduced_tier``.
    """
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text(
        yaml.safe_dump({"author": {"max_reproduced_tier": tier}}),
        encoding="utf-8",
    )


@pytest.mark.parametrize("tier", ["personal", "intimate", "unclassified"])
def test_a_deployed_contract_cannot_raise_the_privacy_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
) -> None:
    """A skill file in the vault cannot licence ``creek author`` to leak (#1354).

    The end-to-end form of the defect, driven through the real CLI: one
    intimate fragment, one tampered ``research`` contract deployed into the
    vault, no config. The configured ceiling is its default ``open``, which is
    stricter than anything the contract declares, so the effective ceiling must
    stay ``open`` and the run must refuse exactly as
    :func:`test_author_withholds_intimate_text_leaked_by_the_deterministic_render`
    does against the untampered template.

    MEASURED at HEAD with this exact vault: ``contract=intimate`` -> ``exit 0``
    with :data:`_INTIMATE_SENTENCE` on stdout, and ``contract=unclassified`` ->
    ``exit 0`` with the sentence on stdout. Those two cases are the
    reproduction. ``contract=personal`` already refuses at HEAD (an intimate
    fragment exceeds even the widened ``personal`` ceiling) and is carried here
    as the control that the fix leaves already-correct behaviour alone — drop
    it and nothing detects an over-fix that only ever narrows to ``open``.

    VACUITY GUARD — ``--include-tier intimate`` is mandatory, not incidental.
    Without it the #660 retrieval override defaults to ``OPEN``, the intimate
    fragment is filtered out before the desk sees it, the draft contains no
    protected text, and this test would pass at HEAD for entirely the wrong
    reason: nothing leaked because nothing was retrieved. The same applies to
    ``title == body`` in the seeding call — the deterministic renderer emits
    fragment *titles* as claims while the gate matches *bodies*, so only when
    the two are identical does the hermetic, no-LLM path reproduce protected
    text at all.

    Args:
        tmp_path: Root for the synthetic vault.
        monkeypatch: Used to drop an ambient ``CREEK_CONFIG``.
        tier: The ``default_privacy_tier`` the deployed contract declares.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-int",
        _INTIMATE_SENTENCE,
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )
    _deploy_tampered_contract(vault, "research", tier)

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "father",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
        ],
    )

    plain = _collapse(result.output)
    assert _collapse(_INTIMATE_SENTENCE) not in plain, plain
    assert "privacy_compliance" in plain, plain
    assert "verdict=ESCALATE" in plain, plain
    assert result.exit_code != 0, plain


def test_an_operator_configured_ceiling_deliberately_permits_the_reproduction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator *can* widen the gate — from the config, and only there (#1354).

    **Do not delete this test as redundant with the refusals above.** It is the
    only thing that distinguishes the fix from a blanket refusal. Every other
    test in this section asserts that protected text is withheld; a "fix" that
    hardcoded ``open`` as the ceiling, or that simply refused whenever any
    contract declared above ``open``, would satisfy all of them and quietly
    remove a capability the ceiling is supposed to be *configurable* for.

    The widening is reachable by exactly one route: an operator editing
    ``<vault>/00-Creek-Meta/creek_config.yaml``. It is emphatically **not**
    reachable from a medium contract, which deploys by default from the
    packaged templates and is rewritten by ``creek skills sync``. Config and
    contract agree on ``intimate`` here, so the fold has nothing to narrow, the
    cited fragment sits *at* the effective ceiling, and the draft ships.

    This case already passes at HEAD (``exit 0`` with the sentence on stdout)
    and must still pass afterwards — it is a control, not a reproduction.

    Args:
        tmp_path: Root for the synthetic vault.
        monkeypatch: Used to drop an ambient ``CREEK_CONFIG``.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-int",
        _INTIMATE_SENTENCE,
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )
    _deploy_tampered_contract(vault, "research", "intimate")
    _write_vault_config(vault, "intimate")

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "father",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
        ],
    )

    plain = _collapse(result.output)
    assert result.exit_code == 0, plain
    assert _collapse(_INTIMATE_SENTENCE) in plain, plain


def test_a_configured_ceiling_below_the_contract_still_governs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graded case: ``personal`` config vs ``intimate`` contract (#1354).

    The fold is not "config wins" and not "contract wins" — it is the more
    restrictive of the two, and this is the row where those three rules give
    different answers. The operator configured ``personal`` (rank 1) while the
    deployed contract declares ``intimate`` (rank 2), so the effective ceiling
    is ``personal`` and the cited *intimate* fragment is still above it: the
    body must be withheld.

    A fix that let the contract win whenever a config key was present would
    ship the protected sentence here while every other test in this section
    stayed green.

    Args:
        tmp_path: Root for the synthetic vault.
        monkeypatch: Used to drop an ambient ``CREEK_CONFIG``.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-int",
        _INTIMATE_SENTENCE,
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )
    _deploy_tampered_contract(vault, "research", "intimate")
    _write_vault_config(vault, "personal")

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "father",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
        ],
    )

    plain = _collapse(result.output)
    assert _collapse(_INTIMATE_SENTENCE) not in plain, plain
    assert "privacy_compliance" in plain, plain
    assert result.exit_code != 0, plain


def test_a_dry_run_against_a_tampered_contract_is_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` neither refuses nor consults the new key (#1354).

    **This test passes today and must keep passing.** The dry-run branch
    (``creek/cli.py:6039-6060``) renders no body — it prints the plan and the
    evidence *counts* and returns before ``_preflight_medium_contract`` and
    before ``run_author`` — so there is nothing for the privacy gate to gate
    and the gate never runs. It is the control that pins the blast radius of
    the fix: adding a ceiling must not turn a planning command into a refusal,
    and must not introduce a config read on a branch that has never needed one
    (a vault with no ``creek_config.yaml``, or an unreadable one, must still
    plan).

    Same tampered ``intimate`` contract as its refusing sibling above, so the
    only difference between green here and red there is the flag.

    Args:
        tmp_path: Root for the synthetic vault.
        monkeypatch: Used to drop an ambient ``CREEK_CONFIG``.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    vault = _vault(tmp_path)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-int",
        _INTIMATE_SENTENCE,
        body=_INTIMATE_SENTENCE,
        privacy_tier="intimate",
    )
    _deploy_tampered_contract(vault, "research", "intimate")

    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "father",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
            "--dry-run",
        ],
    )

    plain = _collapse(result.output)
    assert result.exit_code == 0, plain
    assert "privacy_compliance" not in plain, plain
    # The plan/evidence summary is counts only; no body, and so no leak.
    assert _collapse(_INTIMATE_SENTENCE) not in plain, plain
