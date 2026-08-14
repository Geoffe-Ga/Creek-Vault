"""``creek state`` must honour a tier ceiling in what it writes and serves (#969).

Three leaks were reproduced at HEAD by running
``StateReportGenerator(vault_path=v).render()`` over a temp vault with no
ceiling anywhere:

1. **Drift warnings** — an ``intimate`` fragment older than 90 days and
   unlinked is reported by ``OrphanScanner`` as an *absolute* path whose
   filename is the slugified title of that fragment.
2. **Liminal Watch** — the *file stem* of a ``10-Liminal/Unnamed/`` note is
   rendered unconditionally.
3. **Active eddies** — an eddy whose only member fragment is above the ceiling
   still renders its title, because ``Eddy``/``Thread`` carry no
   ``privacy_tier`` of their own.

That history shapes every assertion in this module, in the same way PR #1068
shaped ``tests/test_mcp_report_tier_ceiling.py``:

* **Nothing here treats a response envelope as evidence of exclusion.**
  ``creek.state.render`` is a *write-side* surface. Its envelope happens to
  echo the rendered content today, but a future shape change that dropped
  ``content`` would silently turn every envelope assertion vacuous while the
  artifact on disk kept leaking. Every exclusion assertion below therefore
  reads ``<vault>/00-Creek-Meta/State/<iso-week>.md`` and ``latest.md`` back
  off the filesystem.
* **Every exclusion assertion is paired with a positive control.** A
  generator that wrote an empty report would satisfy "the intimate canary is
  absent" perfectly while being an outage rather than a gate, so an ``open``
  canary is asserted *present* in the same bytes, and every section header in
  :data:`~creek.generate.state.SECTION_ORDER` is asserted present too.
* **Canaries are unmistakable sentinels**, restricted to ``[A-Za-z0-9-]`` so
  they survive being slugified into a filename. A leak cannot then be excused
  as "that phrase could have come from anywhere".

The production surface these tests describe does not exist yet. Imports of
symbols that #969 will add (``creek.generate.state_tiers``, the ``override``
keyword on :class:`~creek.generate.state.StateReportGenerator`) are made
*inside* the tests that need them rather than at module scope, so a missing
module fails the specific tests that assert its contract instead of collapsing
the whole file into one collection-time ``ImportError`` that hides the
artifact-level failures — which are the interesting ones.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.state import (
    EMPTY_PLACEHOLDER,
    SECTION_ORDER,
    StateReportGenerator,
    _refresh_latest,
)
from creek.models import PrivacyTier
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, to_privacy_override
from creek_mcp.tools.state import state_render_tool
from creek_mcp.tools.state_read import state_read_tool

if TYPE_CHECKING:
    from types import ModuleType

runner = CliRunner()


# ---------------------------------------------------------------------------
# Canaries — one pair per gated section
#
# One shared sentinel would make every failure message ambiguous about *which*
# section leaked, which is the single most useful fact a failure can carry
# here. Restricted to ``[A-Za-z0-9-]`` because two of these ride in filenames.
# ---------------------------------------------------------------------------

_DRIFT_INTIMATE = "CANARY-DRIFT-INTIMATE-9f3a"
"""Rides in the *filename* of an aged, unlinked ``intimate`` fragment.

The drift leak is the slugified title in the orphan path, not a body, so the
sentinel has to be in the path for the reproduction to be real.
"""

_DRIFT_OPEN = "CANARY-DRIFT-OPEN-4c17"
"""Rides in the filename of an equally aged, equally unlinked ``open`` fragment."""

_LIMINAL_INTIMATE = "CANARY-LIMINAL-INTIMATE-6d80"
"""Rides in the file *stem* of a ``10-Liminal/Unnamed/`` note tiered ``intimate``."""

_LIMINAL_OPEN = "CANARY-LIMINAL-OPEN-2e55"
"""Rides in the file stem of a ``10-Liminal/Unnamed/`` note tiered ``open``."""

_PARADOX_NOTIER = "CANARY-PARADOX-NOTIER-7b91"
"""Rides in the stem of a ``type: paradox`` note carrying **no** ``privacy_tier``.

``10-Liminal/Paradoxes/`` notes are not fragments and have no tier field in
their model, so the fail-closed read (``raw_privacy_tier`` on a missing key →
``INTIMATE``) is the only thing standing between them and an ``open`` report.
"""

_EDDY_INTIMATE = "CANARY-EDDY-INTIMATE-5a3c"
"""Rides in the ``title`` of an eddy whose only member fragment is ``intimate``."""

_EDDY_OPEN = "CANARY-EDDY-OPEN-8f2d"
"""Rides in the ``title`` of an eddy whose only member fragment is ``open``."""

_THREAD_INTIMATE = "CANARY-THREAD-INTIMATE-3b6e"
"""Rides in the ``title`` of a thread whose only member fragment is ``intimate``."""

_THREAD_OPEN = "CANARY-THREAD-OPEN-0c94"
"""Rides in the ``title`` of a thread whose only member fragment is ``open``."""

_SYNC_INTIMATE = "CANARY-SYNC-INTIMATE-1e72"
"""Rides in the *id* of an ``intimate`` fragment named by a synchronicity row.

The surprising-connections section renders endpoint ids verbatim, so the id is
where the sentinel has to be for the row to be a real leak.
"""

_SYNC_OPEN = "CANARY-SYNC-OPEN-7a05"
"""Rides in the id of an ``open`` fragment named by a second synchronicity row."""

_HYPER_INTIMATE = "CANARY-HYPER-INTIMATE-2d41"
"""Rides in the ``title`` of a praxis derived from an ``intimate`` fragment."""

_HYPER_OPEN = "CANARY-HYPER-OPEN-6f38"
"""Rides in the ``title`` of a praxis derived from an ``open`` fragment."""

_LINT_CANARY = "CANARY-LINT-9b57"
"""Rides in the body of a ``creek lint`` report under ``00-Creek-Meta/Processing-Log``.

``latest_lint_report`` copies that file into the state report *verbatim*, and
``creek/lint/checks/tags.py`` deliberately runs the tag survey at
``PrivacyTierOverride.ALL`` so it can report honestly about the whole vault.
The copy is therefore untierable: the whole section is admitted or none of it.
"""

_ALL_INTIMATE_CANARIES = (
    _DRIFT_INTIMATE,
    _LIMINAL_INTIMATE,
    _PARADOX_NOTIER,
    _EDDY_INTIMATE,
    _THREAD_INTIMATE,
    _SYNC_INTIMATE,
    _HYPER_INTIMATE,
    _LINT_CANARY,
)
"""Every sentinel that must not survive a render below ``ceiling=intimate``."""

_ALL_OPEN_CANARIES = (
    _DRIFT_OPEN,
    _LIMINAL_OPEN,
    _EDDY_OPEN,
    _THREAD_OPEN,
    _SYNC_OPEN,
    _HYPER_OPEN,
)
"""Every sentinel that must survive a render at *any* ceiling."""


# Click 8 + Rich split ``--include-tier`` across ANSI reset codes when stdout
# looks like a terminal, which differs between CI and a local CliRunner. Same
# helper, same reason, as ``tests/test_cli_privacy.py``.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

_STATE_RELDIR = ("00-Creek-Meta", "State")
_LATEST_NAME = "latest.md"

_STAMP_TYPE_KEY = "type"
_STAMP_TYPE_VALUE = "state-report"
_STAMP_TIER_KEY = "privacy_tier"
_STAMP_CEILING_KEY = "tier_ceiling"
"""The stamp's front-matter shape, written out as literals on purpose.

Spelled here rather than imported from ``creek.generate.state_tiers`` so a
missing module fails :func:`test_state_tiers_exports_the_stamp_key_constants`
alone — where the contract is stated — instead of every artifact assertion in
the file. That one test pins the constants back to these literals, so the two
cannot drift.
"""

_STATE_MODULES = ("creek.generate.state", "creek.generate.state_tiers")
"""Modules a state-side tier gate may legitimately be bound in.

The gate helpers live in ``state_tiers`` per #969's design, but ``state.py``
owns the per-section admission and may bind them into its own namespace with
``from ... import``. A monkeypatch has to hit the *binding the caller uses*, so
the mutation tests below patch the symbol in every one of these that has it and
assert at least one patch landed.
"""

_CEILINGS = (
    TierCeiling.OPEN,
    TierCeiling.PERSONAL,
    TierCeiling.INTIMATE,
    TierCeiling.ALL,
)
"""Every ceiling, in ascending breadth — the axis most tests here parametrise on."""

_ADMITS_INTIMATE = (TierCeiling.INTIMATE, TierCeiling.ALL)
"""The ceilings under which an ``intimate``-derived sentinel is admitted."""


def _plain(text: str) -> str:
    """Return *text* with ANSI escape sequences stripped.

    Args:
        text: CLI output as captured by :class:`~typer.testing.CliRunner`.

    Returns:
        The same text with terminal styling removed, so substring assertions
        about flag names are not defeated by a reset code in the middle of
        ``--include-tier``.
    """
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Vault-building helpers
#
# Kept in this file rather than in conftest.py: each one is shaped around a
# specific section's admission conditions (an orphan must be old *and*
# unlinked; an eddy's title must equal its file stem so the wikilink resolves
# the same way under either implementation), and moving them to shared scope
# invites a later edit to "simplify" one of those conditions away.
# ---------------------------------------------------------------------------


def _write_note(
    vault: Path,
    relpath: str,
    metadata: dict[str, Any],
    body: str,
) -> Path:
    """Write one markdown note with YAML front matter into the vault.

    Args:
        vault: Vault root.
        relpath: Vault-relative destination path.
        metadata: Front-matter mapping, written verbatim — a key omitted here
            is a key genuinely absent from the file, which is the distinction
            the fail-closed tier read depends on.
        body: Markdown body.

    Returns:
        The path written.
    """
    target = vault / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return target


def _seed_dirs(vault: Path) -> None:
    """Create the canonical vault folder layout the state generator reads.

    ``StateReportGenerator.write`` creates ``00-Creek-Meta/State`` itself, but
    the MCP audit log and the lint read-back both assume their parents exist,
    and a missing folder would make a section render its placeholder for the
    wrong reason.

    Args:
        vault: Vault root.
    """
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis/Daily",
        "10-Liminal/Unnamed",
        "10-Liminal/Paradoxes",
        "10-Liminal/Synchronicities",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    privacy_tier: str | None,
    title: str = "Note",
    filename: str | None = None,
    age_days: int = 3,
    eddies: list[str] | None = None,
    threads: list[str] | None = None,
    body: str = "fragment body",
) -> Path:
    """Write one fragment under ``01-Fragments/Notes``.

    Args:
        vault: Vault root.
        frag_id: Fragment id — also the default file stem, which matters for
            the synchronicity canary (rendered verbatim) and the drift canary
            (rendered as a path).
        privacy_tier: The declared tier, or ``None`` to omit the key entirely.
            ``None`` is not ``"unclassified"``: the model defaults a missing
            key to ``unclassified``, and only the raw front matter still tells
            the two apart.
        title: Fragment title.
        filename: Explicit file stem when it must differ from *frag_id*.
        age_days: How old ``created`` is, relative to now. Ages are relative
            because ``OrphanScanner`` compares against ``datetime.now`` at scan
            time, so a hardcoded date would silently cross the 90-day staleness
            threshold as the calendar moved and flip this fixture's meaning.
        eddies: ``eddies`` wikilinks. A resolvable wikilink also makes the
            fragment non-orphaned, which is how the drift fixture is kept
            separable from the eddy fixture.
        threads: ``threads`` wikilinks.
        body: Markdown body.

    Returns:
        The path written.
    """
    when = datetime.now(tz=UTC) - timedelta(days=age_days)
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "wavelength": {
            "phase": "unclassified",
            "mode": "unclassified",
            "dosage": "unclassified",
        },
        "eddies": list(eddies or []),
        "threads": list(threads or []),
        "praxis_potential": "none",
    }
    if privacy_tier is not None:
        metadata["privacy_tier"] = privacy_tier
    stem = filename if filename is not None else frag_id
    return _write_note(vault, f"01-Fragments/Notes/{stem}.md", metadata, body)


def _write_eddy(vault: Path, *, title: str) -> Path:
    """Write an eddy whose file stem is identical to its ``title``.

    The equality is load-bearing. ``StateReportGenerator._leaf_counts_by_link``
    tallies wikilink *targets* and then looks the tally up by ``eddy.title``,
    while a tier derivation could reasonably resolve the same wikilink to the
    eddy *file*. Making stem and title the same string means the fixture is
    valid under either resolution, so a failure here is about the ceiling and
    never about which of the two the implementer chose.

    Args:
        vault: Vault root.
        title: Eddy title, also used as the file stem and the wikilink target.

    Returns:
        The path written.
    """
    metadata: dict[str, Any] = {
        "type": "eddy",
        "id": f"eddy-{title.lower()}",
        "title": title,
        "formed": "2026-01-01",
        "fragment_count": 1,
        "threads": [],
    }
    return _write_note(vault, f"03-Eddies/{title}.md", metadata, "")


def _write_thread(vault: Path, *, title: str) -> Path:
    """Write a thread whose file stem is identical to its ``title``.

    Same reasoning as :func:`_write_eddy`.

    Args:
        vault: Vault root.
        title: Thread title, also the file stem and the wikilink target.

    Returns:
        The path written.
    """
    metadata: dict[str, Any] = {
        "type": "thread",
        "id": f"thread-{title.lower()}",
        "title": title,
        "status": "active",
        "first_seen": "2026-01-01",
        "last_seen": "2026-06-01",
        "fragment_count": 1,
    }
    return _write_note(vault, f"02-Threads/Active/{title}.md", metadata, "")


def _write_praxis(
    vault: Path,
    *,
    praxis_id: str,
    title: str,
    derived_from: list[str],
) -> Path:
    """Write a praxis under ``04-Praxis/Daily``.

    Args:
        vault: Vault root.
        praxis_id: Praxis id and file stem.
        title: Praxis title — the hyperedge canary's carrier, since the
            hyperedge row renders ``<title> — spans: <eddies>``.
        derived_from: Source fragment ids, whose tiers the praxis's own
            derived tier reduces over.

    Returns:
        The path written.
    """
    metadata: dict[str, Any] = {
        "type": "praxis",
        "id": praxis_id,
        "title": title,
        "praxis_type": "habit",
        "status": "proposed",
        "derived_from": list(derived_from),
    }
    return _write_note(vault, f"04-Praxis/Daily/{praxis_id}.md", metadata, "")


def _write_synchronicity(
    vault: Path, *, sync_id: str, frag_a: str, frag_b: str
) -> Path:
    """Write a synchronicity row naming two fragment ids.

    Args:
        vault: Vault root.
        sync_id: Synchronicity id and file stem.
        frag_a: First endpoint fragment id.
        frag_b: Second endpoint fragment id.

    Returns:
        The path written.
    """
    metadata: dict[str, Any] = {
        "type": "synchronicity",
        "id": sync_id,
        "fragment_a_id": frag_a,
        "fragment_b_id": frag_b,
        "similarity": 0.95,
        "time_gap_days": 60,
        "source_a": "journal",
        "source_b": "essay",
    }
    return _write_note(
        vault,
        f"10-Liminal/Synchronicities/{sync_id}.md",
        metadata,
        "",
    )


def _write_liminal_unnamed(vault: Path, *, stem: str, privacy_tier: str) -> Path:
    """Write a ``10-Liminal/Unnamed`` note whose *stem* carries the sentinel.

    The liminal watch renders ``path.stem`` and nothing else, so the stem is
    the only place a sentinel can prove the section leaked.

    Args:
        vault: Vault root.
        stem: File stem.
        privacy_tier: The declared tier.

    Returns:
        The path written.
    """
    when = datetime.now(tz=UTC) - timedelta(days=3)
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": stem,
        "title": stem,
        "created": when.isoformat(),
        "ingested": when.isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "unclassified", "secondary": []},
        "privacy_tier": privacy_tier,
    }
    return _write_note(vault, f"10-Liminal/Unnamed/{stem}.md", metadata, "unnamed body")


def _write_liminal_paradox(vault: Path, *, stem: str) -> Path:
    """Write a ``10-Liminal/Paradoxes`` note with **no** ``privacy_tier`` key.

    ``type: paradox`` notes have no tier field in their model at all, so this
    is the fail-closed case: the note must be excluded below
    ``ceiling=intimate`` because nobody has vouched for it, not because it was
    classified.

    Args:
        vault: Vault root.
        stem: File stem, carrying the sentinel.

    Returns:
        The path written.
    """
    when = datetime.now(tz=UTC) - timedelta(days=3)
    metadata: dict[str, Any] = {
        "type": "paradox",
        "id": stem,
        "title": stem,
        "created": when.isoformat(),
    }
    return _write_note(
        vault,
        f"10-Liminal/Paradoxes/{stem}.md",
        metadata,
        "held tension",
    )


def _write_lint_report(vault: Path) -> Path:
    """Write a ``creek lint`` report where ``latest_lint_report`` will find it.

    ``LintRunner.write`` writes ``00-Creek-Meta/Processing-Log/lint-<date>.md``
    and ``latest_lint_report`` globs ``lint-*.md``, sorts, and returns the last
    file's text **verbatim**. The body shape is therefore irrelevant to this
    fixture — only the path pattern is — which is exactly why the section
    cannot be filtered row by row and must be admitted whole or not at all.

    Args:
        vault: Vault root.

    Returns:
        The path written.
    """
    target = vault / "00-Creek-Meta" / "Processing-Log" / "lint-2026-07-01.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Lint report\n\n## orphan-tags\n\n"
        f"- Orphan tag `{_LINT_CANARY}` appears on 1 note.\n",
        encoding="utf-8",
    )
    return target


def _write_yield_log(vault: Path) -> Path:
    """Write one ``run-summary.jsonl`` line for the pre-LLM yield section.

    The last line of that log describes a pipeline *run*: it carries no
    fragment ids, so there is nothing in it to filter *by*. It exists in this
    fixture so the out-of-scope section has real content, which is what makes
    :func:`test_whole_corpus_sections_are_not_filtered_by_the_ceiling` a
    comparison rather than two placeholders agreeing for free.

    Args:
        vault: Vault root.

    Returns:
        The path written.
    """
    target = vault / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "run_id": "run-canary",
                "timestamp": "2026-07-01T00:00:00+00:00",
                "deterministic_classified": 7,
                "local_model_processed": 2,
                "residue": 1,
                "no_llm": True,
            },
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _build_canary_vault(root: Path, name: str = "canary-state-vault") -> Path:
    """Build one vault carrying an above/below canary pair for every gated section.

    A single vault rather than seven is deliberate: the sections share one
    corpus in production, and a fixture that isolated each one could not catch
    a section admitting content another section already excluded (the eddy
    whose title was derived from an excluded fragment, say). Each pair carries
    its own sentinel so a failure still names one section.

    Ages are relative to *now* because ``OrphanScanner`` compares against
    ``datetime.now``: only the two drift fragments are old enough (200 days) to
    be reported stale, and every other fragment is three days old so it cannot
    drift into the drift section as the calendar moves.

    Args:
        root: Directory the vault is created inside (typically ``tmp_path``).
        name: Vault directory name, so one test can build several vaults.

    Returns:
        The vault root.
    """
    vault = root / name
    _seed_dirs(vault)

    # --- Drift warnings: aged + unlinked, sentinel in the *filename* --------
    _write_fragment(
        vault,
        frag_id="frag-drift-intimate",
        filename=f"private-{_DRIFT_INTIMATE}-moment",
        title="Private moment",
        privacy_tier="intimate",
        age_days=200,
    )
    _write_fragment(
        vault,
        frag_id="frag-drift-open",
        filename=f"public-{_DRIFT_OPEN}-note",
        title="Public note",
        privacy_tier="open",
        age_days=200,
    )

    # --- Liminal watch -----------------------------------------------------
    _write_liminal_unnamed(
        vault,
        stem=f"unnamed-{_LIMINAL_INTIMATE}",
        privacy_tier="intimate",
    )
    _write_liminal_unnamed(
        vault,
        stem=f"unnamed-{_LIMINAL_OPEN}",
        privacy_tier="open",
    )
    _write_liminal_paradox(vault, stem=f"paradox-{_PARADOX_NOTIER}")

    # --- Active eddies -----------------------------------------------------
    intimate_eddy = f"Eddy-{_EDDY_INTIMATE}-One"
    open_eddy = f"Eddy-{_EDDY_OPEN}-Two"
    _write_eddy(vault, title=intimate_eddy)
    _write_eddy(vault, title=open_eddy)
    _write_fragment(
        vault,
        frag_id="frag-eddy-intimate",
        privacy_tier="intimate",
        eddies=[f"[[{intimate_eddy}]]"],
    )
    _write_fragment(
        vault,
        frag_id="frag-eddy-open",
        privacy_tier="open",
        eddies=[f"[[{open_eddy}]]"],
    )

    # --- Active threads ----------------------------------------------------
    intimate_thread = f"Thread-{_THREAD_INTIMATE}-One"
    open_thread = f"Thread-{_THREAD_OPEN}-Two"
    _write_thread(vault, title=intimate_thread)
    _write_thread(vault, title=open_thread)
    _write_fragment(
        vault,
        frag_id="frag-thread-intimate",
        privacy_tier="intimate",
        threads=[f"[[{intimate_thread}]]"],
    )
    _write_fragment(
        vault,
        frag_id="frag-thread-open",
        privacy_tier="open",
        threads=[f"[[{open_thread}]]"],
    )

    # --- Surprising connections: the sentinel is the fragment *id* ----------
    _write_fragment(
        vault,
        frag_id=f"frag-{_SYNC_INTIMATE}",
        privacy_tier="intimate",
    )
    _write_fragment(vault, frag_id="frag-sync-partner", privacy_tier="open")
    _write_fragment(vault, frag_id=f"frag-{_SYNC_OPEN}", privacy_tier="open")
    _write_synchronicity(
        vault,
        sync_id="sync-above",
        frag_a="frag-sync-partner",
        frag_b=f"frag-{_SYNC_INTIMATE}",
    )
    _write_synchronicity(
        vault,
        sync_id="sync-below",
        frag_a="frag-sync-partner",
        frag_b=f"frag-{_SYNC_OPEN}",
    )

    # --- Hyperedges: a praxis whose sources span two eddies -----------------
    # The four spanned eddies are deliberately sentinel-free: this row is about
    # the praxis title, and a canary in a spanned eddy title would make a
    # failure ambiguous between the hyperedge gate and the eddy gate.
    #
    # The two praxis rows get *disjoint* eddy pairs on purpose. Sharing a pair
    # would give each shared eddy one intimate and one open member, so the
    # below-ceiling praxis's own span would depend on whether the implementer
    # computes the spanned set from admitted *fragments* or from admitted
    # *eddies* — and the positive control would then be testing that choice
    # rather than the gate.
    for name in ("Eddy-Hyper-A-Above", "Eddy-Hyper-B-Above"):
        _write_eddy(vault, title=name)
    for name in ("Eddy-Hyper-A-Below", "Eddy-Hyper-B-Below"):
        _write_eddy(vault, title=name)
    _write_fragment(
        vault,
        frag_id="frag-hyper-intimate",
        privacy_tier="intimate",
        eddies=["[[Eddy-Hyper-A-Above]]", "[[Eddy-Hyper-B-Above]]"],
    )
    _write_fragment(
        vault,
        frag_id="frag-hyper-open",
        privacy_tier="open",
        eddies=["[[Eddy-Hyper-A-Below]]", "[[Eddy-Hyper-B-Below]]"],
    )
    _write_praxis(
        vault,
        praxis_id="praxis-above",
        title=f"Praxis {_HYPER_INTIMATE} above",
        derived_from=["frag-hyper-intimate"],
    )
    _write_praxis(
        vault,
        praxis_id="praxis-below",
        title=f"Praxis {_HYPER_OPEN} below",
        derived_from=["frag-hyper-open"],
    )

    # --- Pre-LLM yield -----------------------------------------------------
    # Present so the whole-corpus sections have real content to compare across
    # ceilings; an all-placeholder comparison would be equal for free.
    _write_yield_log(vault)

    # --- Lint summary ------------------------------------------------------
    _write_lint_report(vault)
    return vault


def _build_open_only_vault(root: Path, name: str = "open-only-vault") -> Path:
    """Build a vault whose every tiered note is ``open``.

    Used by the stamp tests: a broad render over a corpus with nothing above
    ``open`` in it must still stamp ``open``, which is the property that keeps
    an ``all``-ceiling render readable at an ``open`` ceiling.

    Args:
        root: Directory the vault is created inside.
        name: Vault directory name.

    Returns:
        The vault root.
    """
    vault = root / name
    _seed_dirs(vault)
    _write_fragment(vault, frag_id="frag-open-a", privacy_tier="open")
    _write_fragment(vault, frag_id="frag-open-b", privacy_tier="open")
    _write_liminal_unnamed(vault, stem="unnamed-open", privacy_tier="open")
    return vault


# ---------------------------------------------------------------------------
# Artifact-reading helpers
#
# Everything below reads the *files* the call wrote. Nothing reads a response
# envelope, for the reason set out in the module docstring.
# ---------------------------------------------------------------------------


def _iso_week_file(vault: Path) -> Path:
    """Return the single ISO-week report file under ``00-Creek-Meta/State``.

    Located by globbing rather than by recomputing the ISO week, because
    ``state_render_tool`` takes no ``today`` and a test that recomputed the
    week would break at a week boundary for reasons having nothing to do with
    tiers.

    Args:
        vault: Vault root.

    Returns:
        The ISO-week report path.
    """
    state_dir = vault.joinpath(*_STATE_RELDIR)
    weeks = sorted(p for p in state_dir.glob("*.md") if p.name != _LATEST_NAME)
    assert len(weeks) == 1, (
        f"expected exactly one ISO-week report under {state_dir}, found "
        f"{[p.name for p in weeks]}"
    )
    return weeks[0]


def _latest_file(vault: Path) -> Path:
    """Return ``00-Creek-Meta/State/latest.md``.

    Args:
        vault: Vault root.

    Returns:
        The ``latest.md`` path (a symlink on most platforms, a copy otherwise).
    """
    return vault.joinpath(*_STATE_RELDIR) / _LATEST_NAME


def _artifact_text(vault: Path) -> str:
    """Return every state artifact's vault-relative path and text, concatenated.

    Both the ISO-week file *and* ``latest.md`` are included because they are
    two independent write paths — a symlink refresh and a copy fallback — and
    #969's cache-thrash behaviour means a caller may read either one. Paths are
    included alongside contents because the drift sentinel rides in a filename,
    and a contents-only sweep would walk straight past a filename that got
    rendered into a row.

    Args:
        vault: Vault root.

    Returns:
        One string covering both artifacts' paths and bodies.
    """
    parts: list[str] = []
    for path in (_iso_week_file(vault), _latest_file(vault)):
        parts.append(str(path.relative_to(vault)))
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _report_body(vault: Path) -> str:
    """Return the ISO-week report's markdown body with the stamp removed.

    Args:
        vault: Vault root.

    Returns:
        The body ``frontmatter.loads`` recovers — identical to the pre-#969
        rendered document if the stamp round-trips, which is what
        :func:`test_stamped_report_body_still_round_trips` asserts.
    """
    raw = _iso_week_file(vault).read_text(encoding="utf-8")
    return str(frontmatter.loads(raw).content)


def _stamp(vault: Path, path: Path | None = None) -> dict[str, Any]:
    """Return the YAML front-matter stamp of a written state artifact.

    Args:
        vault: Vault root.
        path: The artifact to read, defaulting to the ISO-week file.

    Returns:
        The parsed front-matter mapping. An unstamped artifact yields ``{}``,
        which is itself the legacy case the read gate has to fail closed on.
    """
    target = path if path is not None else _iso_week_file(vault)
    return dict(frontmatter.loads(target.read_text(encoding="utf-8")).metadata)


def _section_body(report: str, header: str) -> str:
    """Return the text of one ``## ...`` section of a rendered report.

    Args:
        report: The full rendered markdown body.
        header: The section header, e.g. ``"## Lint summary"``.

    Returns:
        Everything from *header* up to the next ``## `` heading, or the empty
        string when the header is absent — which callers assert against
        explicitly rather than silently treating as "no content".
    """
    start = report.find(header)
    if start == -1:
        return ""
    rest = report[start + len(header) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _snapshot(vault: Path) -> dict[Path, bytes]:
    """Return the full byte contents of every file in the vault.

    Bytes rather than ``(mtime_ns, size)``: a render that rewrote a file with
    same-length content inside one filesystem timestamp tick would look
    unchanged to a stat-based snapshot, and "unchanged" is exactly the state
    this snapshot exists to rule out.

    Args:
        vault: Vault root.

    Returns:
        Mapping of path to contents.
    """
    return {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}


def _changed_files(vault: Path, before: dict[Path, bytes]) -> list[Path]:
    """Return every file that is new or whose bytes differ from *before*.

    Args:
        vault: Vault root.
        before: A :func:`_snapshot` taken before the call under test.

    Returns:
        Sorted list of new-or-modified files.
    """
    return sorted(
        p for p in vault.rglob("*") if p.is_file() and before.get(p) != p.read_bytes()
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canary_vault(tmp_path: Path) -> Path:
    """Return the shared per-section canary vault.

    Function-scoped on purpose: several tests below render into the vault, and
    ``latest.md`` is a single slot shared across ceilings, so a module-scoped
    fixture would let one test's render decide another test's read.
    """
    return _build_canary_vault(tmp_path)


# ---------------------------------------------------------------------------
# The per-section table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SectionCase:
    """One gated section of the state report and how to judge what it wrote.

    Attributes:
        header: The section header, used verbatim in failure messages so a
            failure names the section without the reader consulting this table.
        above_canary: The sentinel carried by content above ``ceiling=open``.
        below_canary: The sentinel carried by admitted content, asserted
            *present* at every ceiling, or ``None`` for the lint summary, whose
            content is admitted whole or not at all and so has no below-ceiling
            half to assert.
        carrier: One line naming what the sentinel actually rides in — the
            fact a reader needs to judge whether the fixture reproduces the
            leak or merely resembles it.
    """

    header: str
    above_canary: str
    below_canary: str | None
    carrier: str


_SECTIONS: tuple[_SectionCase, ...] = (
    _SectionCase(
        header="## Liminal Watch",
        above_canary=_LIMINAL_INTIMATE,
        below_canary=_LIMINAL_OPEN,
        carrier="the file stem of a 10-Liminal/Unnamed note, rendered verbatim",
    ),
    _SectionCase(
        header="## Liminal Watch",
        above_canary=_PARADOX_NOTIER,
        below_canary=_LIMINAL_OPEN,
        carrier=(
            "the file stem of a 10-Liminal/Paradoxes note with no privacy_tier "
            "key at all, which must fail closed to intimate"
        ),
    ),
    _SectionCase(
        header="## Active eddies",
        above_canary=_EDDY_INTIMATE,
        below_canary=_EDDY_OPEN,
        carrier="the title of an eddy whose only member fragment is intimate",
    ),
    _SectionCase(
        header="## Active threads",
        above_canary=_THREAD_INTIMATE,
        below_canary=_THREAD_OPEN,
        carrier="the title of a thread whose only member fragment is intimate",
    ),
    _SectionCase(
        header="## Surprising connections",
        above_canary=_SYNC_INTIMATE,
        below_canary=_SYNC_OPEN,
        carrier="the id of an intimate endpoint fragment, rendered verbatim",
    ),
    _SectionCase(
        header="## Hyperedges",
        above_canary=_HYPER_INTIMATE,
        below_canary=_HYPER_OPEN,
        carrier="the title of a praxis derived from an intimate fragment",
    ),
    _SectionCase(
        header="## Drift warnings",
        above_canary=_DRIFT_INTIMATE,
        below_canary=_DRIFT_OPEN,
        carrier=(
            "the filename of an aged, unlinked intimate fragment — the "
            "slugified title, reported as an orphan path"
        ),
    ),
    _SectionCase(
        header="## Lint summary",
        above_canary=_LINT_CANARY,
        below_canary=None,
        carrier=(
            "a tag name inside the verbatim lint report copy, which the tags "
            "check deliberately produced at PrivacyTierOverride.ALL"
        ),
    ),
)
"""Every section whose *identifiable* content #969 gates, in section order.

Three sections are absent, and each for its own reason rather than as one
bucket. ``## Wavelength snapshot`` and ``## Vault summary`` **are** gated —
they narrow with the ceiling — but what they render is a count, not a
sentinel, so they are asserted by
:func:`test_wavelength_and_vault_summary_narrow_with_the_ceiling` instead of by
the canary table. ``## Pre-LLM yield`` is genuinely ungated: it reports one
pipeline run's totals and names no fragment, so there is nothing in it to
filter by, which
:func:`test_pre_llm_yield_is_not_filtered_by_the_ceiling` asserts.
"""

_SECTION_IDS = [
    f"{case.header.removeprefix('## ').replace(' ', '-')}-{case.above_canary}"
    for case in _SECTIONS
]


_SECTION_DISPOSITIONS: dict[str, str] = {
    "## Wavelength snapshot": "gated-count",
    "## Vault summary": "gated-count",
    "## Pre-LLM yield": "ungated-run-log",
    "## Liminal Watch": "gated-canary",
    "## Active eddies": "gated-canary",
    "## Active threads": "gated-canary",
    "## Surprising connections": "gated-canary",
    "## Hyperedges": "gated-canary",
    "## Drift warnings": "gated-canary",
    "## Suggested questions": "gated-dropped-below-personal",
    "## Lint summary": "gated-canary",
}
"""Every section of the report and how #969 disposed of it.

The forcing function that keeps the artifact stamp honest as the report grows.
The stamp is the maximum tier the render admitted, and the read gate refuses on
it — so a **new** section that emits above-ceiling content without contributing
to that maximum makes the stamp under-report, and a stamp that under-reports is
a read gate that fails open. That failure would be silent: every assertion in
this file would still pass, because none of them knows about a section nobody
wrote a canary for.

Requiring the table to match :data:`~creek.generate.state.SECTION_ORDER`
exactly means a new section has to be triaged **out loud** before the suite is
green — the same discipline ``tests/test_mcp_read_gate.py`` layers (a) and (f)
apply to a newly registered tool.
"""


def test_every_report_section_has_a_recorded_disposition() -> None:
    """The disposition table covers ``SECTION_ORDER`` exactly, in both directions.

    Missing entries are the dangerous direction — an untriaged section can leak
    above the ceiling and leave the stamp under-reporting it. Stale entries are
    checked too, because a disposition for a section that no longer exists is a
    claim about nothing and inflates the apparent coverage of this file.
    """
    recorded = set(_SECTION_DISPOSITIONS)
    actual = set(SECTION_ORDER)
    untriaged = actual - recorded
    assert not untriaged, (
        f"report section(s) with no recorded #969 disposition: {sorted(untriaged)}. "
        "Add an entry saying whether the section is gated and how. A section "
        "nobody triaged can emit above-ceiling content without contributing to "
        "the artifact stamp, which makes creek.state.read fail open on it."
    )
    stale = recorded - actual
    assert not stale, (
        f"_SECTION_DISPOSITIONS names section(s) that no longer exist: "
        f"{sorted(stale)}. Delete the entry, or restore the section."
    )


def test_canary_table_and_disposition_table_agree() -> None:
    """Every ``gated-canary`` section is actually canary-tested, and vice versa.

    Two tables describing the same sections can drift apart, and the drift is
    invisible: a section could be labelled ``gated-canary`` here while no row
    of :data:`_SECTIONS` exercises it, leaving a confident-looking disposition
    over an untested section.
    """
    labelled = {
        header
        for header, disposition in _SECTION_DISPOSITIONS.items()
        if disposition == "gated-canary"
    }
    exercised = {case.header for case in _SECTIONS}
    assert labelled == exercised, (
        "the disposition table and the canary table disagree about which "
        f"sections are canary-tested: labelled-only={sorted(labelled - exercised)}, "
        f"exercised-only={sorted(exercised - labelled)}."
    )


# ---------------------------------------------------------------------------
# T1 — per-section exclusion and admission, on the artifact bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _SECTIONS, ids=_SECTION_IDS)
@pytest.mark.parametrize("ceiling", _CEILINGS, ids=[c.value for c in _CEILINGS])
def test_render_admits_a_section_canary_only_at_a_ceiling_that_allows_it(
    case: _SectionCase,
    ceiling: TierCeiling,
    canary_vault: Path,
) -> None:
    """Each gated section admits above-ceiling content at ``intimate``/``all`` only.

    Asserted against the bytes of the ISO-week file **and** ``latest.md``,
    never against ``state_render_tool``'s envelope. The envelope echoes the
    rendered content today, but ``creek.state.render`` is a write-side surface:
    the artifact is what a later ``creek.state.read``, ``creek state-budget``,
    a git commit or an operator's editor will serve, and it is the artifact
    that #969 reproduced three leaks in.

    The below-ceiling canary is asserted *present* in the same bytes at every
    ceiling, and that is not decoration: without it a generator that filtered
    everything out — or crashed into writing an empty report — would satisfy
    the exclusion half perfectly while being an outage rather than a gate. The
    lint summary has no below-ceiling half (its copy is admitted whole or not
    at all), so its non-vacuity control is the section-header sweep, which
    proves the document was rendered in full.

    Args:
        case: The section under test and its canary pair.
        ceiling: The ceiling the render runs under.
        canary_vault: The shared per-section canary vault.
    """
    result = state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=ceiling,
    )
    assert result["status"] == "ok"
    text = _artifact_text(canary_vault)

    for header in SECTION_ORDER:
        assert header in text, (
            f"the rendered report is missing {header!r} at ceiling="
            f"{ceiling.value}, so every canary assertion below it is vacuous"
        )

    if ceiling in _ADMITS_INTIMATE:
        assert case.above_canary in text, (
            f"{case.header} dropped {case.above_canary!r} at "
            f"privacy_tier_ceiling={ceiling.value}, which admits intimate "
            "content. The permissive direction has to be pinned as hard as "
            "the restrictive one: a gate that drops everything is leak-free "
            "and useless.\n\n"
            f"Sentinel carrier: {case.carrier}."
        )
    else:
        assert case.above_canary not in text, (
            f"{case.header} wrote the above-ceiling sentinel "
            f"{case.above_canary!r} into 00-Creek-Meta/State at "
            f"privacy_tier_ceiling={ceiling.value}.\n\n"
            f"Sentinel carrier: {case.carrier}.\n\n"
            "This is an artifact-level assertion on purpose. "
            "creek.state.render's response envelope is not evidence for a "
            "write-side surface, and the artifact is what every later reader "
            f"of the vault sees.\n\n{text}"
        )

    if case.below_canary is not None:
        assert case.below_canary in text, (
            f"{case.header} dropped the below-ceiling sentinel "
            f"{case.below_canary!r} at privacy_tier_ceiling={ceiling.value}. "
            "Admitted content must still reach the artifact, or the exclusion "
            "assertion above proves nothing."
        )


def test_lint_summary_renders_the_placeholder_below_the_intimate_ceiling(
    canary_vault: Path,
) -> None:
    """Below ``ceiling=intimate`` the lint section is empty, not merely canary-free.

    ``latest_lint_report`` returns an untierable verbatim copy of a
    Processing-Log artifact that embeds above-ceiling titles and tag names, and
    ``creek/lint/checks/tags.py`` deliberately surveys the whole vault at
    ``PrivacyTierOverride.ALL`` so it cannot report "no orphan tags" about a
    vault that has them. There is no row-level filter available, so the honest
    behaviour is to drop the section and say so with the standard placeholder
    rather than to render a silently truncated report.

    Asserting the placeholder rather than only the sentinel's absence matters
    because a lint report with a *different* body would then be admitted while
    this test stayed green.

    This closes the live caveat ``creek_mcp/read_gate.py`` records under
    ``creek.lint``: "it holds only while nothing serves Processing-Log/ back".
    ``creek state`` is the thing that serves it back.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    section = _section_body(_report_body(canary_vault), "## Lint summary")
    assert section, "the report is missing its '## Lint summary' section entirely"
    assert EMPTY_PLACEHOLDER in section, (
        "## Lint summary at privacy_tier_ceiling=open must render "
        f"{EMPTY_PLACEHOLDER!r}; it rendered:\n\n{section}"
    )
    assert _LINT_CANARY not in section


def test_suggested_questions_are_dropped_at_the_open_ceiling(
    canary_vault: Path,
) -> None:
    """``## Suggested questions`` is empty at ``ceiling=open``, by design.

    The seed corpus is mined through ``filter_fragments_by_tier``, which
    *summarises* a personal fragment as ``[Personal-tier summary: <title>]``
    rather than excluding it. At ``ceiling=open`` a personal **title** can
    therefore enter the mining corpus and ride out as a prompt — exactly the
    property ``within_ceiling``'s docstring says a report must not rely on.

    Dropping the section below ``personal`` is the belt to that braces, not the
    whole gate. It does **not**, on its own, make the seed corpus equal to the
    hard-cutoff admitted set — an earlier draft of this docstring claimed it
    did, and the #969 Gate-2.5 review disproved it: the miner's snapshot also
    carries threads, eddies and liminal notes, none of which the fragment
    cutoff touches, and a thread whose members are all above the ceiling titled
    a seed at ``ceiling=personal``. Equality is what
    ``StateReportGenerator._mining_corpus`` now establishes by narrowing the
    snapshot itself; the drop below ``personal`` closes the one axis narrowing
    cannot, because ``filter_fragments_by_tier`` summarises rather than omits.
    ``test_suggested_questions_omit_an_above_ceiling_thread_title`` pins the
    other half.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    section = _section_body(_report_body(canary_vault), "## Suggested questions")
    assert section, "the report is missing its '## Suggested questions' section"
    assert EMPTY_PLACEHOLDER in section, (
        "## Suggested questions must render the empty placeholder at "
        f"privacy_tier_ceiling=open; it rendered:\n\n{section}"
    )


def test_suggested_questions_return_above_the_open_ceiling(
    canary_vault: Path,
) -> None:
    """At ``ceiling=all`` the section surfaces seeds again — the positive control.

    Without this, "the ceiling is enforced" and "the section is permanently
    broken" are indistinguishable. The mechanism is exact rather than hopeful:
    ``phase_filtered_seeds`` runs every strategy for the ``unclassified`` phase
    this fixture carries, and ``mine_unexplored_ontology`` runs with
    ``require_corpus=True`` — it returns ``[]`` only when the ceiling-filtered
    snapshot holds no fragments at all. The canary vault holds many, so a
    non-placeholder section proves the corpus walk ran under the broad ceiling.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    section = _section_body(_report_body(canary_vault), "## Suggested questions")
    assert section, "the report is missing its '## Suggested questions' section"
    assert EMPTY_PLACEHOLDER not in section, (
        "## Suggested questions rendered the empty placeholder at "
        "privacy_tier_ceiling=all over a corpus with fragments in it, so the "
        "open-ceiling assertion in the companion test is satisfied by an "
        f"outage rather than by a gate:\n\n{section}"
    )


def test_drift_warnings_render_vault_relative_paths(canary_vault: Path) -> None:
    """Orphan rows name a vault-relative path, never the operator's home directory.

    ``docs/generation.md`` already claims "The report header renders the
    vault's leaf directory name only — never the absolute path — so committing
    or sharing the artefact does not leak an operator's home directory." That
    claim was false: ``OrphanScanner`` returns ``str(path)`` for an absolute
    path and the drift section rendered it verbatim. Rendering the path
    vault-relative is what makes the existing documentation true, and it is
    part of the same fix as the tier gate because the leaked string is the same
    string — a fragment's slugified title inside an absolute path.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    body = _report_body(canary_vault)
    relative = f"01-Fragments/Notes/public-{_DRIFT_OPEN}-note.md"
    assert relative in body, (
        "## Drift warnings must report an admitted orphan by its "
        f"vault-relative path; {relative!r} is absent from:\n\n"
        f"{_section_body(body, '## Drift warnings')}"
    )
    assert str(canary_vault) not in body, (
        "the rendered report embeds the absolute vault path "
        f"{str(canary_vault)!r}, which leaks the operator's home directory "
        "into an artifact meant to be committable and shareable."
    )


def test_pre_llm_yield_is_not_filtered_by_the_ceiling(canary_vault: Path) -> None:
    """The pre-LLM yield section reads the same at every ceiling, on purpose.

    It is the one section of the eleven with genuinely nothing to filter *by*:
    the last line of ``run-summary.jsonl`` records a pipeline **run** — a run
    id, a timestamp and four integers — and names no fragment, id, title or
    path. There is no tier to read and no item to drop. Its counts describe
    work the pipeline did, not content the vault holds.

    Contrast the two sections that *look* like the same class and are not — see
    :func:`test_wavelength_and_vault_summary_narrow_with_the_ceiling`. This test
    is asserted rather than left as prose because the instinct while closing
    #969 is to thread the override everywhere; if a future change decides this
    section should narrow too, it has to say so here.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=TierCeiling.OPEN)
    at_open = _report_body(canary_vault)
    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=TierCeiling.ALL)
    at_all = _report_body(canary_vault)

    header = "## Pre-LLM yield"
    assert _section_body(at_open, header) == _section_body(at_all, header), (
        f"{header} differs between privacy_tier_ceiling=open and "
        "privacy_tier_ceiling=all. It reports one pipeline run's totals and "
        "names no fragment, so there is nothing in it to filter by."
    )
    assert "- Residue: 1" in _section_body(at_open, header), (
        "the pre-LLM yield section rendered its placeholder, so the equality "
        "assertion above compared two empty strings"
    )


def test_wavelength_and_vault_summary_narrow_with_the_ceiling(tmp_path: Path) -> None:
    """The two *count* sections survey the admitted corpus, not the whole vault.

    These read like whole-corpus statistics — a phase share, a fragment count,
    a frequency histogram — and the tempting disposition is to leave them
    unfiltered on the grounds that filtering produces a different measurement
    rather than a safer one. This codebase has already rejected that argument
    once. ``creek.wheel`` is a frequency **tally** and it is ``GATED``;
    ``tests/test_mcp_read_gate.py``'s
    ``test_wheel_probe_still_counts_the_fragment_it_is_admitted_to`` asserts it
    counts only the fragment it is admitted to. A per-tier count is content
    here, so ``creek.state.render`` may not ship a ``GATED`` posture while
    printing ``Fragments: 100`` on a corpus of which 99 are above the caller's
    ceiling. The count discloses the *existence* of what the rest of the report
    was careful to omit.

    It also keeps the artifact stamp exact rather than merely conservative: the
    stamp is the maximum tier admitted, so a section that admits above-ceiling
    content without contributing to the maximum makes the stamp under-report,
    and a stamp that under-reports is a read gate that fails open.

    The fixture is built here rather than shared because the assertion is a
    count, and a count is only legible against a vault whose exact contents are
    visible in the test that reads it: two fragments, both inside the 28-day
    wavelength window, one ``open`` and one ``intimate``.

    Args:
        tmp_path: Pytest temporary directory.
    """
    vault = tmp_path / "count-vault"
    _seed_dirs(vault)
    _write_fragment(vault, frag_id="frag-count-open", privacy_tier="open", age_days=3)
    _write_fragment(
        vault,
        frag_id="frag-count-intimate",
        privacy_tier="intimate",
        age_days=3,
    )

    state_render_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.OPEN)
    at_open = _report_body(vault)
    state_render_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.ALL)
    at_all = _report_body(vault)

    summary_open = _section_body(at_open, "## Vault summary")
    summary_all = _section_body(at_all, "## Vault summary")
    assert "- Fragments: 1" in summary_open, (
        "## Vault summary counted the intimate fragment at "
        f"privacy_tier_ceiling=open. Expected 1 of 2:\n\n{summary_open}"
    )
    assert "- Fragments: 2" in summary_all, (
        "## Vault summary must count both fragments at "
        f"privacy_tier_ceiling=all, or the assertion above is vacuous:\n\n"
        f"{summary_all}"
    )

    wavelength_open = _section_body(at_open, "## Wavelength snapshot")
    wavelength_all = _section_body(at_all, "## Wavelength snapshot")
    assert "- Fragments observed: 1 " in wavelength_open, (
        "## Wavelength snapshot observed the intimate fragment at "
        f"privacy_tier_ceiling=open. Expected 1 of 2:\n\n{wavelength_open}"
    )
    assert "- Fragments observed: 2 " in wavelength_all, (
        "## Wavelength snapshot must observe both fragments at "
        f"privacy_tier_ceiling=all, or the assertion above is vacuous:\n\n"
        f"{wavelength_all}"
    )


# ---------------------------------------------------------------------------
# T2 — the stamp
# ---------------------------------------------------------------------------


def test_state_tiers_exports_the_stamp_key_constants() -> None:
    """The new module names the stamp's shape, and names the tier key ``privacy_tier``.

    The key must be ``privacy_tier`` — not a bespoke name — so the read side
    can reuse ``creek.classify.privacy_filter.raw_privacy_tier`` **verbatim**.
    A second tier reader is precisely the divergence that module's docstring
    warns about ("two tools that disagree about the same file"), and #969 adds
    none.

    ``type: state-report`` matters for a different reason: ``_load_typed_models``
    filters on ``type == "fragment"|"thread"|"eddy"|"praxis"``, so a stamped
    report cannot be mistaken for vault content by the very generator that
    wrote it.

    This is the one test in the file that imports the new module at all. Every
    other assertion spells the keys as literals, so a missing module fails here
    — where the contract is stated — rather than collapsing the artifact-level
    tests into a collection-time ``ImportError``.
    """
    state_tiers = importlib.import_module("creek.generate.state_tiers")

    assert state_tiers.STATE_REPORT_TYPE == _STAMP_TYPE_VALUE
    assert state_tiers.TIER_STAMP_KEY == _STAMP_TIER_KEY, (
        "the stamp must be written under 'privacy_tier' so the read gate can "
        "reuse raw_privacy_tier unchanged; a bespoke key would require a "
        "second tier reader"
    )
    assert state_tiers.CEILING_STAMP_KEY == _STAMP_CEILING_KEY


def test_write_stamps_the_report_with_type_tier_and_ceiling(
    canary_vault: Path,
) -> None:
    """``write()`` prefixes the report with a three-key YAML stamp.

    The stamp is what turns ``latest.md`` from a cross-ceiling cache into a
    self-describing artifact. ``privacy_tier`` is the highest tier actually
    admitted (what the read gate compares against); ``tier_ceiling`` is the
    override the render ran under, recorded for the audit trail and
    deliberately *not* consulted by the read gate — comparing it would refuse
    a broad render over a narrow corpus for no reason.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    stamp = _stamp(canary_vault)
    assert stamp[_STAMP_TYPE_KEY] == _STAMP_TYPE_VALUE
    assert stamp[_STAMP_TIER_KEY] == PrivacyTier.INTIMATE.value, (
        "a render at ceiling=all over a vault holding intimate content must "
        f"stamp 'intimate'; it stamped {stamp.get(_STAMP_TIER_KEY)!r}"
    )
    assert stamp[_STAMP_CEILING_KEY] == to_privacy_override(TierCeiling.ALL).value


def test_stamped_report_body_still_round_trips(canary_vault: Path) -> None:
    """Adding the stamp must not disturb a single byte of the rendered document.

    The report body is full of em-dashes, backticks and ``##`` headings, all of
    which are YAML-adjacent enough to be worth checking rather than assuming.
    ``frontmatter.loads`` must recover a body that still opens with the
    document header and still carries all eleven section headers **in
    ``SECTION_ORDER`` order** — the ordering contract FEAT-006 pinned and
    FEAT-007 extended.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    body = _report_body(canary_vault)
    assert body.startswith("# Creek state"), (
        "the stamp swallowed the document header; the body now opens with "
        f"{body[:80]!r}"
    )
    indices = [body.index(header) for header in SECTION_ORDER]
    assert indices == sorted(indices), (
        "the eleven sections are no longer in SECTION_ORDER order after "
        f"stamping: {indices}"
    )


@pytest.mark.parametrize("ceiling", _CEILINGS, ids=[c.value for c in _CEILINGS])
def test_stamped_tier_never_exceeds_the_ceiling_it_was_rendered_under(
    ceiling: TierCeiling,
    canary_vault: Path,
) -> None:
    """``privacy_tier <= tier_ceiling`` holds for every render, by construction.

    Every contributor to the stamp cleared ``tier_within_override(tier,
    override)`` on its way into the document, so the reduction over the
    admitted set cannot exceed the override. Asserting it closes the loop: a
    stamp higher than its own ceiling would mean content entered the report
    that the ceiling excluded, and a stamp lower than the admitted maximum
    would under-report the artifact's sensitivity and hand it to a reader who
    should have been refused.

    Args:
        ceiling: The ceiling the render runs under.
        canary_vault: The shared per-section canary vault.
    """
    from creek.classify.privacy_filter import tier_within_override

    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=ceiling)
    stamp = _stamp(canary_vault)
    stamped = PrivacyTier(str(stamp[_STAMP_TIER_KEY]))
    override = to_privacy_override(ceiling)
    assert tier_within_override(stamped, override), (
        f"a render at privacy_tier_ceiling={ceiling.value} stamped "
        f"{stamped.value!r}, a tier that ceiling does not admit. The stamp is "
        "a reduction over content that already cleared the ceiling, so this "
        "can only mean something entered the report ungated."
    )


def test_broad_render_over_an_open_corpus_stamps_open(tmp_path: Path) -> None:
    """``ceiling=all`` over a corpus with nothing above ``open`` stamps ``open``.

    This is the property that makes comparing the *stamp* strictly better than
    comparing the render ceiling, and it is load-bearing rather than incidental.
    A report rendered at ``ceiling=all`` over a vault whose content is all
    ``open`` contains nothing above ``open``; refusing it to an ``open``-ceiling
    caller because of *how it was produced* would make the artifact unreadable
    for no reason and push every caller towards re-rendering.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_open_only_vault(tmp_path)
    state_render_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.ALL)
    assert _stamp(vault)[_STAMP_TIER_KEY] == PrivacyTier.OPEN.value, (
        "an all-ceiling render over an all-open corpus must stamp 'open'; it "
        f"stamped {_stamp(vault).get(_STAMP_TIER_KEY)!r}. Stamping the "
        "*ceiling* rather than the admitted maximum makes a broad render "
        "unreadable at a narrow ceiling for no reason."
    )
    served = state_read_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.OPEN)
    assert served["status"] == "ok"


def test_fresh_vault_report_stamps_open_not_intimate(tmp_path: Path) -> None:
    """An empty vault's report stamps ``open`` — the ``default=OPEN`` decision.

    This is the one place the stamp's reduction deliberately diverges from
    ``creek.classify.privacy_filter.max_source_tier``, whose empty case is
    ``INTIMATE``. The two empties mean different things. ``max_source_tier``'s
    means "we asked for fragments and got none, so we do not know what the call
    would carry" — absence of knowledge. Here it means "the rendered document
    contains no tiered content" — knowledge. Stamping a fresh vault's report
    ``intimate`` would make it unreadable at every ceiling below ``intimate``,
    which is exactly what #969's recoverability criterion forbids.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "fresh-vault"
    _seed_dirs(vault)
    state_render_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.OPEN)
    assert _stamp(vault)[_STAMP_TIER_KEY] == PrivacyTier.OPEN.value, (
        "a report over a vault with no tiered content must stamp 'open'. "
        "Failing closed here would make a freshly-initialised vault's report "
        "unreadable at the default ceiling, i.e. an outage on first run."
    )
    served = state_read_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.OPEN)
    assert served["status"] == "ok", (
        "a fresh vault's report is unreadable at the default ceiling, which is "
        "an outage on first run rather than a privacy gate"
    )


def test_latest_md_carries_the_same_stamp_as_the_iso_week_file(
    canary_vault: Path,
) -> None:
    """The symlinked ``latest.md`` and the ISO-week file stamp identically.

    ``latest.md`` is the documented session-start context, so it is the file
    almost every reader actually opens. A stamp that lived only on the
    ISO-week file would leave the read gate looking at an unstamped artifact
    and refusing everything.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert _stamp(canary_vault, _latest_file(canary_vault)) == _stamp(canary_vault)


def _assert_latest_is_a_stamped_copy(vault: Path) -> None:
    """Assert ``latest.md`` is a real file carrying the ISO-week file's stamp.

    Shared by the two copy-fallback tests below, which reach the same branch of
    ``_refresh_latest`` through two different triggers.

    Args:
        vault: Vault root, already rendered into.
    """
    latest = _latest_file(vault)
    week = _iso_week_file(vault)
    assert not latest.is_symlink(), (
        "the copy fallback was not taken, so this test is asserting about the "
        "symlink branch and the copy branch is unexercised"
    )
    assert _stamp(vault, latest) == _stamp(vault, week), (
        "latest.md lost its stamp on the copy path. latest.md is the "
        "documented session-start context and the file almost every reader "
        "opens; an unstamped copy there means the read gate sees a legacy "
        "artifact and refuses everything."
    )
    assert latest.read_text(encoding="utf-8") == week.read_text(encoding="utf-8")


def test_latest_md_copy_fallback_on_windows_carries_the_stamp(
    canary_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.name == "nt"`` skips the symlink and copies — with the stamp intact.

    Windows lacks an unprivileged symlink path for non-developers, so
    ``_refresh_latest`` never attempts one there. The copy writes ``latest.md``
    as an independent file, which makes it the branch where a stamp could
    plausibly be lost — and the branch every Windows operator is permanently
    on, so a loss there is not an edge case for them.

    The render runs **before** ``os.name`` is patched, and only
    ``_refresh_latest`` is re-entered under the patch. Patching ``os.name`` is
    not a local edit: there is one ``os`` module object, and
    ``pathlib.Path(...)`` dispatches on ``os.name`` to choose ``PosixPath`` or
    ``WindowsPath``. Holding the patch across the render therefore makes every
    ``Path`` built from a string during it a ``WindowsPath`` on a POSIX host —
    which blows up ``_vault_relative``'s ``relative_to`` against a POSIX vault
    root, in a way that says nothing about either Windows or the stamp. Scoping
    the patch to the one function whose ``os.name`` check is under test keeps
    the failure this asserts about the copy branch.

    Args:
        canary_vault: The shared per-section canary vault.
        monkeypatch: pytest's patching fixture.
    """
    written = state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    state_dir = canary_vault / "00-Creek-Meta" / "State"
    target = canary_vault / str(written["report_path"])
    monkeypatch.setattr("creek.generate.state.os.name", "nt")
    _refresh_latest(state_dir, target)
    _assert_latest_is_a_stamped_copy(canary_vault)


def test_latest_md_copy_fallback_on_symlink_error_carries_the_stamp(
    canary_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filesystem that rejects symlinks falls through to the same stamped copy.

    The second trigger for the copy branch, and a distinct one: some networked
    filesystems accept the ``os.name`` check and then reject
    ``Path.symlink_to`` with ``EPERM``. Exercised separately because the
    Windows test never reaches the ``try`` block at all, so it cannot show that
    the ``except OSError`` path also stamps.

    Args:
        canary_vault: The shared per-section canary vault.
        monkeypatch: pytest's patching fixture.
    """

    def _refuse_symlink(self: Path, target: str | Path) -> None:
        """Raise as a symlink-hostile filesystem would.

        Args:
            self: The link path being created.
            target: The link target.

        Raises:
            OSError: Always.
        """
        msg = "symlink refused (test)"
        raise OSError(msg)

    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    _assert_latest_is_a_stamped_copy(canary_vault)


# ---------------------------------------------------------------------------
# T3 — ``creek.state.read`` refuses, and the refusal discloses nothing
# ---------------------------------------------------------------------------


def _write_latest(vault: Path, text: str) -> Path:
    """Hand-write ``latest.md`` verbatim, bypassing the generator.

    Used for the legacy and malformed-stamp cases, which by definition cannot
    be produced by the fixed writer.

    Args:
        vault: Vault root.
        text: Exact bytes to write.

    Returns:
        The path written.
    """
    target = _latest_file(vault)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _stamped_latest(vault: Path, tier_value: object) -> Path:
    """Write a ``latest.md`` stamped with an arbitrary ``privacy_tier`` value.

    The value is written straight into the YAML, so a list, a mapping or a
    ``None`` reaches the reader exactly as a hand-edited vault note would.

    Args:
        vault: Vault root.
        tier_value: Whatever the ``privacy_tier`` key should hold.

    Returns:
        The path written.
    """
    metadata: dict[str, Any] = {
        _STAMP_TYPE_KEY: _STAMP_TYPE_VALUE,
        _STAMP_TIER_KEY: tier_value,
        _STAMP_CEILING_KEY: "all",
    }
    post = frontmatter.Post(content="# Creek state\n\nbody\n", **metadata)
    return _write_latest(vault, frontmatter.dumps(post))


def test_state_read_refuses_an_above_ceiling_stamp_with_the_canonical_payload(
    canary_vault: Path,
) -> None:
    """A stamped-``intimate`` report is refused at ``open``: four keys, no more.

    ``creek.state.read`` **refuses** rather than excludes, and the asymmetry
    with ``creek.state.render`` is deliberate. Read's target is one atomic
    cached artifact: there is nothing to partially admit, because you cannot
    exclude half a rendered markdown document without re-rendering it — and
    re-rendering is what ``render`` is. Render names no target at all; it is a
    corpus walk like ``report``/``wheel``/``mine``, and refusing it would make
    the report unreachable, which is #968's explicit anti-goal.

    The payload is compared for equality against
    :func:`~creek_mcp.tier_ceiling.refusal_response` rather than key by key, so
    an extra key added "for debugging" fails here. Every extra key on a refusal
    is derived from content the caller was not admitted to.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    response = state_read_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert response == refusal_response(
        tool="creek.state.read",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    )
    serialised = json.dumps(response, default=str)
    for canary in _ALL_INTIMATE_CANARIES:
        assert canary not in serialised, (
            f"creek.state.read returned the above-ceiling sentinel {canary!r} "
            f"at privacy_tier_ceiling=open:\n\n{serialised}"
        )


@pytest.mark.parametrize(
    "stamped",
    ["personal", "intimate", "unclassified"],
    ids=["personal", "intimate", "unclassified"],
)
def test_refusal_is_byte_identical_for_every_above_ceiling_stamp(
    stamped: str,
    tmp_path: Path,
) -> None:
    """The refusal must not rank the content the caller was refused.

    A reason, a key, or even a difference in whitespace that varied with the
    stamped tier would turn every refusal into a tier-classification oracle
    over the artifact: a caller could learn "the report contains intimate
    content" without being admitted to a word of it. So the refusal is asserted
    byte-identical across every above-ceiling stamp at the same ceiling, and
    the three tier words are asserted absent from the serialised payload
    entirely at ``ceiling=open``, where none of them is the caller's own echo.

    Args:
        stamped: The tier stamped on ``latest.md``.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / f"oracle-{stamped}"
    _seed_dirs(vault)
    _stamped_latest(vault, stamped)

    response = state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert response["status"] == "refused"
    assert response["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert response == refusal_response(
        tool="creek.state.read",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    ), (
        f"the refusal for a report stamped {stamped!r} differs from the "
        "canonical payload, so the refusal itself ranks the content the "
        "caller was not admitted to"
    )

    serialised = json.dumps(response, default=str, sort_keys=True)
    for word in ("personal", "intimate", "unclassified"):
        assert word not in serialised, (
            f"the refusal payload names the tier word {word!r} at "
            f"privacy_tier_ceiling=open, where the caller's own echoed "
            f"ceiling is 'open'. That is a tier oracle:\n\n{serialised}"
        )


def test_refusals_at_a_personal_ceiling_are_identical_across_stamps(
    tmp_path: Path,
) -> None:
    """At ``ceiling=personal`` the echo is legitimate; identity is the real claim.

    The word ``"personal"`` appears in this refusal because the caller supplied
    it, so the absence check used at ``ceiling=open`` cannot be applied here.
    What still can be — and is the stronger statement anyway — is that the
    refusal for an ``intimate`` stamp is byte-identical to the refusal for an
    unstamped legacy report. If the two differed, a caller could distinguish
    "the report holds intimate content" from "the report predates the stamp"
    without being admitted to either.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    stamped_vault = tmp_path / "personal-stamped"
    _seed_dirs(stamped_vault)
    _stamped_latest(stamped_vault, "intimate")

    legacy_vault = tmp_path / "personal-legacy"
    _seed_dirs(legacy_vault)
    _write_latest(legacy_vault, "# Audit report\n\n- Fragments: 1\n")

    stamped = state_read_tool(
        vault_path=stamped_vault,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    legacy = state_read_tool(
        vault_path=legacy_vault,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert stamped["status"] == "refused"
    assert json.dumps(stamped, default=str, sort_keys=True) == json.dumps(
        legacy,
        default=str,
        sort_keys=True,
    ), (
        "an above-ceiling refusal and a legacy-unstamped refusal are "
        "distinguishable, so the pair discloses whether the vault holds "
        "above-ceiling content"
    )


@pytest.mark.parametrize("ceiling", _CEILINGS, ids=[c.value for c in _CEILINGS])
def test_legacy_unstamped_report_fails_closed(
    ceiling: TierCeiling,
    tmp_path: Path,
) -> None:
    """An unstamped ``latest.md`` is refused below ``intimate`` and served at or above.

    This is *accurate* rather than merely conservative. Every ``latest.md``
    written before #969 was rendered completely unfiltered — the equivalent of
    ``--include-tier all`` — so ``raw_privacy_tier({}) -> INTIMATE`` states a
    fact about those bytes rather than a precaution about them.

    The upgrade path is one command and costs nothing: ``creek.state.render``
    re-renders and re-stamps at the caller's ceiling, or ``creek state
    --include-tier open`` does the same from the CLI. The ISO-week archive is
    untouched either way.

    Args:
        ceiling: The caller's declared ceiling.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / f"legacy-{ceiling.value}"
    _seed_dirs(vault)
    _write_latest(vault, "# Audit report\n\nVault summary lives here.\n")

    response = state_read_tool(vault_path=vault, privacy_tier_ceiling=ceiling)
    if ceiling in _ADMITS_INTIMATE:
        assert response["status"] == "ok", (
            f"an unstamped legacy report must be served at ceiling="
            f"{ceiling.value}, which admits intimate content. Refusing it "
            "there would make the report permanently unreachable."
        )
        assert "Vault summary lives here." in response["content"]
    else:
        assert response["status"] == "refused", (
            f"an unstamped legacy report was served at ceiling={ceiling.value}. "
            "Those bytes were rendered completely unfiltered, so serving them "
            "below ceiling=intimate hands out whatever the vault held."
        )
        assert response["reason"] == GENERIC_ABOVE_CEILING_REASON


@pytest.mark.parametrize(
    ("tier_value", "admitted_at_open"),
    [
        pytest.param("not-a-tier", False, id="unrecognised"),
        pytest.param("", False, id="empty"),
        pytest.param(None, False, id="none"),
        pytest.param(["open"], False, id="list"),
        pytest.param({"tier": "open"}, False, id="mapping"),
        pytest.param(0, False, id="int"),
        pytest.param(False, False, id="bool"),
        pytest.param("OPEN", False, id="uppercase"),
        pytest.param(" open", False, id="padded"),
        pytest.param("open", True, id="open"),
    ],
)
def test_unreadable_stamp_values_fail_closed(
    tier_value: object,
    admitted_at_open: bool,
    tmp_path: Path,
) -> None:
    """Every ``privacy_tier`` the reader cannot parse lands on ``intimate``.

    Mirrors ``raw_privacy_tier``'s pinned rows in
    ``tests/test_mcp_report_tier_ceiling.py`` deliberately, because the read
    gate must *be* that reader rather than resemble it. The rows that bite are
    the non-string ones: YAML front matter is operator-editable, and a
    one-character slip (``privacy_tier:`` followed by an indented ``- open``)
    produces ``["open"]``, which must fail closed rather than be talked into
    ``open`` by some future normalisation. Casing and padding are not silently
    repaired either — guessing what an operator meant is how a gate becomes
    negotiable.

    Args:
        tier_value: The value written into the stamp.
        admitted_at_open: Whether the reader must admit it at ``ceiling=open``.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "stamp-shapes"
    _seed_dirs(vault)
    _stamped_latest(vault, tier_value)

    response = state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    expected = "ok" if admitted_at_open else "refused"
    assert response["status"] == expected, (
        f"a report stamped privacy_tier={tier_value!r} was {response['status']!r} "
        f"at ceiling=open; expected {expected!r}. A tier nobody can read is a "
        "tier nobody has vouched for."
    )


def test_missing_report_is_empty_not_refused(tmp_path: Path) -> None:
    """A fresh vault with no report stays usable, and discloses nothing either way.

    A *missing* report is not a refusal: there is no content to be above the
    ceiling, and answering ``refused`` there would be a vault-emptiness oracle
    in the other direction — a caller could distinguish "you may not read this"
    from "there is nothing to read" only by the absence of that distinction.
    Keeping the ``empty`` branch is also what stops a first run of CrawDad or
    ``/creek`` looking like a permissions failure.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = tmp_path / "no-report"
    _seed_dirs(vault)
    response = state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert response["status"] == "empty"
    assert response["content"] == ""


# ---------------------------------------------------------------------------
# T4 — recoverability, executed rather than read off a comparator
# ---------------------------------------------------------------------------


def test_render_at_all_then_read_at_all_returns_the_content(
    canary_vault: Path,
) -> None:
    """The broad path works end to end: render at ``all``, read at ``all``.

    The first leg of #969's recoverability criterion. Without it the refusal
    tests above would be satisfied by a read gate that refused everything.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=TierCeiling.ALL)
    response = state_read_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert response["status"] == "ok"
    assert _LIMINAL_INTIMATE in response["content"], (
        "a read at ceiling=all returned a report with no above-ceiling content "
        "in it, so the refusal at ceiling=open proves nothing about the gate"
    )


def test_the_same_artifact_read_at_open_is_refused_then_recovers_on_re_render(
    canary_vault: Path,
) -> None:
    """One artifact, two ceilings, one command to recover — executed, not described.

    The whole sequence runs for real:

    1. render at ``ceiling=all`` → the artifact genuinely holds intimate content;
    2. read at ``ceiling=open`` → refused, because the stamp says so;
    3. render at ``ceiling=open`` → the artifact is re-rendered and re-stamped;
    4. read at ``ceiling=open`` → ``ok``, and canary-free.

    Step 3 is the documented recovery, and step 4 is the proof it works.
    ``latest.md`` is a single slot shared across ceilings — kept single
    deliberately, because per-ceiling filenames would multiply artifacts in the
    operator's vault and break ``latest.md`` as the documented session-start
    context — so the ``all`` caller's richer report is replaced here. That is
    the cache-thrash consequence, stated on the tin and exercised.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=TierCeiling.ALL)
    refused = state_read_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert refused["status"] == "refused"

    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=TierCeiling.OPEN)
    recovered = state_read_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert recovered["status"] == "ok", (
        "re-rendering at ceiling=open did not make the report readable at "
        "ceiling=open, so the refusal above is not recoverable and the gate "
        "has made the artifact unreachable"
    )
    for canary in _ALL_INTIMATE_CANARIES:
        assert canary not in recovered["content"], (
            f"the re-rendered open report still carries {canary!r}"
        )
    assert _DRIFT_OPEN in recovered["content"], (
        "the re-rendered open report carries no admitted content at all, so "
        "'recovered' means 'empty'"
    )


@pytest.mark.parametrize("ceiling", _CEILINGS, ids=[c.value for c in _CEILINGS])
def test_no_render_ceiling_makes_the_report_permanently_unreachable(
    ceiling: TierCeiling,
    canary_vault: Path,
) -> None:
    """After a render at *any* ceiling, a read at ``ceiling=all`` is always ``ok``.

    The safety net under every other gate in this file. ``ALL`` admits every
    stamp, including the legacy unstamped one, so there is no sequence of calls
    that can leave an operator locked out of their own report. If a future
    change ever makes this fail, the gate has stopped being a filter and
    started being a wall.

    Args:
        ceiling: The ceiling the render runs under.
        canary_vault: The shared per-section canary vault.
    """
    state_render_tool(vault_path=canary_vault, privacy_tier_ceiling=ceiling)
    response = state_read_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert response["status"] == "ok", (
        f"a report rendered at privacy_tier_ceiling={ceiling.value} is not "
        "readable even at ceiling=all. No render may make the report "
        "permanently unreachable."
    )
    assert "# Creek state" in response["content"]


# ---------------------------------------------------------------------------
# T5 — mutation resistance
# ---------------------------------------------------------------------------


class _InvertingPredicate:
    """A tier predicate stand-in that returns the opposite verdict.

    Wraps the real predicate and negates it, counting calls. Two distinct
    mutations die against it:

    * a section that *calls* the gate and discards its result is unaffected by
      inversion, so its artifact still holds the below-ceiling content and the
      inverted expectation fails;
    * a section that stopped calling the gate leaves ``call_count`` at zero.

    Takes ``*args``/``**kwargs`` so one class covers both
    ``within_ceiling(raw, override)`` and ``tier_within_override(tier,
    override)``, and so a keyword call site is not silently missed.

    Attributes:
        call_count: How many times the predicate was invoked.
    """

    def __init__(self, real: Any) -> None:
        """Store the real predicate and zero the counter.

        Args:
            real: The genuine predicate this stands in for.
        """
        self._real = real
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        """Return the negation of the real verdict.

        Args:
            *args: Positional arguments forwarded verbatim.
            **kwargs: Keyword arguments forwarded verbatim.

        Returns:
            ``not real(*args, **kwargs)``.
        """
        self.call_count += 1
        return not self._real(*args, **kwargs)


def _import_optional(dotted: str) -> ModuleType | None:
    """Import *dotted*, returning ``None`` when the module does not exist yet.

    ``creek.generate.state_tiers`` is created by #969. Treating its absence as
    "not a binding site" rather than as an error keeps the mutation tests
    failing on the behaviour they describe instead of on the module layout.

    Args:
        dotted: Dotted module path.

    Returns:
        The module, or ``None``.
    """
    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError:
        return None


def _patch_state_predicate(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
) -> tuple[_InvertingPredicate, list[str]]:
    """Replace *symbol* with an inverting stand-in in every state module holding it.

    A ``from ... import within_ceiling`` binds the callable in the *importing*
    module's namespace, so patching ``creek.classify.privacy_filter`` would
    change nothing the state generator can see. Patching every state-side
    binding — and reporting which ones were found — is what makes this test
    independent of whether #969 puts the call in ``state.py`` or
    ``state_tiers.py``, while still failing loudly if it is in neither.

    Args:
        monkeypatch: pytest's patching fixture.
        symbol: The predicate name to replace.

    Returns:
        The shared spy and the dotted paths it was installed into.
    """
    real: Any = None
    for dotted in _STATE_MODULES:
        module = _import_optional(dotted)
        if module is not None and hasattr(module, symbol):
            real = getattr(module, symbol)
            break
    spy = _InvertingPredicate(real)
    patched: list[str] = []
    for dotted in _STATE_MODULES:
        module = _import_optional(dotted)
        if module is not None and hasattr(module, symbol):
            monkeypatch.setattr(module, symbol, spy)
            patched.append(dotted)
    return spy, patched


_MUTATION_ROWS = [
    pytest.param(
        "within_ceiling",
        _LIMINAL_INTIMATE,
        _LIMINAL_OPEN,
        "## Liminal Watch",
        id="within_ceiling-liminal",
    ),
    pytest.param(
        "within_ceiling",
        _DRIFT_INTIMATE,
        _DRIFT_OPEN,
        "## Drift warnings",
        id="within_ceiling-drift",
    ),
    pytest.param(
        "tier_within_override",
        _EDDY_INTIMATE,
        _EDDY_OPEN,
        "## Active eddies",
        id="tier_within_override-eddies",
    ),
    pytest.param(
        "tier_within_override",
        _THREAD_INTIMATE,
        _THREAD_OPEN,
        "## Active threads",
        id="tier_within_override-threads",
    ),
]
"""``(predicate, above canary, below canary, section)`` rows for the inversion test.

Two predicates, because #969 gates two different shapes of thing.
``within_ceiling`` reads a note's *raw front matter* and is what the liminal
notes and the fragment admission behind drift warnings reduce to.
``tier_within_override`` takes an already-resolved tier and is what the
*derived* eddy/thread tiers — reduced with ``max_source_tier`` over their
member fragments — must be cut off against, since neither model carries a
``privacy_tier`` field of its own.
"""


@pytest.mark.parametrize(
    ("symbol", "above_canary", "below_canary", "section"),
    _MUTATION_ROWS,
)
def test_state_sections_act_on_the_gate_verdict_not_just_call_it(
    symbol: str,
    above_canary: str,
    below_canary: str,
    section: str,
    canary_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverting the gate must invert the artifact.

    Everything else in this file is satisfied by a section that invokes the
    gate on the request path and then ignores what it said — the call site
    exists, the import exists, an AST walk finds it, and the artifact is
    whatever it always was. Only replacing the predicate with one that answers
    *backwards* can tell "consulted" from "obeyed".

    Three failures are distinguished, each with its own message: the predicate
    was never bound in a state module at all; it was bound but never called; or
    it was called and its verdict discarded.

    Args:
        symbol: The predicate name to invert.
        above_canary: The sentinel that must *appear* once the gate is inverted.
        below_canary: The sentinel that must *vanish* once the gate is inverted.
        section: The section whose rows carry those sentinels.
        canary_vault: The shared per-section canary vault.
        monkeypatch: pytest's patching fixture.
    """
    spy, patched = _patch_state_predicate(monkeypatch, symbol)
    assert patched, (
        f"no module in {list(_STATE_MODULES)} binds {symbol!r}, so {section} "
        "cannot be gated by it. Import the predicate into the module that owns "
        "the section's admission decision."
    )

    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    text = _artifact_text(canary_vault)

    assert spy.call_count > 0, (
        f"{symbol} was never called while rendering {section} at ceiling=open, "
        f"even though it is bound in {patched}. A gate off the request path "
        "enforces nothing."
    )
    assert above_canary in text, (
        f"inverting {symbol} did not change what {section} wrote: "
        f"{above_canary!r} is still absent from the artifact. The section "
        "calls the gate and discards its verdict.\n\n"
        f"{text}"
    )
    assert below_canary not in text, (
        f"inverting {symbol} left {below_canary!r} in {section}, so the "
        "verdict is not what decides admission there.\n\n"
        f"{text}"
    )


def test_no_file_the_render_touches_carries_above_ceiling_content(
    canary_vault: Path,
) -> None:
    """Every file the call created or modified is swept, not a named list.

    The artifact-level tests above look where we already know to look. This one
    derives its file list from the filesystem — everything whose bytes differ
    from a pre-call snapshot — so it also covers an artifact nobody anticipated,
    a cache, a debug dump, and a second ungated walk spliced in next to a
    still-present, still-called gate whose declared output looks unchanged.
    That last case is the one the structural layers of
    ``tests/test_mcp_read_gate.py`` provably cannot see.

    Paths are checked as well as contents because the drift sentinel rides in a
    filename: a bytes-only sweep would walk straight past a file *named* after
    an above-ceiling fragment.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    before = _snapshot(canary_vault)
    state_render_tool(
        vault_path=canary_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    changed = _changed_files(canary_vault, before)
    assert changed, (
        "creek.state.render changed no file at all, so this sweep has nothing "
        "to sweep and every assertion below it is vacuous"
    )

    for path in changed:
        rel = str(path.relative_to(canary_vault))
        for canary in _ALL_INTIMATE_CANARIES:
            assert canary not in rel, (
                f"creek.state.render created {rel!r} at ceiling=open — the "
                f"above-ceiling sentinel {canary!r} is in the file name."
            )
            assert canary.encode("utf-8") not in path.read_bytes(), (
                f"creek.state.render wrote the above-ceiling sentinel "
                f"{canary!r} into {rel!r} at ceiling=open. This file was found "
                "by diffing the vault, not from a list of expected artifacts, "
                "so a second ungated read cannot hide behind an unchanged "
                "declared output."
            )

    text = _artifact_text(canary_vault)
    for canary in _ALL_OPEN_CANARIES:
        assert canary in text, (
            f"the admitted sentinel {canary!r} is missing from the report, so "
            "the sweep above passed on an empty artifact"
        )


# ---------------------------------------------------------------------------
# T6 — the generator's own override keyword, and the CLI flag
# ---------------------------------------------------------------------------


def test_generator_defaults_to_an_unfiltered_render(canary_vault: Path) -> None:
    """``StateReportGenerator`` with no ``override`` renders everything.

    The default is ``PrivacyTierOverride.ALL`` because the library's existing
    callers — and the CLI operator, who is the vault owner — get today's
    behaviour unless they ask to narrow it. That matches every other
    ``--include-tier`` surface in the CLI, whose help text already says
    "Omitting the flag leaves the scan unfiltered ... this flag NARROWS what
    the generated artifact may contain."

    Args:
        canary_vault: The shared per-section canary vault.
    """
    StateReportGenerator(vault_path=canary_vault).write()
    text = _artifact_text(canary_vault)
    for canary in _ALL_INTIMATE_CANARIES:
        assert canary in text, (
            f"an override-less render dropped {canary!r}. The default must be "
            "unfiltered, or #969 silently narrows every existing caller."
        )


def test_generator_override_keyword_narrows_the_render(canary_vault: Path) -> None:
    """``StateReportGenerator(override=OPEN)`` is the CLI's and the tool's one lever.

    Both ``creek state --include-tier`` and ``creek_mcp.tools.state`` reach the
    ceiling through this keyword, so it is pinned directly rather than only
    through its two callers — a signature change that broke one of them would
    otherwise show up as a confusing failure in the other.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    from creek.classify.privacy_filter import PrivacyTierOverride

    StateReportGenerator(
        vault_path=canary_vault,
        override=PrivacyTierOverride.OPEN,
    ).write()
    text = _artifact_text(canary_vault)
    for canary in _ALL_INTIMATE_CANARIES:
        assert canary not in text, (
            f"StateReportGenerator(override=OPEN) wrote {canary!r} into the "
            f"artifact:\n\n{text}"
        )
    assert _DRIFT_OPEN in text, (
        "the narrowed render dropped admitted content too, so the exclusion "
        "assertions above are satisfied by an outage"
    )


def test_cli_state_rejects_an_unknown_include_tier(canary_vault: Path) -> None:
    """A bogus ``--include-tier`` value exits 2 rather than falling back.

    Matches ``_parse_include_tier``'s behaviour on every other generation
    surface. Falling back to a default on an unparsable value is how a typo
    (``--include-tier itimate``) silently becomes a broader render than the
    operator asked for — or a narrower one they did not notice.

    The *message* is asserted, not just the exit code, and that is the point:
    a CLI that has never heard of ``--include-tier`` also exits 2, with Click's
    "No such option". Requiring the rejected value **and** the canonical option
    list in the output is what separates "the flag exists and refused this
    word" from "the flag does not exist", so this test cannot go green on a
    build where nothing was implemented.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    result = runner.invoke(
        app,
        ["state", "--vault", str(canary_vault), "--include-tier", "leaky"],
    )
    assert result.exit_code == 2, result.output
    plain = _plain(result.output)
    assert "leaky" in plain, (
        "creek state exited 2 without naming the value it rejected, which is "
        "what Click prints when the option does not exist at all: "
        f"{plain!r}"
    )
    for member in ("open", "personal", "intimate", "all"):
        assert member in plain, (
            f"the rejection message does not offer {member!r} as a valid "
            f"--include-tier value: {plain!r}"
        )


@pytest.mark.parametrize(
    "value",
    ["open", "personal", "intimate", "all"],
)
def test_cli_state_accepts_every_override_value(value: str, tmp_path: Path) -> None:
    """``--include-tier`` takes the same four words as every other surface.

    The four values mirror :class:`~creek.classify.privacy_filter.PrivacyTierOverride`
    exactly, so the CLI and the MCP ceiling stay in lock-step; a surface that
    accepted three of them would make ``creek state`` the odd one out and
    invite a per-command mental model.

    Args:
        value: The ``--include-tier`` value under test.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_canary_vault(tmp_path, name=f"cli-{value}")
    result = runner.invoke(
        app,
        ["state", "--vault", str(vault), "--include-tier", value],
    )
    assert result.exit_code == 0, result.output
    assert _iso_week_file(vault).exists()


def test_cli_state_include_tier_open_excludes_above_ceiling_content(
    canary_vault: Path,
) -> None:
    """``creek state --include-tier open`` narrows the artifact it writes.

    The CLI is the documented recovery path for an operator whose ``latest.md``
    predates the stamp, so it has to produce a genuinely open report rather
    than an open-stamped copy of an unfiltered one.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    result = runner.invoke(
        app,
        ["state", "--vault", str(canary_vault), "--include-tier", "open"],
    )
    assert result.exit_code == 0, result.output
    text = _artifact_text(canary_vault)
    for canary in _ALL_INTIMATE_CANARIES:
        assert canary not in text, (
            f"creek state --include-tier open wrote {canary!r} into "
            f"00-Creek-Meta/State:\n\n{text}"
        )
    assert _DRIFT_OPEN in text


def test_cli_state_without_include_tier_stays_unfiltered(canary_vault: Path) -> None:
    """Omitting the flag preserves today's output, byte for byte in intent.

    ``creek state`` has always rendered the whole vault, and the operator
    running it is the vault owner. Silently narrowing the default would change
    what an existing script produces without anybody asking for it — and would
    make this issue a behaviour regression wearing a privacy fix's costume.

    Args:
        canary_vault: The shared per-section canary vault.
    """
    result = runner.invoke(app, ["state", "--vault", str(canary_vault)])
    assert result.exit_code == 0, result.output
    text = _artifact_text(canary_vault)
    for canary in _ALL_INTIMATE_CANARIES:
        assert canary in text, (
            f"creek state with no --include-tier dropped {canary!r}; the "
            "default must leave the render unfiltered"
        )


def test_cli_state_help_mentions_include_tier() -> None:
    """``creek state --help`` advertises the flag.

    A gate the operator cannot discover is a gate they will not use, and this
    flag is half of the documented upgrade path for a pre-#969 ``latest.md``.
    """
    result = runner.invoke(app, ["state", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)


# ---------------------------------------------------------------------------
# T7 — three leaks the Gate-2.5 review of #969 found after T1-T6 were green
#
# Every one of them travels a path the tables above provably cannot reach, so
# each is written here as a standalone test over its own vault rather than as a
# row of _SECTIONS. The specific reason a row would not work is recorded on the
# test that replaces it; in outline:
#
# * the hyperedge-span leak needs an eddy with *mixed*-tier membership, and
#   _build_canary_vault deliberately has none (its comment says so: sharing an
#   eddy between the two praxis rows "would give each shared eddy one intimate
#   and one open member");
# * the mined-thread leak needs a thread over the mining threshold
#   (``fragment_count > DEFAULT_MIN_THREAD_FRAGMENTS``), i.e. twelve member
#   fragments, where every fixture thread above has one;
# * the non-YAML stamp is a property of the artifact's *format*, and the
#   malformed-stamp parametrisation above varies a *value* underneath a
#   well-formed YAML stamp.
#
# Adding any of those to the shared fixtures would change what every existing
# row of _SECTIONS asserts, so nothing above this banner is touched.
# ---------------------------------------------------------------------------


# --- Blocker 1: an above-ceiling eddy title riding into ## Hyperedges -------

_SPAN_EDDY_ABOVE = "CANARY-SPANEDDY-INTIMATE-4e6b"
"""Rides in the ``title`` of an eddy with one ``open`` and one ``intimate`` member.

Its derived tier is the maximum over its members — ``intimate`` — so
``## Active eddies`` already excludes it at ``ceiling=open`` and has since T1
went green. That is precisely what made this leak invisible: the sentinel is
*absent* from the section that owns eddy titles while riding into
``## Hyperedges`` inside another section's ``spans:`` list, because
``StateReportGenerator._fragment_to_eddies`` maps each **admitted** fragment to
every eddy its ``eddies:`` wikilinks name and never intersects that set against
``self._state.eddies``.
"""

_SPAN_PRAXIS_BRIDGE = "CANARY-SPANPRAXIS-BRIDGE-1d5f"
"""Rides in the ``title`` of the praxis whose spanned set carries the leak.

Both of its ``derived_from`` fragments are ``open``, so the praxis itself is
admitted at ``ceiling=open`` — the leak is not "an above-ceiling praxis was
rendered" but "an admitted praxis printed an above-ceiling eddy's title".
"""

_SPAN_PRAXIS_CONTROL = "CANARY-SPANPRAXIS-CONTROL-9a70"
"""Rides in the ``title`` of a praxis spanning two eddies that are *both* admitted.

The non-vacuity control for the whole section. Without a second row that
survives at ``ceiling=open``, deleting ``## Hyperedges`` outright — or gating
the section whole the way ``## Lint summary`` is gated — would satisfy every
exclusion assertion below while being an outage.
"""

_SPAN_EDDY_ABOVE_TITLE = f"Eddy-{_SPAN_EDDY_ABOVE}"
"""Title *and* file stem of the mixed-tier eddy, for :func:`_write_eddy`'s reason."""

_SPAN_EDDY_PLAIN_TITLE = "Eddy-Span-Plain"
"""The bridge praxis's second, genuinely-``open`` span.

Sentinel-free on purpose: it must appear in the artifact at every ceiling, so a
canary here would be asserted both present and absent depending on the row
doing the asserting.
"""

_SPAN_EDDY_CONTROL_TITLES = ("Eddy-Span-Control-A", "Eddy-Span-Control-B")
"""The control praxis's two spans, both ``open``-derived and both admitted."""

_SPAN_BRIDGE_TITLE = f"Praxis {_SPAN_PRAXIS_BRIDGE} bridge"
"""Full praxis title, spelled once so fixture and expected row cannot drift."""

_SPAN_CONTROL_TITLE = f"Praxis {_SPAN_PRAXIS_CONTROL} control"
"""Full control-praxis title, for the same reason."""

_SPAN_BRIDGE_ROW = (
    f"- {_SPAN_BRIDGE_TITLE} — spans: "
    f"{_SPAN_EDDY_ABOVE_TITLE}, {_SPAN_EDDY_PLAIN_TITLE}"
)
"""The exact ``## Hyperedges`` row the bridge praxis renders at ``ceiling=all``.

Spelled as the whole row rather than as "the canary appears somewhere" because
the two halves of this gate fail differently. ``section_hyperedges`` renders
``f"- {title} — spans: {', '.join(sorted(spanning))}"``, and the sort puts
``Eddy-C...`` before ``Eddy-S...``, so this string is exact rather than
order-tolerant. Matching it whole is what distinguishes "the row came back at a
broad ceiling" from "the praxis title appears in the document".
"""

_SPAN_CONTROL_ROW = (
    f"- {_SPAN_CONTROL_TITLE} — spans: "
    f"{_SPAN_EDDY_CONTROL_TITLES[0]}, {_SPAN_EDDY_CONTROL_TITLES[1]}"
)
"""The control praxis's row, which must render identically at *every* ceiling."""


def _build_hyperedge_span_vault(root: Path) -> Path:
    """Build a vault where one hyperedge row spans an above-ceiling eddy.

    Bespoke rather than folded into :func:`_build_canary_vault` because the
    shape it needs is the one that fixture deliberately avoids. Its hyperedge
    comment records the choice: the two praxis rows get *disjoint* eddy pairs
    so that no eddy ends up with one ``intimate`` and one ``open`` member.
    A mixed-membership eddy is exactly the leak here, so reproducing it inside
    the shared vault would change what every existing ``_SECTIONS`` row and
    both ``## Hyperedges`` mutation rows assert.

    The layout, and why each piece is load-bearing:

    * ``frag-span-open-a`` (``open``) and ``frag-span-intimate-b``
      (``intimate``) both name :data:`_SPAN_EDDY_ABOVE_TITLE`. The eddy's
      derived tier is therefore ``intimate`` and ``## Active eddies`` drops it
      at ``ceiling=open`` — but ``frag-span-open-a`` is *admitted*, and it is
      the admitted fragment that carries the title into the span set.
    * ``frag-span-open-c`` (``open``) names :data:`_SPAN_EDDY_PLAIN_TITLE`, so
      the bridge praxis reaches two eddies and clears the ``len(spanning) >= 2``
      cutoff at ``ceiling=all``. Once the excluded eddy is removed from the set
      it drops to one, which is why the correct behaviour at ``ceiling=open`` is
      that the row disappears entirely rather than reappearing with one span.
    * ``frag-span-open-d`` (``open``) names both control eddies, giving the
      section a row that must survive at every ceiling.

    Args:
        root: Directory the vault is created inside (typically ``tmp_path``).

    Returns:
        The vault root.
    """
    vault = root / "hyperedge-span-vault"
    _seed_dirs(vault)
    for title in (
        _SPAN_EDDY_ABOVE_TITLE,
        _SPAN_EDDY_PLAIN_TITLE,
        *_SPAN_EDDY_CONTROL_TITLES,
    ):
        _write_eddy(vault, title=title)
    _write_fragment(
        vault,
        frag_id="frag-span-open-a",
        privacy_tier="open",
        eddies=[f"[[{_SPAN_EDDY_ABOVE_TITLE}]]"],
    )
    _write_fragment(
        vault,
        frag_id="frag-span-intimate-b",
        privacy_tier="intimate",
        eddies=[f"[[{_SPAN_EDDY_ABOVE_TITLE}]]"],
    )
    _write_fragment(
        vault,
        frag_id="frag-span-open-c",
        privacy_tier="open",
        eddies=[f"[[{_SPAN_EDDY_PLAIN_TITLE}]]"],
    )
    _write_fragment(
        vault,
        frag_id="frag-span-open-d",
        privacy_tier="open",
        eddies=[f"[[{title}]]" for title in _SPAN_EDDY_CONTROL_TITLES],
    )
    _write_praxis(
        vault,
        praxis_id="praxis-span-bridge",
        title=_SPAN_BRIDGE_TITLE,
        derived_from=["frag-span-open-a", "frag-span-open-c"],
    )
    _write_praxis(
        vault,
        praxis_id="praxis-span-control",
        title=_SPAN_CONTROL_TITLE,
        derived_from=["frag-span-open-d"],
    )
    return vault


@pytest.fixture
def hyperedge_span_vault(tmp_path: Path) -> Path:
    """Return the mixed-tier-eddy vault built by :func:`_build_hyperedge_span_vault`.

    Function-scoped for the same reason :func:`canary_vault` is: the two tests
    below render at different ceilings into the single ``latest.md`` slot, so a
    shared instance would let one test's render decide the other's read.
    """
    return _build_hyperedge_span_vault(tmp_path)


def test_hyperedges_do_not_span_an_above_ceiling_eddy_title(
    hyperedge_span_vault: Path,
) -> None:
    """An excluded eddy's title must not ride into a ``spans:`` list (#969 review).

    ``StateReportGenerator._fragment_to_eddies`` maps each **admitted**
    fragment to every eddy its ``eddies:`` wikilinks name, and
    ``section_hyperedges`` renders those targets verbatim without intersecting
    them against ``self._state.eddies`` — the ceiling-admitted eddy list. An
    eddy whose membership is one ``open`` fragment and one ``intimate``
    fragment derives to ``intimate``, is correctly dropped from
    ``## Active eddies``, and then has its title printed anyway by a hyperedge
    row belonging to an admitted praxis. At ``ceiling=open`` the report reads:

        - Praxis ... bridge — spans: Eddy-CANARY-SPANEDDY-INTIMATE-4e6b, ...

    Asserted on the artifact bytes rather than on the envelope, for the reason
    the module docstring gives: ``creek.state.render`` is a write-side surface
    and the artifact is what every later reader of the vault sees.

    Two positive controls sit underneath, and both are needed:

    * the **row must be gone entirely**, not merely censored. Once the excluded
      eddy leaves the span set the bridge praxis spans one admitted eddy, which
      is below ``section_hyperedges``'s own ``len(spanning) >= 2`` definition of
      a hyperedge. A row that stayed and printed ``spans: Eddy-Span-Plain``
      would still disclose, by contradiction with that definition, that a second
      eddy exists which the caller may not see.
    * the **control row must still render**, or "no leak" and "no section" are
      indistinguishable and a fix that dropped ``## Hyperedges`` whole — the way
      ``## Lint summary`` is legitimately dropped — would pass.

    Args:
        hyperedge_span_vault: The mixed-tier-eddy vault.
    """
    result = state_render_tool(
        vault_path=hyperedge_span_vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    text = _artifact_text(hyperedge_span_vault)
    for header in SECTION_ORDER:
        assert header in text, (
            f"the rendered report is missing {header!r} at ceiling=open, so "
            "every assertion below it is vacuous"
        )

    assert _SPAN_EDDY_ABOVE not in text, (
        f"the above-ceiling sentinel {_SPAN_EDDY_ABOVE!r} reached "
        "00-Creek-Meta/State at privacy_tier_ceiling=open.\n\n"
        "Sentinel carrier: the title of an eddy with one open and one "
        "intimate member fragment, so its derived tier is intimate.\n\n"
        "## Active eddies already excludes it — the only remaining way into "
        "the artifact is the 'spans:' list of a ## Hyperedges row, which is "
        "built from every eddy an admitted fragment names rather than from "
        f"the admitted eddies.\n\n{text}"
    )

    hyperedges = _section_body(_report_body(hyperedge_span_vault), "## Hyperedges")
    assert hyperedges, "the report is missing its '## Hyperedges' section entirely"
    assert _SPAN_CONTROL_ROW in hyperedges, (
        "## Hyperedges dropped the row whose two spanned eddies are both "
        f"admitted at privacy_tier_ceiling=open. Expected {_SPAN_CONTROL_ROW!r} "
        "in:\n\n"
        f"{hyperedges}\n\n"
        "Without this row the exclusion assertion above is satisfied by an "
        "empty section rather than by a gate."
    )
    assert _SPAN_PRAXIS_BRIDGE not in hyperedges, (
        "## Hyperedges still renders a row for the bridging praxis at "
        "privacy_tier_ceiling=open. With the above-ceiling eddy removed from "
        "its span set the praxis reaches one admitted eddy, which is below "
        "the section's own 'spans 2+ eddies' definition, so the row must "
        f"disappear rather than render with a single span:\n\n{hyperedges}"
    )


def test_hyperedges_span_the_full_eddy_pair_at_the_all_ceiling(
    hyperedge_span_vault: Path,
) -> None:
    """At ``ceiling=all`` the bridging row returns, with **both** eddies named.

    The permissive half of the pair above, and it has to be this exact — a gate
    that dropped every praxis whose spans touched a mixed-tier eddy, at every
    ceiling, would be leak-free and would also silently delete a true hyperedge
    from the vault owner's own report. Matching :data:`_SPAN_BRIDGE_ROW` whole
    pins the row's rendering (title, em-dash separator, sorted span list) rather
    than merely asserting the sentinel is somewhere in the document, where
    ``## Active eddies`` would supply it for free at this ceiling.

    Args:
        hyperedge_span_vault: The mixed-tier-eddy vault.
    """
    result = state_render_tool(
        vault_path=hyperedge_span_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok"
    hyperedges = _section_body(_report_body(hyperedge_span_vault), "## Hyperedges")
    assert _SPAN_BRIDGE_ROW in hyperedges, (
        "## Hyperedges must render the bridging praxis with both spanned "
        f"eddies at privacy_tier_ceiling=all. Expected {_SPAN_BRIDGE_ROW!r} "
        f"in:\n\n{hyperedges}\n\n"
        "A gate that removed the row at every ceiling would satisfy the "
        "open-ceiling test above by deleting a genuine hyperedge from the "
        "vault owner's report."
    )
    assert _SPAN_CONTROL_ROW in hyperedges, (
        "## Hyperedges dropped the all-admitted control row at "
        f"privacy_tier_ceiling=all:\n\n{hyperedges}"
    )


# --- Blocker 2: an above-ceiling thread title riding into the seed miner ----

_MINED_THREAD_ABOVE = "CANARY-MINEDTHREAD-INTIMATE-7c3d"
"""Rides in the ``title`` of a thread whose every member fragment is ``intimate``.

``creek.generate.mining._load_mining_snapshot`` loads ``02-Threads`` and
``03-Eddies`` through ``_load_typed``, which takes **no** privacy override —
only the fragment and liminal loaders are gated. ``mine_thread_terminus``
therefore emits ``IdeaSeed(title=thread.title, ...)`` for a thread none of whose
members the caller may see, and ``## Suggested questions`` prints that title.
"""

_MINED_THREAD_BELOW = "CANARY-MINEDTHREAD-OPEN-2f81"
"""Rides in the title of an otherwise identical thread whose members are ``open``.

The positive control. ``## Suggested questions`` is *already* dropped whole
below ``ceiling=personal``, so an exclusion assertion at ``personal`` with no
admitted seed to point at would be indistinguishable from the fix narrowing the
mining snapshot's threads to the empty tuple.
"""

_MINED_THREAD_ABOVE_TITLE = f"Thread-{_MINED_THREAD_ABOVE}-Secret"
"""Title and file stem of the above-ceiling thread."""

_MINED_THREAD_BELOW_TITLE = f"Thread-{_MINED_THREAD_BELOW}-Public"
"""Title and file stem of the admitted control thread."""

_MINED_THREAD_FRAGMENT_COUNT = 12
"""Members per thread — strictly above ``DEFAULT_MIN_THREAD_FRAGMENTS`` (10).

``_mine_thread_terminus_with_diagnostic`` keeps a candidate only when
``thread.fragment_count > self.min_thread_fragments``, so a fixture at or below
the threshold would emit no seed at all and turn every assertion below into a
comparison between two empty sections. The test asserts the relation rather
than trusting this comment.
"""


def _write_mined_thread(
    vault: Path,
    *,
    thread_id: str,
    title: str,
    fragment_count: int,
) -> Path:
    """Write an ``active`` thread over the mining threshold.

    Separate from :func:`_write_thread`, which hardcodes ``fragment_count: 1``
    — one member is below ``DEFAULT_MIN_THREAD_FRAGMENTS`` and produces no
    thread-terminus seed, so the existing helper cannot express this fixture.

    Args:
        vault: Vault root.
        thread_id: Thread id and, deliberately, **not** a sentinel carrier.
            ``_seed_from_thread`` renders the id into the seed's
            ``brief_description`` (``"Thread '<id>' has accumulated ..."``), so
            a sentinel there would make the seed leak provable through a second
            field and blur which one the gate has to cover. The leak under test
            is the seed *title*.
        title: Thread title, also the file stem so the ``threads:`` wikilink on
            each member fragment resolves under either resolution — the same
            reasoning :func:`_write_thread` records.
        fragment_count: The stored count the terminus threshold is measured
            against. It is *stored*, not recounted, so the member fragments
            below exist to give the thread a derived tier rather than to make
            this number true.

    Returns:
        The path written.
    """
    metadata: dict[str, Any] = {
        "type": "thread",
        "id": thread_id,
        "title": title,
        "status": "active",
        "first_seen": "2026-01-01",
        "last_seen": "2026-06-01",
        "fragment_count": fragment_count,
    }
    return _write_note(vault, f"02-Threads/Active/{title}.md", metadata, "")


def _build_mined_thread_vault(root: Path) -> Path:
    """Build a vault with one above-ceiling and one admitted terminus thread.

    One vault rather than two, for :func:`_build_canary_vault`'s stated reason:
    the sections share a corpus in production, and separate vaults could not
    catch the miner admitting a thread that ``## Active threads`` had already
    excluded from the very same render.

    Both threads are ``active``, carry the same ``fragment_count``, and have the
    same number of member fragments; the *only* difference between them is the
    ``privacy_tier`` on those members. That is what makes the pair a controlled
    comparison rather than two fixtures that happen to differ.

    Every fragment declares ``phase: unclassified`` (via :func:`_write_fragment`),
    so ``current_phase_summary`` resolves ``unclassified``,
    ``_PHASE_AWARE_STRATEGIES`` has no entry for it, and
    ``phase_filtered_seeds`` applies no strategy filter — every seed competes on
    score. A thread-terminus seed scores ``fragment_count / min_thread_fragments``
    (``12 / 10 = 1.2``), above the ``1 / (1 + count) <= 1.0`` ceiling of the
    unexplored-ontology strategy, so both thread seeds rank inside the
    five-prompt cap. The section's content is therefore determined by the
    fixture rather than by a tie-break.

    Args:
        root: Directory the vault is created inside (typically ``tmp_path``).

    Returns:
        The vault root.
    """
    vault = root / "mined-thread-vault"
    _seed_dirs(vault)
    _write_mined_thread(
        vault,
        thread_id="thread-mined-above",
        title=_MINED_THREAD_ABOVE_TITLE,
        fragment_count=_MINED_THREAD_FRAGMENT_COUNT,
    )
    _write_mined_thread(
        vault,
        thread_id="thread-mined-below",
        title=_MINED_THREAD_BELOW_TITLE,
        fragment_count=_MINED_THREAD_FRAGMENT_COUNT,
    )
    for index in range(_MINED_THREAD_FRAGMENT_COUNT):
        _write_fragment(
            vault,
            frag_id=f"frag-mined-intimate-{index:02d}",
            privacy_tier="intimate",
            threads=[f"[[{_MINED_THREAD_ABOVE_TITLE}]]"],
        )
        _write_fragment(
            vault,
            frag_id=f"frag-mined-open-{index:02d}",
            privacy_tier="open",
            threads=[f"[[{_MINED_THREAD_BELOW_TITLE}]]"],
        )
    return vault


@pytest.fixture
def mined_thread_vault(tmp_path: Path) -> Path:
    """Return the terminus-thread vault built by :func:`_build_mined_thread_vault`.

    Function-scoped for :func:`canary_vault`'s reason: ``latest.md`` is one slot
    and the two tests below render at different ceilings into it.
    """
    return _build_mined_thread_vault(tmp_path)


def test_suggested_questions_omit_an_above_ceiling_thread_title(
    mined_thread_vault: Path,
) -> None:
    """A thread whose every member is above the ceiling must not seed a prompt.

    ``_load_mining_snapshot`` gates its fragment and liminal loaders with the
    privacy override and then loads ``02-Threads`` / ``03-Eddies`` through
    ``_load_typed``, which accepts no override at all. So even though
    ``## Active threads`` correctly drops a thread whose twelve members are all
    ``intimate`` — its derived tier is ``intimate`` — ``mine_thread_terminus``
    still sees the thread record, and ``## Suggested questions`` renders:

        - Thread-CANARY-... — Thread 't...' has accumulated 12 fragments
          without becoming a published essay. ...

    ``ceiling=personal`` is the ceiling that exposes it: the section is dropped
    whole below ``personal`` (see
    :func:`test_suggested_questions_are_dropped_at_the_open_ceiling`), so
    ``open`` cannot see this and ``intimate``/``all`` legitimately admit it.

    The control thread is asserted **inside the section body**, not in the
    artifact bytes, and that distinction is load-bearing: its title also appears
    in ``## Active threads`` at this ceiling, so a document-wide check would be
    satisfied by that section alone and would not notice the miner going silent.

    Args:
        mined_thread_vault: The paired terminus-thread vault.
    """
    from creek.generate.mining import DEFAULT_MIN_THREAD_FRAGMENTS

    assert _MINED_THREAD_FRAGMENT_COUNT > DEFAULT_MIN_THREAD_FRAGMENTS, (
        "the fixture's threads no longer clear the terminus threshold "
        f"({_MINED_THREAD_FRAGMENT_COUNT} vs "
        f"{DEFAULT_MIN_THREAD_FRAGMENTS}), so no thread-terminus seed is "
        "produced at any ceiling and every assertion below is vacuous"
    )

    result = state_render_tool(
        vault_path=mined_thread_vault,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    text = _artifact_text(mined_thread_vault)
    for header in SECTION_ORDER:
        assert header in text, (
            f"the rendered report is missing {header!r} at ceiling=personal, "
            "so every assertion below it is vacuous"
        )

    assert _MINED_THREAD_ABOVE not in text, (
        f"the above-ceiling sentinel {_MINED_THREAD_ABOVE!r} reached "
        "00-Creek-Meta/State at privacy_tier_ceiling=personal.\n\n"
        "Sentinel carrier: the title of an active thread whose twelve member "
        "fragments are all intimate.\n\n"
        "## Active threads already excludes it on its derived tier — the "
        "remaining way in is ## Suggested questions, whose miner loads "
        "02-Threads through _load_typed, a loader that takes no privacy "
        f"override.\n\n{text}"
    )

    section = _section_body(
        _report_body(mined_thread_vault),
        "## Suggested questions",
    )
    assert section, "the report is missing its '## Suggested questions' section"
    assert EMPTY_PLACEHOLDER not in section, (
        "## Suggested questions rendered the empty placeholder at "
        "privacy_tier_ceiling=personal over a corpus with twelve admitted "
        "fragments and an admitted terminus thread in it, so the exclusion "
        f"assertion above is satisfied by an outage:\n\n{section}"
    )
    assert _MINED_THREAD_BELOW in section, (
        f"## Suggested questions dropped {_MINED_THREAD_BELOW!r} at "
        "privacy_tier_ceiling=personal. That thread is identical to the "
        "excluded one in status and fragment_count and differs only in its "
        "members' privacy_tier, so narrowing the mining snapshot must narrow "
        f"it to the *admitted* threads, not to none:\n\n{section}"
    )


def test_suggested_questions_admit_the_intimate_thread_at_the_all_ceiling(
    mined_thread_vault: Path,
) -> None:
    """At ``ceiling=all`` the intimate thread seeds a prompt again.

    The permissive control for the narrowing above. A fix that handed the miner
    an empty thread tuple, or dropped the thread-terminus strategy from the
    report altogether, would be leak-free at ``personal`` and would also delete
    the vault owner's most useful prompt from their own report at every ceiling.

    Asserted on the section body rather than the artifact for the same reason
    its companion is: at this ceiling ``## Active threads`` renders the title
    too, so a document-wide check would prove nothing about the miner.

    Args:
        mined_thread_vault: The paired terminus-thread vault.
    """
    result = state_render_tool(
        vault_path=mined_thread_vault,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok"
    section = _section_body(
        _report_body(mined_thread_vault),
        "## Suggested questions",
    )
    assert section, "the report is missing its '## Suggested questions' section"
    assert _MINED_THREAD_ABOVE in section, (
        f"## Suggested questions dropped {_MINED_THREAD_ABOVE!r} at "
        "privacy_tier_ceiling=all, which admits intimate content. The "
        "permissive direction has to be pinned as hard as the restrictive "
        "one: a miner that surfaces nothing is leak-free and useless:\n\n"
        f"{section}"
    )
    assert _MINED_THREAD_BELOW in section, (
        f"## Suggested questions dropped {_MINED_THREAD_BELOW!r} at "
        f"privacy_tier_ceiling=all:\n\n{section}"
    )


# --- Blocker 3: a stamp that fails to parse for a non-YAML reason -----------

_JSON_FRONTMATTER_LATEST = (
    '{\n"privacy_tier": "open",\n}\n\n# Creek state\n\nVault summary lives here.\n'
)
"""A ``latest.md`` whose frontmatter routes to JSON and then fails to parse.

``frontmatter.loads`` picks a handler by asking each one to ``detect`` the
text, and ``JSONHandler.FM_BOUNDARY`` is ``^(?:{|})$`` — so a document whose
first line is exactly ``{`` is handed to ``json.loads``. The trailing comma
makes that raise :class:`json.JSONDecodeError`, which is a
:class:`ValueError` and **not** a :class:`yaml.YAMLError`.
:func:`creek.generate.state_tiers.stamped_content_tier` catches only
``yaml.YAMLError``, so at HEAD the exception escapes ``state_read_tool``
uncaught.

A vault note is operator-editable and an artifact is hand-editable, so this is
a real shape rather than a contrived one — and the failure mode is the worst
available: not a leak, but a crash on the read path that was supposed to be the
gate. Fail-closed and refuse; do not raise.
"""


def test_state_read_fails_closed_on_a_non_yaml_frontmatter_error(
    tmp_path: Path,
) -> None:
    """A stamp that raises a non-YAML parse error must refuse, not crash.

    Written beside :func:`test_unreadable_stamp_values_fail_closed` rather than
    as a row of it because that parametrisation varies the ``privacy_tier``
    *value* underneath a well-formed YAML stamp written by
    :func:`_stamped_latest`; this failure is about the document's *format*, and
    ``_stamped_latest`` cannot express it.

    ``stamped_content_tier`` is pinned directly as well as through the tool.
    Going through ``state_read_tool`` alone would leave the reduction untested
    for every other caller of the helper, and pinning the helper alone would not
    show that the tool's ``report_path.read_text`` → ``stamped_content_tier``
    sequence has no other unguarded step in it.

    The refusal is compared for equality against the canonical payload, not
    merely checked for ``status == "refused"``: a distinguishable
    "this artifact would not parse" refusal would be an oracle for the vault's
    contents in the same way a tier-naming refusal is, which is the property
    :func:`test_refusal_is_byte_identical_for_every_above_ceiling_stamp`
    already protects for the tier axis.

    The non-vacuity control for "the reader has not simply started refusing
    everything" is the ``open`` row of
    :func:`test_unreadable_stamp_values_fail_closed`, which asserts a readable
    ``open`` stamp is still served.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    from creek.generate.state_tiers import stamped_content_tier

    assert stamped_content_tier(_JSON_FRONTMATTER_LATEST) == PrivacyTier.INTIMATE, (
        "stamped_content_tier must fail closed on frontmatter it cannot "
        "parse, whatever the parser. frontmatter.loads dispatches on the "
        "document's own opening delimiter, and only the YAML handler raises "
        "yaml.YAMLError — a document opening with a bare '{' is routed to "
        "json.loads, which raises json.JSONDecodeError."
    )

    vault = tmp_path / "non-yaml-stamp"
    _seed_dirs(vault)
    _write_latest(vault, _JSON_FRONTMATTER_LATEST)

    response = state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert response["status"] == "refused", (
        "creek.state.read served a latest.md whose stamp cannot be parsed at "
        "privacy_tier_ceiling=open. A stamp nobody can read is a stamp nobody "
        "has vouched for. (At HEAD this line is not reached at all: the "
        "JSONDecodeError escapes state_read_tool and the test errors rather "
        f"than fails.) Got: {response!r}"
    )
    assert response["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert response == refusal_response(
        tool="creek.state.read",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    ), (
        "the refusal for an unparsable-frontmatter artifact differs from the "
        "canonical payload, so the refusal itself distinguishes 'this would "
        "not parse' from 'this is above your ceiling' — an oracle over the "
        f"artifact the caller was not admitted to. Got: {response!r}"
    )


# ---------------------------------------------------------------------------
# A ``creek save``-written paradox note must carry a real tier (issue #1491)
#
# The detector-written counterpart — ``type: paradox`` with no ``privacy_tier``
# key at all — is covered by ``_write_liminal_paradox`` and the ``paradox`` row
# of ``_SECTIONS``. Both are left exactly as they were: they pin the
# *fail-closed* path, where the reader excludes a note because nobody vouched
# for it. What follows is the path that path cannot cover, because a save does
# write a tier key and the reader is right to believe it.
# ---------------------------------------------------------------------------

# The title is written into the vault note in the clear by
# ``_title_only_summary`` and slugified into the filename. So every test below
# passes an explicit title that shares no substring with the canary, and keeps
# the canary off line 1. Without that, the correct fix still leaves the canary
# in the vault note and the whole battery reads as "the fix does not work".
#
# The standing instruction survives #1505 unchanged, and the reason is worth
# keeping straight. #1505 stopped an *untitled* save from deriving its title
# from the body's first line above ``open`` (``writer._fallback_title``); at
# ``open`` it still derives, and an *operator-supplied* title is still written
# verbatim at every tier, which is exactly what these tests supply. So the
# title remains a cleartext surface here by design, not by defect.
_PARADOX_TITLE = "Both true at once"
_PARADOX_SECRET = "CLEARTEXT-CANARY-1491"
_PARADOX_BODY = (
    "Two framings collide.\n\n"
    + _PARADOX_SECRET
    + " is the part that must never land in the vault."
)

_PARADOX_CONTROL_TITLE = "Two readings of one morning"
"""Title of the ``open``-tier positive control paradox note.

Shares no substring with :data:`_PARADOX_TITLE`, so the two saves slugify to
distinct filenames and neither can be mistaken for the other in a stem list.
"""

_PARADOX_CONTROL_BODY = "A contradiction the operator is happy to publish."
"""Body of the control note — carries no canary, because it is meant to be read."""


def test_save_written_intimate_paradox_is_excluded_below_intimate(
    tmp_path: Path,
) -> None:
    """A saved intimate paradox must not reach an open-ceiling consumer (#1491).

    :func:`_write_liminal_paradox` above covers the *detector*-written paradox
    note, which carries no ``privacy_tier`` key at all;
    :func:`~creek.generate.state._admitted_liminal_notes` excludes it below
    ``intimate`` by failing closed on the missing key. That fixture is
    deliberately untouched — it pins a different note shape excluded for a
    different reason.

    This is the ``creek save``-written counterpart, and it is the shape the
    fail-closed read cannot rescue. A save *does* write a ``privacy_tier``
    key, so the reader believes it — and at HEAD
    :func:`~creek.save.writer.save_to_vault` writes ``open`` onto every
    paradox note whatever tier the operator stated. The key is present, it is
    wrong, and an intimate-derived contradiction therefore rides into a state
    report rendered at ``--include-tier open``. Fail-closed reading protects
    against silence, not against a confident lie.

    Today the note is admitted at all four overrides. After the fix it is
    admitted at ``intimate`` and ``all`` only.

    The ``open`` control is what stops the exclusion assertions passing for
    the wrong reason. A reader that returned nothing at all — a missing
    folder, a glob that stopped matching, an exception swallowed by
    ``_safe_post`` — would satisfy "the intimate stem is absent" at every
    override while being an outage rather than a gate, so the control is
    asserted *present* at all four.

    The note's own bytes are checked first. If the body is in the clear inside
    the paradox note, no reader-side ceiling anywhere downstream can contain
    it, and the admission result below would be a detail rather than the
    finding.

    The save surface is imported inside the test, matching this module's
    convention for the #969-era surface: a change there fails this test rather
    than collapsing the whole file into one collection-time ``ImportError``.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    from creek.classify.privacy_filter import PrivacyTierOverride
    from creek.generate.state import _admitted_liminal_notes
    from creek.save import TARGET_SUBDIRS, SaveRequest, SaveTarget, save_to_vault

    vault = tmp_path / "save-written-paradox"
    stub_parts = ("10-Liminal", "Compost", "intimate-stubs")
    for parts in (*TARGET_SUBDIRS.values(), stub_parts):
        vault.joinpath(*parts).mkdir(parents=True, exist_ok=True)

    sealed = save_to_vault(
        SaveRequest(
            target=SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.INTIMATE,
            provenance=("frag-001",),
        ),
        vault_path=vault,
    )
    control = save_to_vault(
        SaveRequest(
            target=SaveTarget.PARADOX,
            body=_PARADOX_CONTROL_BODY,
            title=_PARADOX_CONTROL_TITLE,
            tier=PrivacyTier.OPEN,
            provenance=("frag-002",),
        ),
        vault_path=vault,
    )

    assert _PARADOX_SECRET not in sealed.read_text(encoding="utf-8"), (
        "the intimate body is in the clear inside the paradox note itself, so "
        "no reader-side ceiling downstream can contain it"
    )

    folder = vault / "10-Liminal" / "Paradoxes"
    for override in (PrivacyTierOverride.OPEN, PrivacyTierOverride.PERSONAL):
        stems = [stem for stem, _tier in _admitted_liminal_notes(folder, override)]
        assert sealed.stem not in stems, (
            "a save-written intimate paradox note was admitted at "
            f"--include-tier {override.value}: {stems}"
        )
        assert control.stem in stems, (
            f"the open control vanished at --include-tier {override.value} "
            f"too — the reader is returning nothing, not filtering: {stems}"
        )
    for override in (PrivacyTierOverride.INTIMATE, PrivacyTierOverride.ALL):
        stems = [stem for stem, _tier in _admitted_liminal_notes(folder, override)]
        assert sealed.stem in stems, (
            "the intimate paradox note was withheld from an entitled "
            f"--include-tier {override.value} consumer: {stems}"
        )
        assert control.stem in stems
