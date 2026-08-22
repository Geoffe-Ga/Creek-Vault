"""Contract: every CLI command and every MCP tool is reachable **and effectful**.

A surface can be registered, documented, reachable, exit ``0`` — and be wired
to nothing. Seventeen issues in this repo have that shape (#935, #649, #658,
#460, #506, #580, #581, #577, #821, #1040, #1231, …): the surface exists, the
implementation exists, the wire between them does not. No other gate notices,
because every one of those bugs exited ``0`` with 6000+ tests green. This
module is that missing gate (issue #1027, epic #1024).

What "effectful" means here
---------------------------
An assertion that a command exits ``0`` is worthless — that is precisely what
every cited bug did. Each entry therefore declares a **named observable** and
the harness proves it both ways:

* :attr:`Effect.writes` / :attr:`Effect.removes` / :attr:`Effect.frontmatter`
  carry their own negative control for free: the harness asserts the artefact
  is **absent before** the surface runs and **present after** (or the reverse
  for ``removes``). "Not run" is the control.
* :attr:`Effect.prints` has no such free control — a print-only scanner renders
  "judged clean" and "found nothing to judge" identically — so every entry that
  declares ``prints`` **must** also declare a :class:`Contrast`: a second world
  (usually an unseeded vault) in which the same substrings must *not* appear.
  :func:`test_printed_effects_declare_a_contrast` enforces that.
* :attr:`Surface.refusal` proves a gate two-sided: with the gate closed the
  documented reason appears *and the vault is byte-identical*; the entry's
  primary run satisfies the gate and produces the effect. A one-sided refusal
  test is passed by a surface that refuses everything.

Why "a file appeared" is never enough
-------------------------------------
Every MCP tool appends ``00-Creek-Meta/audit/mcp.jsonl`` — including all five
``creek.purge.*`` tools *while refusing*. A bare "something changed" predicate
would therefore certify all 24 tools as effectful while proving only that the
decorator ran. :data:`_BOOKKEEPING_GLOBS` names those paths and
:func:`test_no_effect_is_satisfied_by_bookkeeping_alone` fails any entry whose
declared observable lives entirely inside them.

Why this catches #1040
----------------------
#1040 was the hardest shape: ``creek draft`` and ``creek.draft`` both ran, both
saved a file under ``07-Voice/Drafts/``, both exited ``0`` — and the saved
draft carried no ``derivative_score``, so the ``draft-grounding`` lint check's
"scores absent → clean" branch called every vault spotless. A predicate of the
form "a draft file appeared" passes in the dormant world *and* the live one.
The rule that catches it is :attr:`Effect.contains`: the declared observable
names the **feature-specific frontmatter key, imported from the production
constant that defines it** — here
:data:`creek.generate.grounding.DERIVATIVE_FRONTMATTER_KEY` — and
:attr:`Surface.derived_from` records that provenance so
:func:`test_derived_from_names_importable_production_symbols` can prove the
citation is real rather than a comment. The generalisation, stated as a rule:
*where a feature has a downstream reader, the producer's effect must assert the
key that reader looks for.*

Inventories are derived, never retyped
--------------------------------------
The CLI set comes from walking the live ``click`` group that Typer builds; the
MCP set from ``await server.list_tools()``. A retyped list is the drift defect
epic #1024 exists to kill — and §4 of this module's own findings shows it
bit in production twice: ``creek_mcp.tools.link`` had retyped the CLI's method
tuple and lost ``"threads"`` (#1252), and ``creek_mcp.tools.report`` had retyped
the CLI's type tuple and lost five of eleven (#1253). Both copies are gone —
:mod:`creek.surface_modes` is the one declaration — and the two parity tests
below now compare what each surface *advertises to a caller*, so a
re-introduced copy fails on behaviour rather than on inspection.

Adding a surface
----------------
See ``docs/wiring-contract.md``. In short: add an entry to
:data:`CLI_CONTRACT` or :data:`MCP_CONTRACT` naming the observable your surface
produces and which of the wiring bugs the entry would have caught. If the
effect genuinely cannot be proven without a provider key or a real service,
add an :class:`Exemption` instead — but an exemption must carry an *executable*
staleness probe, so it fails the day the blocker disappears.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import inspect
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from unittest import mock

import click
import frontmatter
import pytest
import typer
from typer import rich_utils
from typer.testing import CliRunner

from creek import cli as creek_cli
from creek.author.client import AuthorLLMClient
from creek.classify.llm.completion import Completion
from creek.cli import (
    _CLASSIFY_METHODS,
    _REPORT_DISPATCH,
    app,
)
from creek.generate.grounding import (
    DERIVATIVE_FRONTMATTER_KEY,
    GROUNDING_FRONTMATTER_KEY,
)
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.discord_dispatch import DiscordMode
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from creek.save import SaveTarget
from creek.surface_modes import LINK_METHODS
from creek_mcp.auth import ELEVATED_TOKEN_ENV
from creek_mcp.policy import Transport
from creek_mcp.server import build_server
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.link import link_tool
from creek_mcp.tools.report import report_tool
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Derived inventories
# ---------------------------------------------------------------------------


def cli_surface_names(node: click.Command, prefix: tuple[str, ...] = ()) -> set[str]:
    """Return every invocable CLI path under *node*, space-joined.

    Walks the ``click`` command tree Typer builds, which is what a user's
    shell actually resolves. ``app.registered_commands`` is deliberately
    **not** used: every command under ``clean`` / ``purge`` / ``skills`` /
    ``compost`` lives in ``app.registered_groups`` instead, so that attribute
    sees only 23 of the 37 surfaces — a 38% blind spot in the very inventory
    this module exists to keep honest.

    Args:
        node: A ``click`` command or group to walk.
        prefix: Command path accumulated so far.

    Returns:
        Space-joined CLI paths, e.g. ``{"classify", "clean orphans"}``.
    """
    if isinstance(node, click.Group):
        return {
            name
            for sub_name, sub in node.commands.items()
            for name in cli_surface_names(sub, (*prefix, sub_name))
        }
    return {" ".join(prefix)}


def registered_cli_commands() -> frozenset[str]:
    """Return the live CLI command inventory.

    Returns:
        Every invocable ``creek`` command path.
    """
    return frozenset(cli_surface_names(typer.main.get_command(app)))


def registered_mcp_tools(vault: Path) -> frozenset[str]:
    """Return the live MCP tool inventory.

    Args:
        vault: Vault the probe server is bound to; no tool is called, so
            nothing is written to it.

    Returns:
        Every registered tool name.
    """
    server = build_server(transport=Transport.STDIO, vault_path=vault)
    return frozenset(tool.name for tool in asyncio.run(server.list_tools()))


def declared_cli_mode_variants() -> frozenset[str]:
    """Return every value-dispatched CLI mode, derived from production.

    Nine commands dispatch on the *value* of a flag, and the dispatch table is
    where features get orphaned (#580 / #581 / #577 were report generators
    with no route to them). Every set below is read from the constant that
    routes it, so a new mode cannot be added without appearing here.

    ``"wavelength"`` is appended by hand for one reason, pinned by
    :func:`test_report_error_message_lists_exactly_the_declared_types`: it is
    special-cased in ``creek report`` because it needs ``--period``, and so is
    genuinely absent from :data:`creek.cli._REPORT_DISPATCH`. Enumerating the
    dict alone silently under-covers the surface by one. That is also why this
    function reads the *dispatch dict* rather than
    :data:`creek.surface_modes.REPORT_TYPES`: the declared tuple is what both
    frontends advertise, so comparing it against itself would prove nothing,
    whereas comparing it against the dict plus the one special case proves the
    declaration still describes the code.

    Returns:
        Strings of the form ``"<command>.<mode>"``.
    """
    return frozenset(
        {f"report.{name}" for name in (*_REPORT_DISPATCH, "wavelength")}
        | {f"link.{name}" for name in LINK_METHODS}
        | {f"classify.{name}" for name in _CLASSIFY_METHODS}
        | {f"ingest.{name}" for name in INGESTOR_REGISTRY}
        | {f"save.{target.value}" for target in SaveTarget}
        | {f"discord.{mode.value}" for mode in DiscordMode}
    )


# ---------------------------------------------------------------------------
# The contract vocabulary
# ---------------------------------------------------------------------------


class Shape(StrEnum):
    """How a surface makes itself observable."""

    WRITES = "writes-an-artefact"
    FRONTMATTER = "mutates-frontmatter-in-place"
    REPORTS = "prints-a-judgement"
    REMOVES = "deletes-vault-content"


@dataclass(frozen=True)
class Effect:
    """The named observable a surface must produce.

    Attributes:
        writes: Glob patterns, relative to the target directory. Each must
            match nothing before the run and something after.
        removes: Glob patterns that must match before the run and nothing
            after.
        contains: Substrings that must appear in the concatenation of every
            file matched by ``writes``. This is the field that catches a
            dormant guard: it names the feature-specific key a downstream
            reader looks for.
        prints: Substrings that must appear in the surface's output. Requires
            a :class:`Contrast`.
        absent: Substrings that must **not** appear in the surface's output.
        frontmatter: ``{fragment id: value}`` read back off disk as one exact
            map. The classify entries seed it to span three tiers, because a
            single-valued map cannot be told apart from a fail-closed default.
        frontmatter_key: Which frontmatter key ``frontmatter`` reads.
        exit_code: Expected CLI exit status.
    """

    writes: tuple[str, ...] = ()
    removes: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    prints: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    frontmatter: Mapping[str, str] = field(default_factory=dict)
    frontmatter_key: str = "privacy_tier"
    exit_code: int = 0

    def is_filesystem_backed(self) -> bool:
        """Report whether the observable is proven by on-disk state.

        Returns:
            True when the free "not run" control applies.
        """
        return bool(self.writes or self.removes or self.frontmatter)


@dataclass(frozen=True)
class Contrast:
    """The negative control for a printed judgement.

    Attributes:
        why: What makes this world one in which the observable must be absent.
        seeded: Run against the seeded corpus (``True``) or a bare vault.
        prepared: Run the surface's ``prepare`` steps first.
        argv: Alternative CLI arguments, or ``None`` to reuse the entry's.
        kwargs: Alternative MCP arguments, or ``None`` to reuse the entry's.
        stdin: Alternative interactive input, or ``None`` to reuse the entry's.
    """

    why: str
    seeded: bool = False
    prepared: bool = True
    argv: tuple[str, ...] | None = None
    kwargs: Mapping[str, Any] | None = None
    stdin: str | None = None


@dataclass(frozen=True)
class Refusal:
    """The closed-gate half of a two-sided gate proof.

    Attributes:
        reason: Substring of the documented refusal message.
        argv: CLI arguments that leave the gate closed.
        kwargs: MCP arguments that leave the gate closed.
        mutate: Vault-relative path -> content, written *before* the run so a
            gate whose trigger is pre-existing local state can be declared at
            all. ``skills sync`` refuses on *drift*, which cannot exist in a
            vault straight from ``creek init``; without this the drift gate
            would be undeclarable and #1306 would have stayed invisible here.
            The write lands before the harness takes its baseline digest, so
            the mutated bytes become the "untouched" reference.
        exit_code: Expected CLI exit status.
    """

    reason: str
    argv: tuple[str, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    mutate: Mapping[str, str] = field(default_factory=dict)
    exit_code: int = 1


@dataclass(frozen=True)
class Surface:
    """One CLI command or MCP tool, and the effect it must produce.

    Attributes:
        shape: How the surface makes itself observable.
        why: Which wiring bug an entry of this shape would have caught. Must
            cite an issue number.
        derived_from: ``"module:symbol"`` citations for every production
            constant this entry's expectation is derived from.
        argv: CLI arguments after the command path.
        kwargs: MCP tool arguments.
        prepare: CLI invocations to run first (chained-precondition surfaces).
        strip: Vault-relative globs the fixture deletes before the run, so a
            surface whose artefacts ``creek init`` already deploys still has
            something to do. Without this, ``skills sync`` could only ever be
            asserted against files that predate it.
        stdin: Input fed to an interactive prompt.
        effect: The named observable.
        contrast: Negative control; required whenever ``effect.prints`` is set.
        refusal: Closed-gate half of a two-sided gate proof.
        target: ``"vault"`` (default) or ``"fresh"`` for a surface such as
            ``init`` whose artefacts land in a directory that does not exist
            yet.
    """

    shape: Shape
    why: str
    derived_from: tuple[str, ...]
    effect: Effect
    argv: tuple[str, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    prepare: tuple[tuple[str, ...], ...] = ()
    strip: tuple[str, ...] = ()
    stdin: str = ""
    contrast: Contrast | None = None
    refusal: Refusal | None = None
    target: str = "vault"


@dataclass(frozen=True)
class Exemption:
    """A surface whose effect cannot be proven hermetically.

    Every exemption carries an *executable* staleness probe so the allowlist
    can only shrink, exactly as ``DORMANT_CONFIG_FIELDS`` does in
    ``tests/test_config_contract.py``. A silent exemption is the same defect
    one level up.

    Attributes:
        reason: Why the effect is unprovable here. Must cite an issue number.
        argv: CLI arguments the blocker probe runs, if any.
        kwargs: MCP arguments the blocker probe runs, if any.
        blocker: Substring the surface must still emit. When the blocker stops
            appearing the surface has become provable and the exemption is
            stale.
        delegate: ``"tests/module.py:needle"`` — a test module that already
            asserts this surface's effect, and a substring proving it invokes
            the surface. Used where the effect is real but lives in a
            dedicated suite; duplicating it here would be theatre.
        seam: ``"module:name"`` (stale once that attribute exists) or
            ``"module:callable:parameter"`` (stale once that keyword parameter
            exists). Used where the blocker is the *absence of an injection
            point*: the day someone adds the seam, the effect becomes provable
            and this exemption must go.
    """

    reason: str
    argv: tuple[str, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    blocker: str = ""
    delegate: str = ""
    seam: str = ""


_ISSUE_REFERENCE: Final = re.compile(r"#\d+")

# Paths every surface touches merely by running. An effect that lives only
# here proves the decorator fired, not that the feature is wired.
_BOOKKEEPING_GLOBS: Final[frozenset[str]] = frozenset(
    {
        "00-Creek-Meta/audit/mcp.jsonl",
        "00-Creek-Meta/audit/purge.jsonl",
        "00-Creek-Meta/Processing-Log/provenance.jsonl",
        "00-Creek-Meta/Processing-Log/consent-log.json",
        "00-Creek-Meta/Processing-Log/run-summary.jsonl",
    },
)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

CANARY: Final = "the creek writes the stone's biography in silt"
"""Phrase seeded into the corpus; its appearance downstream proves data flow."""

_CONFESSIONAL_ID: Final = "frag-conf-0001"
_SHIPPING_ID: Final = "frag-ship-0001"
_ESSAY_ID: Final = "frag-essay-001"
_DANGLING_ID: Final = "frag-orphan-01"
_ECHO_ID: Final = "frag-echo-0001"

_ESSAY_BODY: Final = (
    f"The creek does not argue with the stone. It goes around, and in going "
    f"around, {CANARY}. Attention is the only currency that compounds without "
    f"interest, and the ledger is written in weather."
)

_EXPECTED_TIER_MAP: Final[Mapping[str, str]] = {
    _CONFESSIONAL_ID: "intimate",
    _DANGLING_ID: "personal",
    _ECHO_ID: "personal",
    _ESSAY_ID: "personal",
    _SHIPPING_ID: "open",
}
"""Exact ``{id: privacy_tier}`` map ``creek classify --method rules`` must write.

Three distinct tiers across five fragments, deliberately. The rules classifier
fails **closed**, so a map whose every value is ``"intimate"`` is what a
*broken* wire also produces; and every value ``"unclassified"`` is what #935
produced while exiting 0. Only a multi-valued map distinguishes a live
classifier from a default.
"""

_ANCIENT: Final = datetime(2001, 1, 1, tzinfo=UTC).timestamp()

_PURGE_CONFIRM_PHRASE: Final = "I understand this is irreversible"
"""Phrase ``creek purge vault`` demands before it will act."""

_ELEVATED_TOKEN: Final = "w" * 32
"""Elevated-authorization token the MCP purge tools are handed as ``{token}``."""


def _seed_corpus(vault: Path) -> None:
    """Write the fragments and hygiene bait every effect assertion reads.

    Args:
        vault: Vault root to seed.
    """
    specs = (
        (
            _CONFESSIONAL_ID,
            "Ninety days",
            SourcePlatform.JOURNAL,
            None,
            "ninety days of sobriety today and the walk home felt shorter.",
        ),
        (
            _SHIPPING_ID,
            "Ingest rewrite",
            SourcePlatform.DISCORD,
            "#general",
            "Shipped the ingest rewrite; the routing table is derived, not retyped.",
        ),
        (_ESSAY_ID, "Creek and stone", SourcePlatform.MARKDOWN, None, _ESSAY_BODY),
        (
            _ECHO_ID,
            "Creek and stone (echo)",
            SourcePlatform.MARKDOWN,
            None,
            _ESSAY_BODY,
        ),
        (
            _DANGLING_ID,
            "Dangling",
            SourcePlatform.MARKDOWN,
            None,
            "This fragment points at [[does-not-exist-anywhere]] and nothing else.",
        ),
    )
    for frag_id, title, platform, channel, body in specs:
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=frag_id,
                title=title,
                source=FragmentSource(
                    platform=platform,
                    author=Authorship.SELF,
                    channel=channel,
                ),
            ),
            body=body,
        )
    # `clean orphans` gates on file age, so the bait must actually be old.
    os.utime(
        vault / "01-Fragments" / "Notes" / f"{_DANGLING_ID}.md", (_ANCIENT, _ANCIENT)
    )
    stale_review = vault / "00-Creek-Meta" / "review-queue-2001-01-01_000000.md"
    stale_review.write_text("# stale review queue\n", encoding="utf-8")
    inbound = vault / "00-Creek-Meta" / "Inbound"
    inbound.mkdir(parents=True, exist_ok=True)
    (inbound / "leak.md").write_text(
        "reach me at bob@example.com or 555-867-5309\n",
        encoding="utf-8",
    )
    # The MCP ingest/scan tools refuse paths outside the vault root, so their
    # source tree has to live inside it.
    staging = inbound / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "canary-note.md").write_text(
        f"# Canary note\n\n{CANARY}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Reading rendered output
# ---------------------------------------------------------------------------

_ANSI_RE: Final = re.compile(r"\x1b\[[0-9;]*m")

# The Unicode Box Drawing block — the ``│``/``─``/``╭`` glyphs Rich frames a
# panel with. Matched as a range so a Rich style change to a different border
# set cannot quietly reintroduce the brittleness :func:`_flatten` removes.
_BOX_DRAWING_RE: Final = re.compile("[─-╿]")


def _flatten(text: str) -> str:
    """Return *text* with ANSI codes, Rich panel borders and wrapping removed.

    Every declaration in this module is a substring of what a surface renders,
    and Rich renders differently depending on whether the process believes it
    owns a terminal. Two consequences make a raw ``in`` test a coin flip on
    the environment rather than a statement about the wiring:

    * **Styling splits a literal.** Typer highlights option names, and Rich
      emits one escape run per style span. ``--mode`` matches both the
      ``option`` and the ``switch`` pattern of ``typer.rich_utils.highlighter``,
      so the spans overlap and the rendered token is
      ``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-mode\\x1b[0m`` — an escape sequence
      lands *between the two hyphens*. The same thing happens to every
      highlighted number: ``Orphans found: 5`` ships as ``Orphans found:
      \\x1b[1;36m5\\x1b[0m``. CI is a terminal (or sets ``FORCE_COLOR``); a
      developer's pytest run usually is not, which is how ten assertions in
      this file passed locally and failed in CI on PR #1255.
    * **Panels wrap and are framed.** A message inside a Rich error box is
      hard-wrapped to the console width with ``│`` borders and padding, so a
      phrase can break mid-sentence with a border sitting between its halves.
      That is issue #1141, where a lengthened ``tmp_path`` moved the wrap into
      the phrase under test.

    Dropping the escapes rejoins split tokens; dropping the box-drawing block
    and collapsing runs of whitespace rejoins wrapped sentences. What survives
    is wording, which is what the contract actually declares. Nothing else is
    touched: case, punctuation and word order are left exactly as rendered, so
    a normalised comparison can never accept a *different* message.

    Args:
        text: Rendered output, as captured.

    Returns:
        The same text reduced to its wording.
    """
    return " ".join(_BOX_DRAWING_RE.sub(" ", _ANSI_RE.sub("", text)).split())


def _says(haystack: str, needle: str) -> bool:
    """Return whether *needle* appears in *haystack*, both flattened first.

    The single comparison every declared ``prints`` / ``absent`` / ``contains``
    substring and every :attr:`Refusal.reason` funnels through, so a new entry
    naming a ``--flag`` cannot reintroduce the split-literal failure and no
    entry needs to know Rich exists. Both sides are flattened because a
    declaration may itself carry a newline the renderer would not.

    Args:
        haystack: Rendered output, or the text of a written artefact.
        needle: The declared substring.

    Returns:
        ``True`` when the declaration is present in the rendered wording.
    """
    return _flatten(needle) in _flatten(haystack)


# ---------------------------------------------------------------------------
# The bench
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What running a surface produced.

    Attributes:
        exit_code: CLI exit status (``0`` for MCP calls, which raise instead).
        output: CLI stdout, or the MCP result serialised as JSON.
        before: Relative path -> content digest, taken before the run.
        after: The same map, taken after.
    """

    exit_code: int
    output: str
    before: Mapping[str, str]
    after: Mapping[str, str]

    def says(self, needle: str) -> bool:
        """Return whether the run rendered *needle*.

        Args:
            needle: The declared substring.

        Returns:
            ``True`` when :func:`_says` finds it in this run's output.
        """
        return _says(self.output, needle)

    def readable(self) -> str:
        """Return the tail of the output as the assertions compare it.

        A failure message that quotes the raw capture is unreadable under
        colour and, worse, misleading: it shows escapes the comparison never
        saw. This shows the flattened wording instead.

        Returns:
            The last 1500 characters of :func:`_flatten` applied to the output.
        """
        return _flatten(self.output)[-1500:]

    def touched(self) -> frozenset[str]:
        """Return every relative path the run added, changed or removed.

        Returns:
            Relative paths whose on-disk state differs across the run.
        """
        keys = set(self.before) | set(self.after)
        return frozenset(k for k in keys if self.before.get(k) != self.after.get(k))


class Bench:
    """Builds vaults and runs surfaces against them.

    One bench per test; every vault is built by the real ``creek init`` code
    path so the contract runs against the tree a user actually gets, marker
    file and starter config included.
    """

    def __init__(self, root: Path) -> None:
        """Create the bench under *root*.

        Args:
            root: Scratch directory owned by pytest's ``tmp_path``.
        """
        self._root = root
        self._runner = CliRunner()
        self._serial = 0
        self._slots: dict[Path, Mapping[str, str]] = {}

    def _init_vault(self, name: str) -> Path:
        """Scaffold a vault through ``creek init``.

        Args:
            name: Sub-directory name under the bench root.

        Returns:
            The vault root.
        """
        vault = self._root / name
        result = self._runner.invoke(app, ["init", "--vault", str(vault)])
        assert result.exit_code == 0, result.output
        return vault

    def seeded_vault(self) -> Path:
        """Return a fresh vault carrying the shared corpus.

        Returns:
            Vault root seeded by :func:`_seed_corpus`.
        """
        self._serial += 1
        vault = self._init_vault(f"seeded-{self._serial}")
        _seed_corpus(vault)
        return vault

    def bare_vault(self) -> Path:
        """Return a fresh vault with no corpus at all.

        Returns:
            Vault root straight from ``creek init``.
        """
        self._serial += 1
        vault = self._init_vault(f"bare-{self._serial}")
        # The staging tree exists on both vaults; only the seeded one holds
        # bait. A contrast must differ by corpus, never by a missing directory.
        (vault / "00-Creek-Meta" / "Inbound" / "staging").mkdir(parents=True)
        return vault

    def source_dir(self) -> Path:
        """Return a source tree holding one ingestable markdown note.

        Returns:
            Directory containing ``note.md``.
        """
        self._serial += 1
        source = self._root / f"source-{self._serial}"
        source.mkdir()
        (source / "note.md").write_text(
            f"# Canary note\n\n{CANARY}\n", encoding="utf-8"
        )
        return source

    def slots(self, vault: Path) -> Mapping[str, str]:
        """Return the placeholder substitutions available to an entry.

        Cached per vault so a caller can resolve ``{fresh}`` or ``{source}``
        before a run and still be looking at the same directory afterwards.

        Args:
            vault: The vault this run targets.

        Returns:
            Mapping of ``{name}`` placeholders to absolute paths.
        """
        cached = self._slots.get(vault)
        if cached is not None:
            return cached
        self._serial += 1
        slots = {
            "vault": str(vault),
            "fresh": str(self._root / f"fresh-{self._serial}"),
            "source": str(self.source_dir()),
            "essay": str(vault / "01-Fragments" / "Notes" / f"{_ESSAY_ID}.md"),
            "inbound": str(vault / "00-Creek-Meta" / "Inbound"),
            "out": str(vault / "00-Creek-Meta" / "health.md"),
            "staging": str(vault / "00-Creek-Meta" / "Inbound" / "staging"),
            "token": _ELEVATED_TOKEN,
        }
        self._slots[vault] = slots
        return slots

    def run_cli(
        self,
        command: str,
        argv: tuple[str, ...],
        *,
        vault: Path,
        target: Path,
        prepare: tuple[tuple[str, ...], ...] = (),
        strip: tuple[str, ...] = (),
        stdin: str = "",
    ) -> Outcome:
        """Invoke a CLI surface and snapshot the target directory around it.

        Args:
            command: Space-joined command path, e.g. ``"clean orphans"``.
            argv: Arguments after the command path, with ``{}`` placeholders.
            vault: Vault the command operates on.
            target: Directory whose contents are snapshotted.
            prepare: Commands to run first, as full argv tuples.
            strip: Vault-relative globs to delete before the run.
            stdin: Input for interactive prompts.

        Returns:
            The run's :class:`Outcome`.
        """
        slots = self.slots(vault)
        for pattern in strip:
            for path in vault.glob(pattern):
                if path.is_file():
                    path.unlink()
        for step in prepare:
            self._runner.invoke(app, [part.format(**slots) for part in step])
        before = _digest_tree(target)
        # ``creek.cli.author_llm_factory`` is the CLI twin of the factories
        # ``build_server`` takes (#1254): the one seam that lets `creek author`
        # be voiced hermetically. Installed for every run because a CLI
        # invocation carries no injection channel of its own; every other
        # command ignores it.
        with mock.patch.object(creek_cli, "author_llm_factory", _author_llm_double):
            result = self._runner.invoke(
                app,
                [*command.split(), *(part.format(**slots) for part in argv)],
                input=stdin,
            )
        # A surface that dies at a client boundary raises rather than printing,
        # and the exemption staleness probes need to see that reason too.
        detail = "" if result.exception is None else f"\n{result.exception!r}"
        return Outcome(
            result.exit_code,
            result.output + detail,
            before,
            _digest_tree(target),
        )

    def run_mcp(
        self,
        tool: str,
        kwargs: Mapping[str, Any],
        *,
        vault: Path,
        prepare: tuple[tuple[str, ...], ...] = (),
    ) -> Outcome:
        """Call an MCP tool on a real in-process server and snapshot the vault.

        The LLM factories ``build_server`` accepts are the production seam for
        provider injection, so a recording double proves the wire without a
        key — including ``author_llm_factory``, added by #1254 so the wire past
        the Writing Desk stopped being an exemption and became an assertion.

        Args:
            tool: Registered tool name.
            kwargs: Tool arguments, with ``{}`` placeholders.
            vault: Vault the server is bound to.
            prepare: CLI commands to run first.

        Returns:
            The call's :class:`Outcome`, with the structured result as output.
        """
        slots = self.slots(vault)
        for step in prepare:
            self._runner.invoke(app, [part.format(**slots) for part in step])
        server = build_server(
            transport=Transport.STDIO,
            vault_path=vault,
            draft_llm_factory=lambda _tier: lambda _prompt: _LLM_CANARY,
            compile_llm_factory=lambda _tier: lambda _prompt: _LLM_CANARY,
            reflect_llm_factory=lambda: lambda _tier: lambda _prompt: _REFLECT_CANARY,
            author_llm_factory=_author_llm_double,
        )
        args = {
            key: value.format(**slots) if isinstance(value, str) else value
            for key, value in kwargs.items()
        }
        before = _digest_tree(vault)
        raw = asyncio.run(server.call_tool(tool, args))
        structured = raw[1] if isinstance(raw, tuple) else raw
        return Outcome(
            0, json.dumps(structured, default=str), before, _digest_tree(vault)
        )


_LLM_CANARY: Final = f"LLM-DOUBLE-SPOKE. {CANARY}"
"""Text the injected provider double returns; it must reach the saved artefact."""

_REFLECT_CANARY: Final = json.dumps(
    {"notes": [{"quote": "I rest sometimes", "kind": "reframe", "note": "Yours."}]},
)

_AUTHOR_CANARY: Final = "AUTHOR-DOUBLE-SPOKE"
"""Prefix the injected author voice double emits; it must reach the draft body.

Proves the *injected client* was consulted rather than the desk's deterministic
rendering — the half of #460/#649/#658 that no other gate could see.
"""

_SHOUTED_CLAIM: Final = "INGEST REWRITE"
"""The seeded ``Ingest rewrite`` claim as the double shouts it back.

The second half, and the reason :class:`_AuthorVoiceDouble` uppercases rather
than echoing verbatim. Every corpus string the voice prompt carries — claim
excerpts, fragment ids — the MCP envelope *also* reports in ``provenance`` and
``claims``, whether or not a model ever ran; a verbatim needle would therefore
be satisfied by a dead wire. Uppercased, the needle exists nowhere but on the
far side of the client, so one assertion covers the whole path: corpus →
evidence → prompt → injected client → body → rendered output.
"""


class _AuthorVoiceDouble:
    """Provider double for the author desk's voice call.

    Shouts back the prompt it was handed under :data:`_AUTHOR_CANARY`, so the
    contract can read the corpus out of the far end of the wire instead of
    trusting that a fixed reply means the evidence arrived.
    """

    is_cloud = False
    """The double never leaves the process, so the cloud-consent gate is moot."""

    @property
    def model(self) -> str:
        """Return the double's model identifier.

        Returns:
            A name no real backend answers to.
        """
        return "author-voice-double"

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
        """Return the voice prompt shouted back under the canary prefix.

        Args:
            prompt: The dynamic voice prompt carrying the assembled evidence.
            max_tokens: Ignored; the double generates nothing to truncate.
            system: The static prompt prefix, shouted with the rest.

        Returns:
            A :class:`Completion` whose text is the prefix plus the whole
            prompt in upper case — see :data:`_SHOUTED_CLAIM` for why the case
            change is load-bearing rather than decorative.
        """
        del max_tokens
        spoken = f"{_AUTHOR_CANARY}\n\n{system or ''}\n\n{prompt}"
        return Completion(text=spoken.upper())


def _author_llm_double(_tier: PrivacyTier | None) -> AuthorLLMClient:
    """Return the voice client both author surfaces are injected with.

    Args:
        _tier: The run's content tier, which the double does not route on.

    Returns:
        An :class:`AuthorLLMClient` wrapping :class:`_AuthorVoiceDouble`.
    """
    return AuthorLLMClient(_AuthorVoiceDouble())


def _digest_tree(root: Path) -> dict[str, str]:
    """Return a relative-path -> content-digest map for every file under *root*.

    Args:
        root: Directory to snapshot; a missing directory yields an empty map.

    Returns:
        Mapping used to compute what a run touched.
    """
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): f"{path.stat().st_size}:{hash(path.read_bytes())}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    """Provide a per-test :class:`Bench`.

    Args:
        tmp_path: pytest-owned scratch directory.

    Returns:
        A bench rooted at *tmp_path*.
    """
    return Bench(tmp_path)


@pytest.fixture
def elevated_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the MCP elevated-authorization token for the duration of a test.

    Args:
        monkeypatch: Restores the environment at teardown.

    Returns:
        The token value the purge tools must be handed.
    """
    monkeypatch.setenv(ELEVATED_TOKEN_ENV, _ELEVATED_TOKEN)
    return _ELEVATED_TOKEN


# ---------------------------------------------------------------------------
# The CLI contract
# ---------------------------------------------------------------------------

_BARE = "an unseeded vault has no corpus, so the corpus-derived verdict must vanish"

_AUTHOR_ARGV: Final[tuple[str, ...]] = (
    "--vault",
    "{vault}",
    "--query",
    "creek and stone",
    "--include-tier",
    "all",
)
"""Arguments both ``author`` runs share; the ceiling matches the MCP twin's.

``--include-tier all`` is not decoration: the seeded corpus is deliberately
left unclassified, and under the default ``open`` ceiling the desk gathers no
evidence at all, so the prompt would carry nothing for the double to shout
back. The MCP entry admits the same corpus through ``_ALL``; the two surfaces
must be asked the same question or the comparison between them is worthless.
"""

CLI_CONTRACT: Final[Mapping[str, Surface]] = {
    "init": Surface(
        shape=Shape.WRITES,
        why="#1025: the scaffold and the writers' routing constants drifted apart",
        derived_from=("creek.scaffold:VAULT_TEMPLATE_DIR",),
        argv=("--vault", "{fresh}"),
        target="fresh",
        effect=Effect(
            writes=(
                "00-Creek-Meta/creek_config.yaml",
                "00-Creek-Meta/Skills/*.SKILL.md",
                "01-Fragments/**/*",
            ),
            contains=("vault_path",),
        ),
    ),
    "index": Surface(
        shape=Shape.WRITES,
        why="#1231: `creek index` exited 0 having written nothing",
        derived_from=("creek.generate.indexes:IndexGenerator",),
        argv=("--vault", "{vault}"),
        effect=Effect(
            writes=(
                "00-Creek-Meta/Source-Index.md",
                "00-Creek-Meta/Temporal-Index.md",
                "02-Threads/Thread-Index.md",
                "03-Eddies/Eddy-Map.md",
                "06-Frequencies/*/F*-Index.md",
            ),
        ),
    ),
    "state": Surface(
        shape=Shape.WRITES,
        why="#1231: a report command that writes nothing still exits 0",
        derived_from=("creek.generate.state:StateReportGenerator",),
        argv=("--vault", "{vault}"),
        effect=Effect(
            writes=("00-Creek-Meta/State/latest.md",),
            contains=("Frequency distribution",),
        ),
    ),
    "state-budget": Surface(
        shape=Shape.REPORTS,
        why="#1024: a chained gate that silently skips reads exactly like a pass",
        derived_from=("creek.generate.state_budget:check_budget",),
        argv=("--vault", "{vault}"),
        prepare=(("state", "--vault", "{vault}"),),
        effect=Effect(prints=("/ budget",), absent=("skipped",)),
        contrast=Contrast(
            why="without the producer the gate must say it skipped, not that it passed",
            seeded=True,
            prepared=False,
        ),
    ),
    "lint": Surface(
        shape=Shape.WRITES,
        why="#1040: the draft-grounding check reported every vault clean while dormant",
        derived_from=("creek.lint.runner:LintRunner",),
        argv=("--vault", "{vault}"),
        effect=Effect(
            writes=("00-Creek-Meta/Processing-Log/lint-*.md",),
            contains=("draft-grounding", "broken-links"),
        ),
    ),
    "classify": Surface(
        shape=Shape.FRONTMATTER,
        why=(
            "#935: classify ran but PrivacyClassifier was never called, so the "
            "tiers it wrote were defaults"
        ),
        derived_from=("creek.classify.privacy:PrivacyClassifier",),
        argv=("--vault", "{vault}", "--method", "rules"),
        effect=Effect(frontmatter=_EXPECTED_TIER_MAP),
    ),
    "link": Surface(
        shape=Shape.WRITES,
        why="#1024: a linker stage that computes nothing still reports success",
        derived_from=("creek.surface_modes:LINK_METHODS",),
        argv=("--vault", "{vault}", "--method", "embeddings"),
        effect=Effect(writes=("00-Creek-Meta/embeddings.parquet",)),
    ),
    "report": Surface(
        shape=Shape.WRITES,
        why="#580/#581/#577: report generators orphaned from any command",
        derived_from=(
            "creek.cli:_REPORT_DISPATCH",
            "creek.generate.tags:TagGardenGenerator",
        ),
        argv=("--vault", "{vault}", "--type", "tags"),
        effect=Effect(writes=("00-Creek-Meta/Tag-Garden.md",)),
    ),
    "fill": Surface(
        shape=Shape.WRITES,
        why="#580: a fan-out command can run every stage and land nothing",
        derived_from=("creek.generate.indexes:IndexGenerator",),
        argv=("--vault", "{vault}"),
        effect=Effect(
            writes=(
                "00-Creek-Meta/Source-Index.md",
                "00-Creek-Meta/embeddings.parquet",
            ),
        ),
    ),
    "mine": Surface(
        shape=Shape.WRITES,
        why="#1024: a miner with no corpus wire still prints a full seed table",
        derived_from=("creek.generate.mining:IdeaMiner",),
        argv=("--vault", "{vault}"),
        effect=Effect(
            writes=("00-Creek-Meta/Processing-Log/compile-gaps.jsonl",),
            prints=("Idea seeds",),
        ),
        contrast=Contrast(why="no vault at all means no seed table", seeded=False),
    ),
    "author": Surface(
        shape=Shape.REPORTS,
        why=(
            "#460/#649/#658: `creek author` printed 'No LLM provider available — "
            "rendering the deterministic stub', exited 0 and wrote nothing; "
            "#1027 could only exempt it, because until #1254 the command built "
            "its voice client internally and no test could reach past the desk"
        ),
        derived_from=(
            "creek.cli:author_llm_factory",
            "creek.author.voice:VoiceAgent",
        ),
        argv=_AUTHOR_ARGV,
        effect=Effect(prints=(_AUTHOR_CANARY, _SHOUTED_CLAIM)),
        contrast=Contrast(
            why=(
                "--dry-run plans and counts evidence without voicing, so neither "
                "the injected client's words nor the corpus text it would have "
                "been handed may reach a body"
            ),
            argv=(*_AUTHOR_ARGV, "--dry-run"),
            seeded=True,
        ),
    ),
    "save": Surface(
        shape=Shape.WRITES,
        why="#580: `save` routes by target; an unrouted target writes nowhere",
        derived_from=("creek.save:SaveTarget", "creek.save:TARGET_SUBDIRS"),
        argv=(
            "--vault",
            "{vault}",
            "--target",
            "observation",
            "--title",
            "Canary",
            "--tier",
            "open",
            "--body",
            "-",
        ),
        stdin=CANARY,
        effect=Effect(writes=("05-Wavelength/Observations/*.md",), contains=(CANARY,)),
    ),
    "ingest": Surface(
        shape=Shape.WRITES,
        why="#1024: an ingestor registered but unrouted creates no fragment",
        derived_from=("creek.ingest:INGESTOR_REGISTRY",),
        argv=(
            "--vault",
            "{vault}",
            "--type",
            "markdown",
            "--input",
            "{source}",
            "--yes",
        ),
        effect=Effect(
            writes=("01-Fragments/Notes/*Canary-note.md",), contains=(CANARY,)
        ),
        refusal=Refusal(
            reason="--type and --input are required",
            argv=("--vault", "{vault}"),
            exit_code=2,
        ),
    ),
    "redact": Surface(
        shape=Shape.REPORTS,
        why="#1024: a scanner that reads nothing reports 'no findings' identically",
        derived_from=("creek.redact.scanner:RedactionScanner",),
        argv=("--scan", "--source", "{inbound}", "--vault", "{vault}", "--report"),
        effect=Effect(prints=("email",)),
        contrast=Contrast(
            why="the same staging tree without seeded PII must yield no finding",
            seeded=False,
        ),
        refusal=Refusal(
            reason="--scan requires --source",
            argv=("--scan", "--vault", "{vault}"),
            exit_code=2,
        ),
    ),
    "review": Surface(
        shape=Shape.FRONTMATTER,
        why=(
            "#1024: persisting every decision identically is indistinguishable "
            "from persisting none"
        ),
        derived_from=("creek.models:Fragment",),
        argv=(
            "--vault",
            "{vault}",
        ),
        stdin="a\na\na\na\na\n",
        effect=Effect(
            prints=("Review complete: 5 accepted",),
            frontmatter=dict.fromkeys(_EXPECTED_TIER_MAP, "manual"),
            frontmatter_key="classification_method",
        ),
        contrast=Contrast(
            why="deferring every fragment must persist a different decision",
            seeded=True,
            stdin="d\nd\nd\nd\nd\n",
        ),
    ),
    "process": Surface(
        shape=Shape.WRITES,
        why="#821: a pipeline stage can be orphaned and the fan-out still exits 0",
        derived_from=("creek.pipeline:Pipeline",),
        argv=("--vault", "{vault}", "--source", "{source}", "--yes", "--no-llm"),
        effect=Effect(
            writes=(
                "01-Fragments/Notes/*Canary-note.md",
                "00-Creek-Meta/Source-Index.md",
                # #1303: Stage 5 used to compute the whole link graph and
                # persist none of it. The parquet is the one link artefact
                # a single-fragment canary vault can produce (one fragment
                # cannot reach ``eddy_min_fragments``), and it is absent at
                # the broken revision — so it is a real tripwire, not a
                # tautology. Deliberately NOT ``03-Eddies/*.md``: the
                # IndexGenerator's ``Eddy-Map.md`` would match that on
                # every run, orphaned stage or not.
                "00-Creek-Meta/embeddings.parquet",
            ),
            contains=(CANARY,),
        ),
    ),
    "sync": Surface(
        shape=Shape.REPORTS,
        why="#1024: a planner branch wired to nothing prints the same banner",
        derived_from=("creek.sync.schedule:render_crontab",),
        argv=("--dry-run", "--vault", "{vault}"),
        effect=Effect(prints=("dry-run", "plan")),
        contrast=Contrast(
            why=(
                "the --status branch must not render a plan; if it does, the "
                "two modes are not distinct"
            ),
            argv=("--status", "--vault", "{vault}"),
            seeded=True,
        ),
    ),
    "gdrive": Surface(
        shape=Shape.REPORTS,
        why="#1024: a credential doctor that never inspects prints 'OK' regardless",
        derived_from=("creek.ingest.gdrive:GoogleDriveDownloader",),
        argv=("--check",),
        effect=Effect(prints=("Credentials (credentials.json): MISSING",)),
        contrast=Contrast(
            why="the --revoke branch reports on the token, never on credentials.json",
            argv=("--revoke",),
            seeded=True,
        ),
        refusal=Refusal(
            reason="credentials.json",
            argv=("--download",),
        ),
    ),
    "discord": Surface(
        shape=Shape.REPORTS,
        why="#1024: an unrouted --mode silently ingests nothing and exits 0",
        derived_from=("creek.ingest.discord_dispatch:DiscordMode",),
        argv=("--mode", "data_package", "--package", "{source}", "--vault", "{vault}"),
        effect=Effect(prints=("data_package ingested",)),
        contrast=Contrast(
            why="the exporter mode must not claim the data_package route ran",
            argv=("--mode", "exporter", "--package", "{source}", "--vault", "{vault}"),
            seeded=True,
        ),
        refusal=Refusal(
            reason="Missing option '--mode'",
            argv=("--vault", "{vault}"),
            exit_code=2,
        ),
    ),
    "voice-check": Surface(
        shape=Shape.REPORTS,
        why="#506: voice fidelity was off the reflection node's primary path",
        derived_from=("creek.generate.voice_authenticity:VOICE_DISTANCE_KEY",),
        argv=("{essay}", "--vault", "{vault}"),
        prepare=(("report", "--vault", "{vault}", "--type", "fingerprint"),),
        effect=Effect(prints=("voice_distance",), absent=("Skipping the check",)),
        contrast=Contrast(
            why=(
                "with no corpus behind it there is no fingerprint to measure "
                "against, so the check must skip rather than deliver a verdict"
            ),
            seeded=False,
            prepared=False,
            argv=("{vault}/AGENTS.md", "--vault", "{vault}"),
        ),
    ),
    "voice-authenticity": Surface(
        shape=Shape.REPORTS,
        why="#506: an authenticity report that reads no corpus prints the same shape",
        derived_from=(
            "creek.generate.voice_authenticity:build_voice_authenticity_report",
        ),
        argv=("--vault", "{vault}"),
        effect=Effect(prints=("corpus of 5 eligible fragments",)),
        contrast=Contrast(why=_BARE, seeded=False),
    ),
    "clean orphans": Surface(
        shape=Shape.REPORTS,
        why="#1024: 'judged clean' and 'found nothing to judge' render identically",
        derived_from=("creek.clean.hygiene:OrphanScanner",),
        argv=("--vault", "{vault}", "--age-days", "0"),
        effect=Effect(prints=("Orphans found: 5",)),
        contrast=Contrast(why=_BARE, seeded=False),
    ),
    "clean stale-reviews": Surface(
        shape=Shape.REPORTS,
        why="#1024: an unwired scanner reports zero stale files on every vault",
        derived_from=("creek.clean.hygiene:StaleReviewScanner",),
        argv=("--vault", "{vault}"),
        effect=Effect(prints=("Stale files: 1",)),
        contrast=Contrast(why=_BARE, seeded=False),
    ),
    "clean broken-links": Surface(
        shape=Shape.REPORTS,
        why="#1024: the scan must name the seeded defect, not merely count to zero",
        derived_from=("creek.clean.hygiene:BrokenLinkScanner",),
        argv=("--vault", "{vault}"),
        effect=Effect(prints=("Broken links: 1", "does-not-exist-anywhere")),
        contrast=Contrast(why=_BARE, seeded=False),
    ),
    "clean duplicates": Surface(
        shape=Shape.REPORTS,
        why="#1024: a duplicate scanner wired to nothing finds no duplicates",
        derived_from=("creek.clean.hygiene:DuplicateScanner",),
        argv=("--vault", "{vault}"),
        effect=Effect(prints=("Duplicate candidates: 1",)),
        contrast=Contrast(why=_BARE, seeded=False),
    ),
    "clean report": Surface(
        shape=Shape.WRITES,
        why="#1024: a health summary that aggregates nothing still renders a table",
        derived_from=("creek.clean.hygiene:HygieneReporter",),
        argv=("--vault", "{vault}", "--output", "{out}"),
        effect=Effect(
            writes=("00-Creek-Meta/health.md",),
            contains=("| Total fragments | 5 |", "| Broken links | 1 |"),
        ),
    ),
    "skills generate": Surface(
        shape=Shape.WRITES,
        why="#460: the skill tree existed but no command deployed it",
        derived_from=("creek.generate.skills:SkillTreeGenerator",),
        argv=("--vault", "{vault}", "--generate"),
        effect=Effect(writes=("creek-skills/frequencies/F*.SKILL.md",)),
    ),
    "skills sync": Surface(
        shape=Shape.WRITES,
        why=(
            "#460: the canonical skills shipped in the package but never "
            "reached a vault"
        ),
        derived_from=(
            "creek.scaffold:SKILLS_TEMPLATE_DIR",
            "creek.scaffold:deploy_skills",
        ),
        argv=("--vault", "{vault}", "--force"),
        strip=("00-Creek-Meta/Skills/**/*.md",),
        effect=Effect(
            writes=(
                "00-Creek-Meta/Skills/*.SKILL.md",
                # #1306: the medium contracts are the *other* half of what
                # this command deploys. Declaring only the flat schema skills
                # is what let the drift guard cover one class for a whole
                # release without a single test noticing.
                "00-Creek-Meta/Skills/mediums/*.MEDIUM.md",
            )
        ),
        refusal=Refusal(
            reason="local changes",
            argv=("--vault", "{vault}"),
            mutate={
                "00-Creek-Meta/Skills/mediums/essay.MEDIUM.md": "# my house style\n",
            },
        ),
    ),
    "purge fragment": Surface(
        shape=Shape.REMOVES,
        why="#1024: a purge that deletes nothing prints the same summary",
        derived_from=("creek.purge.engine:PurgeEngine",),
        argv=(_DANGLING_ID, "--vault", "{vault}", "--yes"),
        effect=Effect(removes=(f"01-Fragments/Notes/{_DANGLING_ID}.md",)),
    ),
    "purge source": Surface(
        shape=Shape.REMOVES,
        why="#1024: source-scoped deletion that matches nothing is silent",
        derived_from=("creek.purge.engine:PurgeEngine",),
        argv=("discord", "--vault", "{vault}", "--yes"),
        effect=Effect(removes=(f"01-Fragments/Notes/{_SHIPPING_ID}.md",)),
    ),
    "purge classifications": Surface(
        shape=Shape.REPORTS,
        why=(
            "#935: only a per-fragment read-back distinguishes a real tier "
            "reset from a no-op"
        ),
        derived_from=("creek.purge.engine:PurgeEngine",),
        argv=("--vault", "{vault}", "--yes"),
        prepare=(("classify", "--vault", "{vault}", "--method", "rules"),),
        effect=Effect(prints=("Purge classifications (APPLY)",)),
        contrast=Contrast(
            why="--dry-run must report a dry run, not an apply",
            argv=("--vault", "{vault}", "--dry-run", "--yes"),
            seeded=True,
        ),
    ),
    "purge daterange": Surface(
        shape=Shape.REMOVES,
        why="#1024: a date filter wired to nothing deletes nothing and reports success",
        derived_from=("creek.purge.engine:PurgeEngine",),
        argv=("2000-01-01", "2100-01-01", "--vault", "{vault}", "--yes"),
        effect=Effect(removes=("01-Fragments/Notes/frag-*.md",)),
    ),
    "purge vault": Surface(
        shape=Shape.REMOVES,
        why=(
            "#1024: the confirmation gate is worthless unless the permitted "
            "path really deletes"
        ),
        derived_from=("creek.purge.engine:PurgeEngine",),
        argv=(
            "--vault",
            "{vault}",
            "--force-non-interactive",
            "--confirm-text",
            _PURGE_CONFIRM_PHRASE,
        ),
        effect=Effect(removes=("01-Fragments/Notes/frag-*.md",)),
        refusal=Refusal(
            reason="Refusing to purge vault from a non-interactive session",
            argv=("--vault", "{vault}"),
        ),
    ),
    "compost scan": Surface(
        shape=Shape.REPORTS,
        why="#1024: the candidate scan must read the corpus, not print a fixed banner",
        derived_from=("creek.generate.compost_scan:run_compost_scan",),
        argv=("--vault", "{vault}", "--no-llm", "--dry-run"),
        effect=Effect(prints=("Dry run",)),
        contrast=Contrast(
            why="without --dry-run the command must not claim it wrote nothing",
            argv=("--vault", "{vault}", "--no-llm"),
            seeded=True,
        ),
    ),
}


# ---------------------------------------------------------------------------
# The MCP contract
# ---------------------------------------------------------------------------

_ALL: Final[Mapping[str, Any]] = {"privacy_tier_ceiling": "all"}

MCP_CONTRACT: Final[Mapping[str, Surface]] = {
    "creek.reflect": Surface(
        shape=Shape.REPORTS,
        why="#506: reflection ran off the voice-fidelity path and nobody noticed",
        derived_from=("creek_mcp.server:build_server",),
        kwargs={"content": "I rest sometimes and the work survives.", **_ALL},
        effect=Effect(prints=("reframe", "I rest sometimes")),
        contrast=Contrast(
            why="the verbatim-quote guard must drop a note the entry never said",
            kwargs={"content": "Tarmac resurfacing begins on Tuesday.", **_ALL},
            seeded=True,
        ),
    ),
    "creek.wheel": Surface(
        shape=Shape.REPORTS,
        why="#935: a wheel that reads no frontmatter renders all-zero on every vault",
        derived_from=("creek.models:Fragment",),
        kwargs=dict(_ALL),
        prepare=(("classify", "--vault", "{vault}", "--method", "rules"),),
        effect=Effect(prints=('"unclassified": 5',)),
        contrast=Contrast(why=_BARE, seeded=False),
    ),
    "creek.journal": Surface(
        shape=Shape.WRITES,
        why="#1024: an entry accepted over the wire but never landed in the vault",
        derived_from=("creek.vault.writer:VaultWriter",),
        # ``tier`` is required as of #1494 — ``creek.journal`` refuses a call
        # that omits it rather than defaulting to ``open``, so the contract
        # must name one to exercise the write path at all.
        kwargs={
            "content": CANARY,
            "external_id": "j-canary-1",
            "tier": "open",
            **_ALL,
        },
        effect=Effect(writes=("01-Fragments/Journal/*.md",), contains=(CANARY,)),
    ),
    "creek.upload": Surface(
        shape=Shape.WRITES,
        why="#1024: an upload acknowledged but not ingested",
        derived_from=("creek.ingest:INGESTOR_REGISTRY",),
        # ``tier`` is required as of #1494, exactly as for ``creek.journal``
        # above — an omitted tier is refused before a byte is decoded.
        kwargs={
            "filename": "canary.md",
            "content_base64": base64.b64encode(
                f"# Canary\n\n{CANARY}\n".encode(),
            ).decode(),
            "external_id": "u-canary-1",
            "tier": "open",
            **_ALL,
        },
        effect=Effect(
            writes=("00-Creek-Meta/adepthood/uploads/u-canary-1-*.md",),
            contains=(CANARY,),
        ),
    ),
    "creek.state.read": Surface(
        shape=Shape.REPORTS,
        why=(
            "#1024: a reader with no producer returns 'empty' exactly like a broken one"
        ),
        derived_from=("creek.generate.state:StateReportGenerator",),
        kwargs=dict(_ALL),
        prepare=(("state", "--vault", "{vault}"),),
        effect=Effect(prints=("Frequency distribution",)),
        contrast=Contrast(
            why="without the producer the reader must say empty, not render a report",
            seeded=True,
            prepared=False,
        ),
    ),
    "creek.state.render": Surface(
        shape=Shape.WRITES,
        why="#1231: rendering that writes nothing still returns ok",
        derived_from=("creek.generate.state:StateReportGenerator",),
        kwargs=dict(_ALL),
        effect=Effect(writes=("00-Creek-Meta/State/latest.md",)),
    ),
    "creek.lint": Surface(
        shape=Shape.WRITES,
        why="#1040: the draft-grounding check must be present in the report it writes",
        derived_from=("creek.lint.runner:LintRunner",),
        kwargs=dict(_ALL),
        effect=Effect(
            writes=("00-Creek-Meta/Processing-Log/lint-*.md",),
            contains=("draft-grounding",),
        ),
    ),
    "creek.mine": Surface(
        shape=Shape.WRITES,
        why="#1024: seeds returned over the wire but never recorded",
        derived_from=("creek.generate.mining:IdeaMiner",),
        kwargs=dict(_ALL),
        effect=Effect(writes=("00-Creek-Meta/Processing-Log/compile-gaps.jsonl",)),
    ),
    "creek.draft": Surface(
        shape=Shape.WRITES,
        why=(
            "#1040: the draft saved, exited 0, and carried no derivative_score, so "
            "the draft-grounding lint check called every vault clean"
        ),
        derived_from=(
            "creek.generate.grounding:DERIVATIVE_FRONTMATTER_KEY",
            "creek.generate.grounding:GROUNDING_FRONTMATTER_KEY",
            "creek_mcp.server:build_server",
        ),
        kwargs=dict(_ALL),
        effect=Effect(
            writes=("07-Voice/Drafts/*.md",),
            contains=(
                _LLM_CANARY,
                DERIVATIVE_FRONTMATTER_KEY,
                GROUNDING_FRONTMATTER_KEY,
            ),
        ),
    ),
    "creek.author": Surface(
        shape=Shape.REPORTS,
        why=(
            "#460: the verb answered `status: ok` carrying a deterministically "
            "stubbed body, indistinguishable from a live one, until #1254 gave "
            "`build_server` the author factory its three siblings already had"
        ),
        derived_from=(
            "creek_mcp.server:build_server",
            "creek.author.conductor:run_author",
        ),
        kwargs={"query": "creek and stone", **_ALL},
        effect=Effect(prints=(_AUTHOR_CANARY, _SHOUTED_CLAIM)),
        contrast=Contrast(
            why=(
                "a dry run returns the plan and evidence *counts*; a voiced body "
                "must be absent from it"
            ),
            kwargs={"query": "creek and stone", "dry_run": True, **_ALL},
            seeded=True,
        ),
    ),
    "creek.save": Surface(
        shape=Shape.WRITES,
        why="#580: the save target routing table is where artefacts get orphaned",
        derived_from=("creek.save:SaveTarget", "creek.save:TARGET_SUBDIRS"),
        # ``tier`` is required as of #1434 — ``creek.save`` refuses a call
        # that omits it rather than defaulting to ``open``, so the contract
        # must name one to exercise the write path at all.
        kwargs={
            "target": "observation",
            "title": "Canary obs",
            "body": CANARY,
            "tier": "open",
            **_ALL,
        },
        effect=Effect(writes=("05-Wavelength/Observations/*.md",), contains=(CANARY,)),
    ),
    "creek.ingest": Surface(
        shape=Shape.WRITES,
        why="#1024: an ingest verb that acknowledges but creates no fragment",
        derived_from=("creek.ingest:INGESTOR_REGISTRY",),
        kwargs={"source_type": "markdown", "input_path": "{staging}", **_ALL},
        effect=Effect(
            writes=("01-Fragments/Notes/*-Canary-note.md",),
            contains=(CANARY,),
        ),
        refusal=Refusal(
            reason="resolves outside the vault root",
            kwargs={"source_type": "markdown", "input_path": "{source}", **_ALL},
        ),
    ),
    "creek.redact.scan": Surface(
        shape=Shape.REPORTS,
        why="#1024: a scanner scoped to a staging tree must find the PII seeded there",
        derived_from=("creek.redact.scanner:RedactionScanner",),
        kwargs={"input_path": "{inbound}", **_ALL},
        effect=Effect(prints=('"match_type": "email"', '"total_findings": 2')),
        contrast=Contrast(
            why="a bare vault's staging tree holds no seeded PII",
            seeded=False,
        ),
        refusal=Refusal(
            reason="resolves outside the vault root",
            kwargs={"input_path": "{source}", **_ALL},
        ),
    ),
    "creek.classify": Surface(
        shape=Shape.FRONTMATTER,
        why="#935: classify over MCP wrote default tiers while reporting success",
        derived_from=("creek.classify.privacy:PrivacyClassifier",),
        kwargs={"method": "rules", **_ALL},
        effect=Effect(frontmatter=_EXPECTED_TIER_MAP),
    ),
    "creek.link": Surface(
        shape=Shape.WRITES,
        why="#1024: a linker stage that computes nothing still returns counts",
        derived_from=("creek.surface_modes:LINK_METHODS",),
        kwargs={"method": "embeddings", **_ALL},
        effect=Effect(writes=("00-Creek-Meta/embeddings.parquet",)),
        refusal=Refusal(
            reason="unknown method",
            kwargs={"method": "not-a-linker", **_ALL},
        ),
    ),
    "creek.report": Surface(
        shape=Shape.WRITES,
        why="#580/#581/#577: report generators orphaned from any command",
        derived_from=("creek.surface_modes:REPORT_TYPES",),
        kwargs={"report_type": "tags", **_ALL},
        effect=Effect(writes=("00-Creek-Meta/Tag-Garden.md",)),
        refusal=Refusal(
            reason="unsupported report_type",
            kwargs={"report_type": "not-a-report", **_ALL},
        ),
    ),
    "creek.skills.refresh": Surface(
        shape=Shape.WRITES,
        why="#460: the skill tree existed but nothing deployed it over MCP",
        derived_from=("creek.generate.skills:SkillTreeGenerator",),
        kwargs=dict(_ALL),
        effect=Effect(writes=("creek-skills/frequencies/F*.SKILL.md",)),
    ),
    "creek.purge.fragment": Surface(
        shape=Shape.REMOVES,
        why="#1024: an elevated gate is worthless unless the permitted path deletes",
        derived_from=("creek_mcp.auth:ELEVATED_TOKEN_ENV",),
        kwargs={"fragment_id": _DANGLING_ID, "auth_token": "{token}"},
        effect=Effect(removes=(f"01-Fragments/Notes/{_DANGLING_ID}.md",)),
        refusal=Refusal(
            reason="elevated authorization required",
            kwargs={"fragment_id": _DANGLING_ID},
        ),
    ),
    "creek.purge.source": Surface(
        shape=Shape.REMOVES,
        why="#1024: source-scoped deletion that matches nothing is silent",
        derived_from=("creek_mcp.auth:ELEVATED_TOKEN_ENV",),
        kwargs={"source_type": "discord", "auth_token": "{token}"},
        effect=Effect(removes=(f"01-Fragments/Notes/{_SHIPPING_ID}.md",)),
        refusal=Refusal(
            reason="elevated authorization required",
            kwargs={"source_type": "discord"},
        ),
    ),
    "creek.purge.classifications": Surface(
        shape=Shape.REPORTS,
        why=(
            "#935: the apply and dry-run branches must be distinguishable; the "
            "deeper per-fragment effect needs a corpus the rules classifier "
            "cannot produce, tracked as a #1027 follow-up"
        ),
        derived_from=("creek_mcp.auth:ELEVATED_TOKEN_ENV",),
        kwargs={"auth_token": "{token}"},
        prepare=(("classify", "--vault", "{vault}", "--method", "rules"),),
        effect=Effect(prints=('"dry_run": false',)),
        contrast=Contrast(
            why="dry_run=True must report a dry run, not an apply",
            kwargs={"auth_token": "{token}", "dry_run": True},
            seeded=True,
        ),
        refusal=Refusal(reason="elevated authorization required", kwargs={}),
    ),
    "creek.purge.daterange": Surface(
        shape=Shape.REMOVES,
        why="#1024: a date filter wired to nothing deletes nothing and reports success",
        derived_from=("creek_mcp.auth:ELEVATED_TOKEN_ENV",),
        kwargs={"start": "2000-01-01", "end": "2100-01-01", "auth_token": "{token}"},
        effect=Effect(removes=("01-Fragments/Notes/frag-*.md",)),
        refusal=Refusal(
            reason="elevated authorization required",
            kwargs={"start": "2000-01-01", "end": "2100-01-01"},
        ),
    ),
    "creek.purge.vault": Surface(
        shape=Shape.REMOVES,
        why=(
            "#1024: the vault-path confirmation is worthless unless the "
            "permitted path really deletes"
        ),
        derived_from=("creek.purge.engine:PurgeEngine",),
        kwargs={"confirm_vault_path": "{vault}", "auth_token": "{token}"},
        effect=Effect(removes=("01-Fragments/Notes/frag-*.md",)),
        refusal=Refusal(reason="elevated authorization required", kwargs={}),
    ),
}


# ---------------------------------------------------------------------------
# The exemptions
# ---------------------------------------------------------------------------

EXEMPTIONS: Final[Mapping[str, Exemption]] = {
    "draft": Exemption(
        reason=(
            "#1024: `creek draft` builds its provider internally and exposes no "
            "injection seam, so the effect cannot be reached without a key. The "
            "same wire IS proven on the MCP twin, which takes draft_llm_factory "
            "— see MCP_CONTRACT['creek.draft']. Adding the CLI seam is #1040's "
            "unfinished half."
        ),
        argv=("--vault", "{vault}", "--no-llm"),
        blocker="LLM provider unavailable; cannot generate draft",
    ),
    "compile": Exemption(
        reason=(
            "#1024: `creek compile` builds its provider internally; with none "
            "reachable it fails at the client boundary. The MCP twin takes "
            "compile_llm_factory and IS asserted — see MCP_CONTRACT['creek.compile']."
        ),
        seam="creek.cli:compile_llm_factory",
    ),
    "compost calibrate": Exemption(
        reason=(
            "#1024: calibration scores a checked-in labelled fixture and its only "
            "effect is printed metrics, whose numbers already have a home. "
            "Duplicating them here would be theatre."
        ),
        delegate="tests/test_compost_calibration.py:compost",
    ),
    "creek.handshake": Exemption(
        reason=(
            "#750: handshake's effect is that `capabilities` equals the live tool "
            "registry, which is already asserted against `list_tools()` — a "
            "second copy would be the retyped-inventory defect this module exists "
            "to prevent."
        ),
        delegate="tests/test_mcp_server.py:creek.handshake",
    ),
    "creek.compile": Exemption(
        reason=(
            "#1024: `build_server` does expose compile_llm_factory, but the "
            "injected double must return the compile tool's JSON contract, "
            "which is owned and asserted by the compile suite. Duplicating that "
            "schema here would give this module a second copy to drift."
        ),
        delegate="tests/test_mcp_write_tools.py:creek.compile",
    ),
}


# ---------------------------------------------------------------------------
# The mode-dispatch contract
# ---------------------------------------------------------------------------

_VIA_REPORT: Final = (
    "routed by creek.cli:_REPORT_DISPATCH; entry CLI_CONTRACT['report']"
)
_VIA_INGEST: Final = (
    "routed by creek.ingest:INGESTOR_REGISTRY; entry CLI_CONTRACT['ingest']"
)
_VIA_SAVE: Final = "routed by creek.save:SaveTarget; entry CLI_CONTRACT['save']"

MODE_CONTRACT: Final[Mapping[str, str]] = {
    **{f"report.{name}": _VIA_REPORT for name in (*_REPORT_DISPATCH, "wavelength")},
    **{f"ingest.{name}": _VIA_INGEST for name in INGESTOR_REGISTRY},
    **{f"save.{target.value}": _VIA_SAVE for target in SaveTarget},
    "link.embeddings": "entry CLI_CONTRACT['link'] and MCP_CONTRACT['creek.link']",
    "link.temporal": (
        "routed by creek.surface_modes:LINK_METHODS; entry CLI_CONTRACT['link']"
    ),
    "link.eddies": (
        "routed by creek.surface_modes:LINK_METHODS; entry CLI_CONTRACT['link']"
    ),
    "link.threads": (
        "routed by creek.surface_modes:LINK_METHODS; reachable over MCP since "
        "#1252 — test_link_reaches_the_threads_linker in "
        "tests/test_mcp_write_tools.py"
    ),
    "classify.rules": (
        "entry CLI_CONTRACT['classify'] and MCP_CONTRACT['creek.classify']"
    ),
    "classify.llm": (
        "needs a provider; exempt for the same reason as EXEMPTIONS['draft'] (#1024)"
    ),
    "discord.data_package": "entry CLI_CONTRACT['discord']",
    "discord.exporter": "entry CLI_CONTRACT['discord'] contrast arm",
    "discord.bot_capture": (
        "needs a live gateway connection; `live` marker territory (#1024)"
    ),
}


# ---------------------------------------------------------------------------
# Inventory ratchets — derived from the live objects, never retyped
# ---------------------------------------------------------------------------


def test_every_cli_command_is_declared(bench: Bench) -> None:
    """Every registered CLI command has a contract entry or an exemption.

    Args:
        bench: Unused here beyond forcing a clean temp root.
    """
    registered = registered_cli_commands()
    declared = {
        name for name in (*CLI_CONTRACT, *EXEMPTIONS) if not name.startswith("creek.")
    }

    assert registered == declared, (
        f"CLI surfaces with no wiring contract: {sorted(registered - declared)} "
        f"— add an entry to CLI_CONTRACT naming the observable the command "
        f"produces, or an EXEMPTIONS entry with an executable staleness probe "
        f"(docs/wiring-contract.md). Stale entries naming commands that no "
        f"longer exist: {sorted(declared - registered)}."
    )


def test_every_mcp_tool_is_declared(bench: Bench) -> None:
    """Every registered MCP tool has a contract entry or an exemption.

    Args:
        bench: Supplies the vault the probe server binds to.
    """
    registered = registered_mcp_tools(bench.bare_vault())
    declared = {
        name for name in (*MCP_CONTRACT, *EXEMPTIONS) if name.startswith("creek.")
    }

    assert registered == declared, (
        f"undeclared MCP tools: {sorted(registered - declared)}; "
        f"stale entries: {sorted(declared - registered)}"
    )


def test_every_cli_mode_variant_is_declared() -> None:
    """Every value-dispatched mode is declared, in both directions."""
    live = declared_cli_mode_variants()
    declared = set(MODE_CONTRACT)

    assert live == declared, (
        f"undeclared modes: {sorted(live - declared)}; "
        f"stale modes: {sorted(declared - live)}. Mode tables are where "
        "generators get orphaned (#580/#581/#577)."
    )


def test_report_error_message_lists_exactly_the_declared_types(bench: Bench) -> None:
    """``report``'s own valid-list must equal the derived set.

    ``wavelength`` lives outside :data:`creek.cli._REPORT_DISPATCH`, so the
    dispatch dict alone under-covers the surface. This pins the two together:
    a second special case cannot hide.

    Args:
        bench: Supplies a vault to run against.
    """
    vault = bench.bare_vault()
    outcome = bench.run_cli(
        "report",
        ("--vault", "{vault}", "--type", "definitely-not-a-report"),
        vault=vault,
        target=vault,
    )
    rendered = _flatten(outcome.output)
    listed = {
        name.strip()
        for name in rendered.split("Valid types:")[-1].split(".")[0].split(",")
    }
    expected = {
        name.removeprefix("report.")
        for name in MODE_CONTRACT
        if name.startswith("report.")
    }

    assert listed == expected, (
        f"`creek report` advertises {sorted(listed)} but the derived set is "
        f"{sorted(expected)}"
    )


def _listed_after(rendered: str, marker: str) -> set[str]:
    """Return the comma-separated names a surface advertises after *marker*.

    Both parity tests below read each surface's **own** rejection message
    rather than its module constant. Reading the constant would be circular
    once #1252/#1253 land — the two frontends import the same tuple, so
    ``set(X) == set(X)`` would pass whatever the surfaces actually do. What a
    caller can reach is what the caller is told, so that is what is compared.

    Args:
        rendered: The surface's message, ANSI- and panel-flattened.
        marker: The label the list follows (``"supported:"``).

    Returns:
        The advertised names, lowercased and stripped.
    """
    tail = rendered.lower().split(marker.lower())[-1].split(".")[0]
    return {name.strip() for name in tail.split(",") if name.strip()}


def test_cli_and_mcp_agree_on_link_methods(bench: Bench) -> None:
    """The MCP linker must reach every method the CLI routes.

    #1252: ``creek_mcp.tools.link`` carried a retyped copy of the CLI's method
    tuple that had lost ``"threads"`` — the linker #880 fixed, and the one with
    the largest measured output on a real vault. Both surfaces now read one
    declaration, and this asserts it through what each one *tells a caller*.

    Args:
        bench: Supplies a vault to run against.
    """
    vault = bench.bare_vault()
    outcome = bench.run_cli(
        "link",
        ("--vault", "{vault}", "--method", "definitely-not-a-linker"),
        vault=vault,
        target=vault,
    )
    cli_listed = _listed_after(_flatten(outcome.output), "supported:")
    refusal = link_tool(
        vault_path=vault,
        method="definitely-not-a-linker",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    mcp_listed = _listed_after(str(refusal["reason"]), "supported:")

    assert cli_listed == mcp_listed, (
        f"`creek link` advertises {sorted(cli_listed)} but `creek.link` "
        f"advertises {sorted(mcp_listed)}; CLI-only: "
        f"{sorted(cli_listed - mcp_listed)}"
    )


def test_cli_and_mcp_agree_on_report_types(bench: Bench) -> None:
    """The MCP report tool must reach every type the CLI routes.

    #1253: ``creek_mcp.tools.report`` advertised 6 of the 11 types ``creek
    report`` routes, so ``unnamed``, ``fingerprint``, ``paradox``,
    ``synchronicity`` and ``wavelength`` were invisible over MCP. A type that
    cannot be *served* must still be *named* — silent omission is what produced
    the bug — so the two advertised sets must match exactly.

    Args:
        bench: Supplies a vault to run against.
    """
    vault = bench.bare_vault()
    outcome = bench.run_cli(
        "report",
        ("--vault", "{vault}", "--type", "definitely-not-a-report"),
        vault=vault,
        target=vault,
    )
    cli_listed = _listed_after(_flatten(outcome.output), "valid types:")
    refusal = report_tool(
        vault_path=vault,
        report_type="definitely-not-a-report",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    mcp_listed = _listed_after(str(refusal["reason"]), "valid types:")

    assert cli_listed == mcp_listed, (
        f"`creek report` advertises {sorted(cli_listed)} but `creek.report` "
        f"advertises {sorted(mcp_listed)}; CLI-only: "
        f"{sorted(cli_listed - mcp_listed)}"
    )


# ---------------------------------------------------------------------------
# Effect assertions
# ---------------------------------------------------------------------------


def _matches(root: Path, patterns: tuple[str, ...]) -> dict[str, list[Path]]:
    """Resolve each glob pattern against *root*.

    Args:
        root: Directory to search.
        patterns: Glob patterns relative to *root*.

    Returns:
        Pattern -> matching files.
    """
    return {
        pattern: [path for path in sorted(root.glob(pattern)) if path.is_file()]
        for pattern in patterns
    }


def _frontmatter_map(vault: Path, key: str) -> dict[str, str]:
    """Read ``{fragment id: <key>}`` back off disk.

    Args:
        vault: Vault root.
        key: Frontmatter key to read; ``"absent"`` stands in for a missing one.

    Returns:
        One entry per seeded fragment file found.
    """
    return {
        path.stem: str(frontmatter.load(path).metadata.get(key, "absent"))
        for path in sorted((vault / "01-Fragments").rglob("frag-*.md"))
    }


def _assert_writes(surface: Surface, target: Path, outcome: Outcome, name: str) -> None:
    """Assert every declared artefact was absent before the run and present after.

    Args:
        surface: The contract entry under test.
        target: Directory the artefacts land in.
        outcome: The run's outcome.
        name: Surface name, for the failure message.
    """
    for pattern, found in _matches(target, surface.effect.writes).items():
        assert found, (
            f"{name}: declared artefact {pattern!r} was never written. The "
            f"surface exited {outcome.exit_code} regardless — which is exactly "
            f"how #1231 shipped. Output:\n{outcome.readable()}"
        )
        relative = {str(path.relative_to(target)) for path in found}
        assert relative & outcome.touched(), (
            f"{name}: {pattern!r} matched {sorted(relative)} but this run did "
            "not create or change any of them — the artefact predates the run."
        )
    if surface.effect.contains:
        body = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for found in _matches(target, surface.effect.writes).values()
            for path in found
        )
        for needle in surface.effect.contains:
            assert _says(body, needle), (
                f"{name}: the artefact was written but carries no {needle!r}. A "
                "file that appears without its payload is #1040's exact shape."
            )


def _assert_removes(surface: Surface, outcome: Outcome, name: str) -> None:
    """Assert every declared target existed before the run and is gone after.

    Args:
        surface: The contract entry under test.
        outcome: The run's outcome.
        name: Surface name, for the failure message.
    """
    for pattern in surface.effect.removes:
        existed = [key for key in outcome.before if Path(key).match(pattern)]
        assert existed, (
            f"{name}: nothing matched {pattern!r} before the run — bad fixture"
        )
        survivors = [key for key in outcome.after if Path(key).match(pattern)]
        assert not survivors, (
            f"{name}: {sorted(survivors)} survived a purge that reported success. "
            f"Output:\n{outcome.readable()}"
        )


def _assert_output(effect: Effect, outcome: Outcome, name: str) -> None:
    """Assert the declared substrings are present and the forbidden ones absent.

    Args:
        effect: The declared observable.
        outcome: The run's outcome.
        name: Surface name, for the failure message.
    """
    for needle in effect.prints:
        assert outcome.says(needle), (
            f"{name}: declared output {needle!r} never appeared. Output:\n"
            f"{outcome.readable()}"
        )
    for forbidden in effect.absent:
        assert not outcome.says(forbidden), (
            f"{name}: output still carries {forbidden!r}, so the surface did not "
            f"take the path the entry claims. Output:\n{outcome.readable()}"
        )


@pytest.mark.parametrize(("name", "surface"), sorted(CLI_CONTRACT.items()))
def test_cli_surface_produces_its_declared_effect(
    bench: Bench,
    name: str,
    surface: Surface,
) -> None:
    """Every CLI command must produce the observable its entry declares.

    Args:
        bench: The bench.
        name: Command path.
        surface: The contract entry.
    """
    vault = bench.seeded_vault()
    target = (
        vault if surface.target == "vault" else Path(bench.slots(vault)[surface.target])
    )
    outcome = bench.run_cli(
        name,
        surface.argv,
        vault=vault,
        target=target,
        prepare=surface.prepare,
        strip=surface.strip,
        stdin=surface.stdin,
    )

    assert outcome.exit_code == surface.effect.exit_code, (
        f"{name}: expected exit {surface.effect.exit_code}, got "
        f"{outcome.exit_code}. Output:\n{outcome.readable()}"
    )
    _assert_writes(surface, target, outcome, name)
    _assert_removes(surface, outcome, name)
    _assert_output(surface.effect, outcome, name)
    _assert_frontmatter(surface, vault, name)


@pytest.mark.parametrize(("name", "surface"), sorted(MCP_CONTRACT.items()))
def test_mcp_surface_produces_its_declared_effect(
    bench: Bench,
    elevated_token: str,
    name: str,
    surface: Surface,
) -> None:
    """Every MCP tool must produce the observable its entry declares.

    Args:
        bench: The bench.
        elevated_token: Installs the elevated-authorization token the purge
            tools require; ``{token}`` in an entry resolves to it.
        name: Tool name.
        surface: The contract entry.
    """
    assert elevated_token == _ELEVATED_TOKEN
    vault = bench.seeded_vault()
    outcome = bench.run_mcp(name, surface.kwargs, vault=vault, prepare=surface.prepare)

    _assert_writes(surface, vault, outcome, name)
    _assert_removes(surface, outcome, name)
    _assert_output(surface.effect, outcome, name)
    _assert_frontmatter(surface, vault, name)


def _run_contrast(
    bench: Bench,
    name: str,
    surface: Surface,
    contrast: Contrast,
) -> Outcome:
    """Run a surface in the world its contrast describes.

    Args:
        bench: The bench.
        name: Surface name.
        surface: The contract entry.
        contrast: The negative control to realise.

    Returns:
        The contrast run's outcome.
    """
    vault = bench.seeded_vault() if contrast.seeded else bench.bare_vault()
    prepare = surface.prepare if contrast.prepared else ()
    if name in MCP_CONTRACT:
        kwargs = contrast.kwargs if contrast.kwargs is not None else surface.kwargs
        return bench.run_mcp(name, kwargs, vault=vault, prepare=prepare)
    return bench.run_cli(
        name,
        contrast.argv if contrast.argv is not None else surface.argv,
        vault=vault,
        target=vault,
        prepare=prepare,
        strip=surface.strip,
        stdin=surface.stdin if contrast.stdin is None else contrast.stdin,
    )


@pytest.mark.parametrize(
    ("name", "surface"),
    sorted(
        (name, surface)
        for table in (CLI_CONTRACT, MCP_CONTRACT)
        for name, surface in table.items()
        if surface.contrast is not None
    ),
)
def test_declared_contrast_makes_the_observable_vanish(
    bench: Bench,
    elevated_token: str,
    name: str,
    surface: Surface,
) -> None:
    """A printed judgement must be absent in the world its contrast describes.

    This is the assertion that separates a real scanner from a banner. Without
    it, "judged clean" and "found nothing to judge" are the same string, which
    is exactly how a print-only surface hides a dead wire.

    Args:
        bench: The bench.
        elevated_token: Kept installed so a contrast may cross the elevated gate.
        name: Surface name.
        surface: The contract entry, whose ``contrast`` is not ``None``.
    """
    assert elevated_token == _ELEVATED_TOKEN
    contrast = surface.contrast
    assert contrast is not None
    outcome = _run_contrast(bench, name, surface, contrast)

    for needle in surface.effect.prints:
        assert not outcome.says(needle), (
            f"{name}: {needle!r} still appears under the contrast "
            f"({contrast.why}). The declared observable is a constant, not a "
            f"reading of the corpus. Output:\n{outcome.readable()}"
        )
    for restored in surface.effect.absent:
        assert outcome.says(restored), (
            f"{name}: the contrast was supposed to restore {restored!r} "
            f"({contrast.why}) but it never appeared."
        )


@pytest.mark.parametrize(
    ("name", "surface"),
    sorted(
        (name, surface)
        for table in (CLI_CONTRACT, MCP_CONTRACT)
        for name, surface in table.items()
        if surface.refusal is not None
    ),
)
def test_declared_gate_refuses_and_leaves_the_vault_untouched(
    bench: Bench,
    elevated_token: str,
    name: str,
    surface: Surface,
) -> None:
    """A gate must refuse with its documented reason and change nothing.

    The other half — that the same surface *does* act once the gate is
    satisfied — is asserted by the effect test above. One half alone is passed
    by a surface that refuses everything.

    Args:
        bench: The bench.
        elevated_token: Installed but deliberately not handed to the tool.
        name: Surface name.
        surface: The contract entry, whose ``refusal`` is not ``None``.
    """
    assert elevated_token == _ELEVATED_TOKEN
    refusal = surface.refusal
    assert refusal is not None
    vault = bench.seeded_vault()
    for relative, content in refusal.mutate.items():
        seeded = vault / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(content, encoding="utf-8")

    if name in MCP_CONTRACT:
        outcome = bench.run_mcp(name, refusal.kwargs, vault=vault)
    else:
        outcome = bench.run_cli(name, refusal.argv, vault=vault, target=vault)
        assert outcome.exit_code == refusal.exit_code, (
            f"{name}: refusal expected exit {refusal.exit_code}, got "
            f"{outcome.exit_code}. Output:\n{outcome.readable()}"
        )

    assert outcome.says(refusal.reason), (
        f"{name}: the closed gate did not give its documented reason "
        f"{refusal.reason!r}. Output:\n{outcome.readable()}"
    )
    substantive = outcome.touched() - _BOOKKEEPING_GLOBS
    assert not substantive, (
        f"{name}: refused, and still touched {sorted(substantive)}. A refusal "
        "that mutates the vault is not a refusal."
    )


def _assert_frontmatter(surface: Surface, vault: Path, name: str) -> None:
    """Assert the exact ``{id: value}`` map the entry declares, read off disk.

    The map is deliberately multi-valued. A uniform map is what a *broken*
    wire also produces — the rules classifier fails closed, and #935's dead
    wire left every fragment ``unclassified`` while exiting 0.

    Args:
        surface: The contract entry under test.
        vault: Vault root.
        name: Surface name, for the failure message.
    """
    declared = surface.effect.frontmatter
    if not declared:
        return
    observed = _frontmatter_map(vault, surface.effect.frontmatter_key)
    assert observed == dict(declared), (
        f"{name}: the {surface.effect.frontmatter_key!r} map read back off disk "
        f"is {observed}, not the {dict(declared)} the entry declares. Every "
        f"value being identical is what a dead wire produces too (#935)."
    )


# ---------------------------------------------------------------------------
# Exemptions — and the ratchet that keeps them shrinking
# ---------------------------------------------------------------------------


def _resolve_seam(seam: str) -> bool:
    """Report whether an exemption's missing injection point now exists.

    Args:
        seam: ``"module:name"`` or ``"module:callable:parameter"``.

    Returns:
        True once the named attribute or keyword parameter exists.
    """
    parts = seam.split(":")
    module = importlib.import_module(parts[0])
    if len(parts) == 2:
        return hasattr(module, parts[1])
    target = getattr(module, parts[1], None)
    if target is None:
        return False
    return parts[2] in inspect.signature(target).parameters


@pytest.mark.parametrize(("name", "exemption"), sorted(EXEMPTIONS.items()))
def test_exemption_reason_cites_an_issue(name: str, exemption: Exemption) -> None:
    """Every exemption points at where the decision is tracked.

    Args:
        name: Surface name.
        exemption: The declared exemption.
    """
    assert _ISSUE_REFERENCE.search(exemption.reason), (
        f"EXEMPTIONS[{name!r}] must cite an issue number so the next engineer "
        f"can find the decision; got {exemption.reason!r}."
    )


@pytest.mark.parametrize(("name", "exemption"), sorted(EXEMPTIONS.items()))
def test_exemption_declares_exactly_one_staleness_probe(
    name: str,
    exemption: Exemption,
) -> None:
    """An exemption without an executable probe is a silent exemption.

    Args:
        name: Surface name.
        exemption: The declared exemption.
    """
    probes = [bool(exemption.blocker), bool(exemption.delegate), bool(exemption.seam)]
    assert sum(probes) == 1, (
        f"EXEMPTIONS[{name!r}] must declare exactly one of blocker / delegate / "
        f"seam. A reason with no probe can never be proven stale, which is the "
        f"same defect one level up."
    )


@pytest.mark.parametrize(
    ("name", "exemption"),
    sorted((n, e) for n, e in EXEMPTIONS.items() if e.blocker),
)
def test_exempt_surface_still_reports_its_blocker(
    bench: Bench,
    name: str,
    exemption: Exemption,
) -> None:
    """The blocker must still fire; if it stops, the exemption is stale.

    Args:
        bench: The bench.
        name: Surface name.
        exemption: The declared exemption.
    """
    vault = bench.seeded_vault()
    outcome = bench.run_cli(name, exemption.argv, vault=vault, target=vault)

    assert outcome.says(exemption.blocker), (
        f"EXEMPTIONS[{name!r}] claims this surface still cannot be proven "
        f"hermetically because of {exemption.blocker!r} — but that no longer "
        f"appears. The surface has become provable: delete the exemption and "
        f"add a real contract entry. Output:\n{outcome.readable()}"
    )


@pytest.mark.parametrize(
    ("name", "exemption"),
    sorted((n, e) for n, e in EXEMPTIONS.items() if e.delegate),
)
def test_delegated_exemption_names_a_suite_that_exercises_the_surface(
    name: str,
    exemption: Exemption,
) -> None:
    """A delegated effect must actually live where the exemption says.

    Args:
        name: Surface name.
        exemption: The declared exemption.
    """
    module_path, _, needle = exemption.delegate.partition(":")
    delegate = Path(__file__).parent.parent / module_path
    assert delegate.is_file(), (
        f"EXEMPTIONS[{name!r}] delegates to a missing {module_path}"
    )
    assert needle in delegate.read_text(encoding="utf-8"), (
        f"EXEMPTIONS[{name!r}] delegates to {module_path}, but that module never "
        f"mentions {needle!r} — the delegation is a fossil."
    )


@pytest.mark.parametrize(
    ("name", "exemption"),
    sorted((n, e) for n, e in EXEMPTIONS.items() if e.seam),
)
def test_seam_exemption_is_stale_once_the_injection_point_exists(
    name: str,
    exemption: Exemption,
) -> None:
    """The absent seam must still be absent; if it lands, the exemption goes.

    Args:
        name: Surface name.
        exemption: The declared exemption.
    """
    assert not _resolve_seam(exemption.seam), (
        f"EXEMPTIONS[{name!r}] is justified only by the absence of "
        f"{exemption.seam!r} — and that injection point now exists. Delete the "
        f"exemption and assert the wire through it."
    )


# ---------------------------------------------------------------------------
# Meta-contract: rules the table itself must obey
# ---------------------------------------------------------------------------

_ALL_SURFACES: Final[Mapping[str, Surface]] = {**CLI_CONTRACT, **MCP_CONTRACT}


@pytest.mark.parametrize(("name", "surface"), sorted(_ALL_SURFACES.items()))
def test_every_entry_says_which_wiring_bug_it_would_have_caught(
    name: str,
    surface: Surface,
) -> None:
    """A reviewer must be able to ask "which bug?" and get an answer.

    Args:
        name: Surface name.
        surface: The contract entry.
    """
    assert _ISSUE_REFERENCE.search(surface.why), (
        f"{name}: `why` must cite the issue whose shape this entry catches; "
        f"got {surface.why!r}."
    )


@pytest.mark.parametrize(("name", "surface"), sorted(_ALL_SURFACES.items()))
def test_derived_from_names_importable_production_symbols(
    name: str,
    surface: Surface,
) -> None:
    """Every provenance citation must resolve, so it cannot rot into a comment.

    Args:
        name: Surface name.
        surface: The contract entry.
    """
    assert surface.derived_from, f"{name}: declare what the expectation is derived from"
    for citation in surface.derived_from:
        module_name, _, symbol = citation.partition(":")
        module = importlib.import_module(module_name)
        assert hasattr(module, symbol), (
            f"{name}: derived_from cites {citation!r}, which does not exist. A "
            "provenance citation that does not resolve is a comment."
        )


@pytest.mark.parametrize(("name", "surface"), sorted(_ALL_SURFACES.items()))
def test_printed_effects_declare_a_contrast(name: str, surface: Surface) -> None:
    """A printed judgement without a negative control proves nothing.

    Args:
        name: Surface name.
        surface: The contract entry.
    """
    if not surface.effect.prints and not surface.effect.absent:
        return
    assert surface.contrast is not None, (
        f"{name}: declares printed output but no Contrast. 'Judged clean' and "
        "'found nothing to judge' render identically, so a bare substring "
        "assertion is passed by a hardcoded banner."
    )
    assert (
        _ISSUE_REFERENCE.search(surface.contrast.why) or len(surface.contrast.why) > 20
    ), f"{name}: the contrast must say what makes the observable absent there."


@pytest.mark.parametrize(("name", "surface"), sorted(_ALL_SURFACES.items()))
def test_no_effect_is_satisfied_by_bookkeeping_alone(
    name: str, surface: Surface
) -> None:
    """An observable that lives only in the audit log proves the decorator ran.

    Every MCP tool appends ``00-Creek-Meta/audit/mcp.jsonl`` — including the
    five ``purge.*`` tools while *refusing*. An entry whose declared artefacts
    are all bookkeeping would certify a dead surface.

    Args:
        name: Surface name.
        surface: The contract entry.
    """
    declared = {*surface.effect.writes, *surface.effect.removes}
    if not declared:
        assert (
            surface.effect.prints or surface.effect.absent or surface.effect.frontmatter
        ), f"{name}: declares no observable at all."
        return
    assert declared - _BOOKKEEPING_GLOBS, (
        f"{name}: every declared artefact ({sorted(declared)}) is bookkeeping "
        "that any tool writes merely by running. Name the feature's own output."
    )


def test_every_surface_is_declared_exactly_once() -> None:
    """A surface must not be both contracted and exempt."""
    overlap = (set(CLI_CONTRACT) | set(MCP_CONTRACT)) & set(EXEMPTIONS)

    assert not overlap, (
        f"{sorted(overlap)} are both contracted and exempt. An exemption is a "
        "confession that the effect is unprovable; a contract entry says it is "
        "proven. They cannot both be true."
    )


# ---------------------------------------------------------------------------
# Guarding the guard
# ---------------------------------------------------------------------------


def test_harness_fails_when_a_declared_artefact_is_never_written(bench: Bench) -> None:
    """Cut a wire and the harness must go red.

    A contract test whose predicates have quietly become vacuous is the exact
    defect being hunted here, so the harness is run against a probe surface
    that names an artefact production never creates. If this passes, every
    verdict above it is meaningless.

    Args:
        bench: The bench.
    """
    probe = Surface(
        shape=Shape.WRITES,
        why="#1027: proves the harness can still say no",
        derived_from=("creek.cli:app",),
        argv=("--vault", "{vault}"),
        effect=Effect(writes=("00-Creek-Meta/this-is-never-written.md",)),
    )
    vault = bench.seeded_vault()
    outcome = bench.run_cli("index", probe.argv, vault=vault, target=vault)

    with pytest.raises(AssertionError, match="declared artefact"):
        _assert_writes(probe, vault, outcome, "probe")


def test_harness_fails_when_the_payload_is_missing_from_a_written_artefact(
    bench: Bench,
) -> None:
    """#1040's exact shape: the file appears, the payload does not.

    ``creek index`` really does write ``Source-Index.md``, so a "a file
    appeared" predicate passes. Asking for a key that is not in it must fail —
    that is the difference between "the wire exists" and "the wire carries what
    the other end reads".

    Args:
        bench: The bench.
    """
    probe = Surface(
        shape=Shape.WRITES,
        why="#1040: a dormant guard writes the file and omits the key",
        derived_from=("creek.generate.grounding:DERIVATIVE_FRONTMATTER_KEY",),
        argv=("--vault", "{vault}"),
        effect=Effect(
            writes=("00-Creek-Meta/Source-Index.md",),
            contains=(DERIVATIVE_FRONTMATTER_KEY,),
        ),
    )
    vault = bench.seeded_vault()
    outcome = bench.run_cli("index", probe.argv, vault=vault, target=vault)

    _assert_writes(
        Surface(
            shape=probe.shape,
            why=probe.why,
            derived_from=probe.derived_from,
            argv=probe.argv,
            effect=Effect(writes=probe.effect.writes),
        ),
        vault,
        outcome,
        "probe",
    )
    with pytest.raises(AssertionError, match="carries no"):
        _assert_writes(probe, vault, outcome, "probe")


def test_harness_fails_when_a_uniform_frontmatter_map_replaces_a_real_one(
    bench: Bench,
) -> None:
    """#935's exact shape: every fragment carries the same default tier.

    Args:
        bench: The bench.
    """
    probe = Surface(
        shape=Shape.FRONTMATTER,
        why="#935: a dead PrivacyClassifier leaves every tier at the default",
        derived_from=("creek.classify.privacy:PrivacyClassifier",),
        effect=Effect(frontmatter=dict.fromkeys(_EXPECTED_TIER_MAP, "unclassified")),
    )
    vault = bench.seeded_vault()
    bench.run_cli(
        "classify",
        ("--vault", "{vault}", "--method", "rules"),
        vault=vault,
        target=vault,
    )

    with pytest.raises(AssertionError, match="dead wire"):
        _assert_frontmatter(probe, vault, "probe")


def test_harness_rejects_an_effect_made_only_of_bookkeeping() -> None:
    """The audit log alone must never satisfy an effect declaration."""
    probe = Surface(
        shape=Shape.WRITES,
        why="#1027: every MCP tool writes the audit log, including while refusing",
        derived_from=("creek.cli:app",),
        effect=Effect(writes=tuple(sorted(_BOOKKEEPING_GLOBS))),
    )

    with pytest.raises(AssertionError, match="bookkeeping"):
        test_no_effect_is_satisfied_by_bookkeeping_alone("probe", probe)


def test_declarations_survive_a_colour_terminal(
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared flag must still be found when Rich is allowed to style it.

    Every assertion above compares a hand-written substring against text Rich
    rendered, and Rich only styles when it believes it owns a terminal. That
    makes the whole contract environment-dependent unless :func:`_says`
    normalises — which is exactly how PR #1255 shipped ten assertions that
    were green on a developer's machine and red in CI.

    The hostile input is produced by the production renderer rather than typed
    out here: ``typer.rich_utils`` decides ``FORCE_TERMINAL`` from the
    environment at import time, so flipping it and invoking the real CLI makes
    Rich emit whatever Rich actually emits today. A pasted escape sequence
    would only pin this author's guess about that.

    ``discord`` is used because it has a required option; nothing about the
    fix is specific to it.

    Args:
        bench: The bench.
        monkeypatch: Used to let Rich colour in-process.
    """
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    reason = "Missing option '--mode'"
    vault = bench.bare_vault()

    outcome = bench.run_cli(
        "discord", ("--vault", "{vault}"), vault=vault, target=vault
    )

    assert "\x1b[" in outcome.output, (
        "Rich rendered no escape codes, so this guard is proving nothing. "
        "FORCE_TERMINAL is no longer the switch that makes typer colour."
    )
    assert reason not in outcome.output, (
        f"{reason!r} now survives a raw substring test, so this guard has gone "
        "vacuous — Rich stopped splitting the flag across style spans. Find a "
        "declaration it does still split before deleting this."
    )
    assert outcome.says(reason), (
        f"_says failed to recover {reason!r} from coloured output. That is the "
        f"CI-only failure this exists to prevent. Output:\n{outcome.readable()}"
    )
    assert outcome.says(f"Error {reason}"), (
        "the panel border and its line break were not removed, so a message "
        "that wraps mid-phrase (#1141) would still be unmatchable."
    )
    # Near misses, one per weakening that would make the recovery above pass
    # for the wrong reason: a different flag, a collapsed hyphen, a lowercased
    # comparison, stripped punctuation.
    for near_miss in (
        "Missing option '--modes'",
        "Missing option '-mode'",
        "missing option '--mode'",
        "Missing option --mode",
    ):
        assert not outcome.says(near_miss), (
            f"normalisation has become fuzzy: it accepted {near_miss!r}, which "
            "the surface never printed. An assertion that passes on the wrong "
            "message is worse than the failure it replaced."
        )


# ---------------------------------------------------------------------------
# Documentation coverage: a registration nobody documents is an orphan (#1470)
# ---------------------------------------------------------------------------
#
# The dead-code gate (``scripts/lint_vulture.py``) carves ``@app.command`` and
# ``@server.tool`` out of its scan, because registration — not a Python call
# site — is what invokes them. That carve-out is correct and it is also a hole:
# a *retired* command still wired into the app is indistinguishable from a live
# one, so vulture reports zero either way. Blindness #2 in that module's
# docstring names it.
#
# This section is the missing half. Registration proves a surface is reachable;
# a doc naming it proves somebody still means for it to be reachable. Nothing
# else in the repository asks that question: ``test_every_cli_command_is_declared``
# only asks whether the contract table knows about it, and the table is written
# by the same hand that writes the decorator.
#
# The inventory is the LIVE registry, never :data:`_ALL_SURFACES`. Parametrising
# over the contract table would make the check unfalsifiable by exactly the
# defect it exists to catch: an orphan is added as a decorator, and a decorator
# that nobody added a table entry for would simply never be asked about.


DOC_ROOTS: Final[tuple[str, ...]] = ("README.md", "docs", ".claude/commands")
"""Where a surface may be documented, relative to ``creek-tools/``.

This tuple is the whole strength of the check below, which is why it is a
named constant rather than a literal inside a test body: the cheapest way to
turn a doc-coverage failure green is not to write the doc, it is to widen the
search until something already mentions the name. Every root here is a place a
*reader* looks — the README, the ``docs/`` tree, and the slash-command
definitions under ``.claude/commands/``. Adding a source or test directory
would let a docstring, or the surface's own test, certify the surface;
:func:`test_doc_roots_hold_documentation_and_nothing_else` refuses that.

Deliberately confined to ``creek-tools/``. The repo root also carries prose
that happens to name some surfaces (ADRs under ``docs/decisions/``), but a
gate on the surfaces this package registers must not depend on a sibling
directory being checked out, and an ADR recording a past decision is not the
documentation a user of ``creek link`` needs to find.
"""

_NON_DOC_ROOTS: Final[tuple[str, ...]] = ("creek", "creek_mcp", "tests")

_PACKAGE_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


def _doc_files() -> tuple[Path, ...]:
    """Return every Markdown file reachable from :data:`DOC_ROOTS`.

    Returns:
        Absolute paths, deduplicated and sorted.
    """
    found: set[Path] = set()
    for root in DOC_ROOTS:
        target = _PACKAGE_ROOT / root
        if target.is_file():
            found.add(target)
        else:
            found.update(path for path in target.rglob("*.md") if path.is_file())
    return tuple(sorted(found))


_DOC_CORPUS: Final[tuple[tuple[Path, str], ...]] = tuple(
    (path, path.read_text(encoding="utf-8", errors="replace")) for path in _doc_files()
)


def _docs_naming(needle: str) -> list[str]:
    """Return the doc files whose text names `needle` as a whole token.

    The trailing guard is what keeps ``creek state`` from being certified by a
    page that only ever mentions ``creek state-budget``. It rejects a following
    word character or hyphen and nothing else, so ordinary prose punctuation
    (``creek link.``, ``creek link``` ) still counts.

    Args:
        needle: The invocation a doc would have to spell out, e.g.
            ``"creek clean orphans"`` or ``"creek.purge.vault"``.

    Returns:
        Package-relative paths of the files that name it.
    """
    pattern = re.compile(re.escape(needle) + r"(?![\w-])")
    return [
        str(path.relative_to(_PACKAGE_ROOT))
        for path, text in _DOC_CORPUS
        if pattern.search(text)
    ]


def test_doc_roots_hold_documentation_and_nothing_else() -> None:
    """The search surface must be real, and must not include source or tests.

    Three ways the doc-coverage check below could go quietly vacuous, all
    closed here: a root that no longer exists (the corpus shrinks and nobody
    notices), a root that is not prose (a ``.py`` file certifying a surface by
    its own docstring), and a root under ``creek/``, ``creek_mcp/`` or
    ``tests/`` (the implementation or its test certifying itself).
    """
    for root in DOC_ROOTS:
        target = _PACKAGE_ROOT / root
        assert target.exists(), (
            f"DOC_ROOTS names {root!r}, which does not exist under "
            f"{_PACKAGE_ROOT}. A root that resolves to nothing shrinks the "
            "search surface in silence."
        )
    assert _DOC_CORPUS, "the doc corpus is empty, so every check over it passes"
    for path, _ in _DOC_CORPUS:
        assert path.suffix == ".md", (
            f"{path} is not Markdown. DOC_ROOTS is a prose surface; a "
            "non-prose file lets a symbol certify itself."
        )
        for forbidden in _NON_DOC_ROOTS:
            assert not path.is_relative_to(_PACKAGE_ROOT / forbidden), (
                f"{path} lives under {forbidden}/, so the doc-coverage check "
                "would accept an implementation or a test as this surface's "
                "documentation. Widening DOC_ROOTS is not how a missing doc "
                "gets written."
            )


@pytest.mark.parametrize("command", sorted(registered_cli_commands()))
def test_every_registered_cli_command_is_named_by_a_doc(command: str) -> None:
    """A registered command no doc mentions is an orphan (#1470).

    Args:
        command: A live ``creek`` command path, e.g. ``"clean orphans"``.
    """
    invocation = f"creek {command}"
    assert _docs_naming(invocation), (
        f"{invocation!r} is registered on the Typer app and named by none of "
        f"the {len(_DOC_CORPUS)} files under DOC_ROOTS={DOC_ROOTS!r}. The "
        "dead-code gate cannot see this: scripts/lint_vulture.py carves out "
        "@app.command because registration is the caller, so a retired "
        "command still wired into the app reports as live forever. Either "
        "delete the command and its tests, or document it — and do not widen "
        "DOC_ROOTS to make this sentence go away."
    )


def test_every_registered_mcp_tool_is_named_by_a_doc(bench: Bench) -> None:
    """A registered MCP tool no doc mentions is an orphan (#1470).

    Reported in one pass rather than parametrised because the inventory comes
    from a live server, which needs a vault, which needs a fixture.

    Args:
        bench: The bench, for the throwaway vault the probe server binds to.
    """
    tools = sorted(registered_mcp_tools(bench.bare_vault()))
    assert tools, "the probe server registered no tools, so this proves nothing"

    orphans = [tool for tool in tools if not _docs_naming(tool)]

    assert orphans == [], (
        f"{orphans} are registered MCP tools that none of the "
        f"{len(_DOC_CORPUS)} files under DOC_ROOTS={DOC_ROOTS!r} names. "
        "scripts/lint_vulture.py carves out @server.tool because the MCP "
        "registry is the caller, so a retired tool looks alive to every other "
        "gate. Either delete the tool and its tests, or document it."
    )
