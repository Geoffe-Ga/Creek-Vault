"""``creek state`` audit-report generator (FEAT-006).

The :class:`StateReportGenerator` re-reads existing vault state and
renders a single markdown audit report — the document an agent or human
opens to understand the vault. The report is organised into seven
structural sections:

1. Vault summary
2. Pre-LLM yield
3. Active eddies
4. Active threads
5. Surprising connections
6. Hyperedges
7. Drift warnings

This module is intentionally a *view* over the compiled layer — it
never re-runs classification, linking, or compile passes. It only
reads what is already on disk.

FEAT-007 inserts a wavelength snapshot and a suggested-questions
section between sections 1 and 2; the section-ordering contract
pinned by tests here makes that insertion straightforward.

**Tier ceiling (#969).** The generator takes an
:class:`~creek.classify.privacy_filter.PrivacyTierOverride` and admits content
per section against it; :meth:`StateReportGenerator.write` then stamps the
artifact with the highest tier it actually admitted, which is what
``creek.state.read`` compares a caller's ceiling against. Two properties of
that design are easy to break and are stated here rather than only at the call
sites:

* The corpus is loaded **unfiltered first** and only then admitted. An eddy's
  tier is the maximum over *all* its member fragments, so filtering before
  deriving would resolve an eddy with one ``open`` and one ``intimate`` member
  to ``open`` and leak its title.
* The default is ``ALL``. ``creek state``'s operator is the vault owner, and a
  narrower default would silently delete most of an existing operator's report
  — a behaviour regression wearing a privacy fix's costume.

The type this module takes is ``PrivacyTierOverride``, never
``creek_mcp.tier_ceiling.TierCeiling``: ``creek`` may never import
``creek_mcp`` (the layering rule pinned at ``creek/ingest/journal_staging.py``
and ``creek/care/__init__.py``), so the MCP wrapper converts at its own
boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path  # plain stdlib import; no lazy benefit
from typing import TYPE_CHECKING, TypeVar

import frontmatter
import yaml
from pydantic import ValidationError

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    max_source_tier,
    raw_privacy_tier,
    tier_of,
    tier_within_override,
    within_ceiling,
)
from creek.clean.hygiene import BrokenLinkScanner, OrphanScanner
from creek.generate.indexes import FREQUENCY_NAMES
from creek.generate.mining import (
    IdeaMiner,
    IdeaSeed,
    MiningSnapshot,
    _load_mining_snapshot,
    phase_filtered_seeds,
)
from creek.generate.state_tiers import (
    derived_link_tiers,
    max_admitted_tier,
    stamp_report,
)
from creek.generate.wavelength import (
    DEFAULT_CURRENT_PHASE_WINDOW_DAYS,
    CurrentPhaseSummary,
    current_phase_summary,
)
from creek.hierarchy import select_by_policy
from creek.models import Eddy, Fragment, Frequency, Praxis, PrivacyTier, Thread

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)


_HEADER_WAVELENGTH: str = "## Wavelength snapshot"
_HEADER_VAULT_SUMMARY: str = "## Vault summary"
_HEADER_PRE_LLM_YIELD: str = "## Pre-LLM yield"
_HEADER_LIMINAL_WATCH: str = "## Liminal Watch"
_HEADER_ACTIVE_EDDIES: str = "## Active eddies"
_HEADER_ACTIVE_THREADS: str = "## Active threads"
_HEADER_SYNCHRONICITIES: str = "## Surprising connections"
_HEADER_HYPEREDGES: str = "## Hyperedges"
_HEADER_DRIFT_WARNINGS: str = "## Drift warnings"
_HEADER_SUGGESTED_QUESTIONS: str = "## Suggested questions"
_HEADER_LINT_SUMMARY: str = "## Lint summary"


SECTION_ORDER: tuple[str, ...] = (
    _HEADER_WAVELENGTH,
    _HEADER_VAULT_SUMMARY,
    _HEADER_PRE_LLM_YIELD,
    _HEADER_LIMINAL_WATCH,
    _HEADER_ACTIVE_EDDIES,
    _HEADER_ACTIVE_THREADS,
    _HEADER_SYNCHRONICITIES,
    _HEADER_HYPEREDGES,
    _HEADER_DRIFT_WARNINGS,
    _HEADER_SUGGESTED_QUESTIONS,
    _HEADER_LINT_SUMMARY,
)
"""Section headers in the order pinned by FEAT-006 and extended by FEAT-007.

FEAT-007 prepends ``## Wavelength snapshot`` (the audit report's
interpretive prime) and inserts ``## Liminal Watch`` between the
pre-LLM yield and the active-eddies section. ``## Suggested questions``
closes the FEAT-007 content; FEAT-008's ``## Lint summary`` is the
final appendix.
"""

_LIMINAL_WATCH_TOP_N: int = 5
"""Cap on the number of fresh liminal items rendered **per subfolder** — see
:meth:`StateReportGenerator.section_liminal_watch`."""

_LIMINAL_ROOT: str = "10-Liminal"
"""Vault subdirectory holding the liminal surfaces."""

_LIMINAL_SUBDIRS: tuple[str, ...] = ("Unnamed", "Paradoxes")
"""Subfolders under ``10-Liminal`` that the liminal watch surfaces.

``Synchronicities`` is excluded because it has its own section under
``## Surprising connections``.
"""

EMPTY_PLACEHOLDER: str = "_No surfacing this week._"
"""Body used when a section has no content to surface.

FEAT-006 acceptance criteria require an explicit empty-state note —
the section header must always render, never silently disappear.
"""

_TOP_N: int = 10
"""Cap on the number of items rendered in each list-style section."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
_THREADS_SUBDIR: str = "02-Threads"
_EDDIES_SUBDIR: str = "03-Eddies"
_PRAXIS_SUBDIR: str = "04-Praxis"
_SYNCHRONICITIES_SUBDIR: str = f"{_LIMINAL_ROOT}/Synchronicities"
_STATE_SUBPATH: tuple[str, str] = ("00-Creek-Meta", "State")
_YIELD_RELATIVE: tuple[str, str, str] = (
    "00-Creek-Meta",
    "Processing-Log",
    "run-summary.jsonl",
)


def _yield_log_path(vault_path: Path) -> Path:
    """Return the canonical path to ``run-summary.jsonl`` under *vault_path*."""
    return vault_path / _YIELD_RELATIVE[0] / _YIELD_RELATIVE[1] / _YIELD_RELATIVE[2]


_FREQUENCY_ORDER: dict[str, int] = {
    member.value: index for index, member in enumerate(Frequency)
}
"""Canonical sort position per :class:`Frequency` value (F1..F10 then UNCLASSIFIED)."""

_WIKILINK_PATTERN: re.Pattern[str] = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
"""Match an Obsidian wikilink like ``[[Target]]`` or ``[[Target|Alias]]``."""

_STALE_FRAGMENT_AGE_DAYS: int = 90
"""Age threshold (days) before an unreferenced fragment is reported as stale."""


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SynchronicityRow:
    """One synchronicity row read off disk for the surprising-connections section."""

    sync_id: str
    fragment_a_id: str
    fragment_b_id: str
    similarity: float
    time_gap_days: int


@dataclass(frozen=True)
class _TierIndex:
    """Everything the ceiling decided, resolved once at load time (#969).

    Lives on :class:`_VaultState` rather than on
    :class:`StateReportGenerator` for two reasons. The generator already
    carries exactly seven instance attributes, which is pylint's ``R0902``
    ceiling, and a snapshot recording the ceiling it was taken under is the
    semantically apt place for it anyway — the same claim the artifact stamp
    makes about the document.

    Attributes:
        override: The ceiling the snapshot was taken under.
        content_tiers: One tier per admitted item across every gated section
            resolved at load time — fragments, eddies, threads, praxis and
            liminal-watch notes. It is the base of the artifact stamp, not the
            whole of it: :meth:`StateReportGenerator._content_tier` adds the
            two contributions that are only knowable at render time, and
            states there why each one is needed. A section that admits content
            accounted for in neither place makes the stamp under-report and
            the read gate fail open.
        admitted_paths: ``str(path)`` of every admitted fragment file, in the
            exact form the hygiene scanners return, so the drift section can
            compare without re-normalising.
        liminal: ``{subfolder: (admitted file stem, ...)}`` in render order.
    """

    override: PrivacyTierOverride = PrivacyTierOverride.ALL
    content_tiers: tuple[PrivacyTier, ...] = ()
    admitted_paths: frozenset[str] = frozenset()
    liminal: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass
class _VaultState:
    """Snapshot of every collection :class:`StateReportGenerator` reads.

    Every model collection below is the **admitted** slice: what survived the
    ceiling, not what is on disk. The unfiltered corpus is deliberately not
    retained past :func:`_load_vault_state`, so no section can reach around
    the gate by accident.

    Attributes:
        fragments: Admitted Fragment models from ``01-Fragments``.
        threads: Admitted Thread models from ``02-Threads``.
        eddies: Admitted Eddy models from ``03-Eddies``.
        praxis: Admitted Praxis models from ``04-Praxis``.
        synchronicities: Synchronicity rows from ``10-Liminal/Synchronicities``.
            Not filtered here: a row names two fragment *ids*, and
            :meth:`StateReportGenerator._is_leaf` already answers ``False`` for
            an id absent from the admitted load, which fails the row closed.
        latest_yield: Most recent line of
            ``00-Creek-Meta/Processing-Log/run-summary.jsonl``, or
            ``None`` when the log is missing or empty.
        tiers: What the ceiling decided — see :class:`_TierIndex`.
        mining_corpus: Memoised narrowed snapshot backing
            ``## Suggested questions``, or ``None`` before it is first built.
            Loaded lazily rather than in :func:`_load_vault_state` because it
            is needed only at ``ceiling=personal`` or broader and costs a
            second vault walk. It lives here — not on
            :class:`StateReportGenerator`, which is at pylint's seven-attribute
            ``R0902`` limit — so both the section that renders it and the stamp
            that must account for it read one memoised value rather than two
            independently-built ones.
    """

    fragments: list[Fragment] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    eddies: list[Eddy] = field(default_factory=list)
    praxis: list[Praxis] = field(default_factory=list)
    synchronicities: list[_SynchronicityRow] = field(default_factory=list)
    latest_yield: dict[str, object] | None = None
    tiers: _TierIndex = field(default_factory=_TierIndex)
    mining_corpus: MiningSnapshot | None = None


@dataclass(frozen=True)
class _FragmentLoad:
    """The fragment corpus split by the ceiling, plus the tiers it implies.

    Attributes:
        admitted: Fragments the ceiling admits, in vault-walk order.
        tiers: Those fragments' tiers, positionally parallel to *admitted*.
        paths: ``str(path)`` of each admitted fragment file.
        eddy_tiers: ``{eddy title: [member tier, ...]}`` over the **whole**
            corpus, admitted or not — see :func:`derived_link_tiers`.
        thread_tiers: The same, keyed by thread title.
    """

    admitted: list[Fragment]
    tiers: list[PrivacyTier]
    paths: frozenset[str]
    eddy_tiers: dict[str, list[PrivacyTier]]
    thread_tiers: dict[str, list[PrivacyTier]]

    def tiers_by_id(self) -> dict[str, PrivacyTier]:
        """Return ``{fragment id: tier}`` for the admitted slice.

        Returns:
            A lookup the hyperedge gate uses to answer both of its questions at
            once — "did this ``derived_from`` id resolve to something admitted?"
            and "how sensitive was it?".
        """
        return {
            fragment.id: tier
            for fragment, tier in zip(self.admitted, self.tiers, strict=True)
        }


_ModelT = TypeVar("_ModelT", Fragment, Thread, Eddy, Praxis)
"""Model types :func:`_load_typed_models` can return.

Constrained rather than bound so each call site gets its concrete type back;
the previous union return forced an ``isinstance`` narrow at every caller that
mypy could not do for itself.
"""

_LinkedT = TypeVar("_LinkedT", Thread, Eddy)
"""Models whose tier is *derived* from the fragments naming their ``title``."""


def _safe_post(md_file: Path) -> frontmatter.Post | None:
    """Load a frontmatter post, returning ``None`` on parse errors."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown: %s", md_file)
        return None


def _load_typed_models(
    root: Path,
    *,
    type_tag: str,
    cls: type[_ModelT],
) -> list[_ModelT]:
    """Walk *root* and return every model whose ``type`` frontmatter matches.

    Files that fail YAML parsing or schema validation are skipped at
    DEBUG level rather than aborting the report — drift in a single
    fragment must not block the whole audit view.

    Args:
        root: Directory to walk recursively.
        type_tag: Required ``type`` frontmatter value.
        cls: Model class to validate each match against.

    Returns:
        Every validated model, in sorted on-disk order.
    """
    if not root.exists():
        return []
    collected: list[_ModelT] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != type_tag:
            continue
        try:
            collected.append(cls.model_validate(metadata))
        except ValidationError:
            logger.debug("Skipping invalid %s frontmatter: %s", type_tag, md_file)
            continue
    return collected


def _read_fragment_files(
    root: Path,
) -> list[tuple[Path, Fragment, dict[str, object]]]:
    """Load every ``type: fragment`` note under *root* with its raw frontmatter.

    Separate from :func:`_load_typed_models` because #969 needs three things
    the shared loader throws away: the file's path (the drift section reports
    orphans by path), and the *raw* metadata alongside the validated model.
    Raw is not a nicety — :class:`~creek.models.Fragment` defaults a **missing**
    ``privacy_tier`` to ``unclassified``, which ranks with ``personal`` and is
    admitted at ``ceiling=personal``, so reading the tier off the model alone
    fails open relative to what the file actually says.

    Args:
        root: The ``01-Fragments`` directory.

    Returns:
        ``(path, fragment, raw frontmatter)`` per valid fragment, in sorted
        on-disk order. Unreadable, non-fragment and schema-invalid files are
        skipped, and so are invisible to every gate below — which fails them
        closed, since a fragment nobody can load is a fragment nobody can vouch
        for.
    """
    if not root.exists():
        return []
    loaded: list[tuple[Path, Fragment, dict[str, object]]] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "fragment":
            continue
        try:
            fragment = Fragment.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
            continue
        loaded.append((md_file, fragment, metadata))
    return loaded


def _load_fragments_admitted(
    root: Path,
    override: PrivacyTierOverride,
) -> _FragmentLoad:
    """Load the fragment corpus, derive link tiers, then apply the ceiling.

    The ordering is the whole point and is not an implementation detail:
    ``eddy_tiers`` / ``thread_tiers`` reduce over **every** fragment, admitted
    or not, and only then is the corpus narrowed. Reversing the two would let
    an eddy whose members are one ``open`` and one ``intimate`` fragment
    resolve to ``open`` at ``ceiling=open`` and render its title, which is
    leak (3) of the three #969 reproduced.

    Args:
        root: The ``01-Fragments`` directory.
        override: The admission ceiling.

    Returns:
        A :class:`_FragmentLoad`.
    """
    loaded = _read_fragment_files(root)
    eddy_tiers = derived_link_tiers(
        (raw_privacy_tier(raw), _wikilink_targets(fragment.eddies))
        for _path, fragment, raw in loaded
    )
    thread_tiers = derived_link_tiers(
        (raw_privacy_tier(raw), _wikilink_targets(fragment.threads))
        for _path, fragment, raw in loaded
    )
    admitted = [entry for entry in loaded if within_ceiling(entry[2], override)]
    return _FragmentLoad(
        admitted=[fragment for _path, fragment, _raw in admitted],
        tiers=[raw_privacy_tier(raw) for _path, _fragment, raw in admitted],
        paths=frozenset(str(path) for path, _fragment, _raw in admitted),
        eddy_tiers=eddy_tiers,
        thread_tiers=thread_tiers,
    )


def _admit_by_derived_tier(
    models: list[_LinkedT],
    link_tiers: dict[str, list[PrivacyTier]],
    override: PrivacyTierOverride,
) -> tuple[list[_LinkedT], list[PrivacyTier]]:
    """Admit eddies / threads on the tier of the fragments that name them.

    Neither model carries a ``privacy_tier`` field, so the cutoff runs against
    a *derived* tier: the maximum over the tiers of every fragment whose
    ``eddies`` / ``threads`` wikilinks name this title.
    :func:`~creek.classify.privacy_filter.max_source_tier` supplies the empty
    case, ``INTIMATE`` — an eddy no fragment names has no tier evidence at all,
    and that includes the ``fragment_count`` fallback path in
    :meth:`StateReportGenerator._eddy_count`, whose stored count is a number
    rather than evidence.

    Args:
        models: Every eddy or thread loaded from disk.
        link_tiers: ``{title: [member tier, ...]}`` from
            :func:`_load_fragments_admitted`.
        override: The admission ceiling.

    Returns:
        ``(admitted models, their derived tiers)``, positionally parallel.
    """
    admitted: list[_LinkedT] = []
    tiers: list[PrivacyTier] = []
    for model in models:
        tier = max_source_tier(link_tiers.get(model.title, []))
        if tier_within_override(tier, override):
            admitted.append(model)
            tiers.append(tier)
    return admitted, tiers


def _admit_praxis(
    praxis: list[Praxis],
    fragment_tiers: dict[str, PrivacyTier],
) -> tuple[list[Praxis], list[PrivacyTier]]:
    """Admit a praxis only when *every* source fragment survived the ceiling.

    A hyperedge row renders the praxis title and the eddies its sources span,
    both of which are derived from those sources. Admitting on "some source
    resolved" would let a praxis whose evidence is mostly above the ceiling
    still describe itself, so the rule is all-or-nothing.

    An **empty** ``derived_from`` is excluded rather than admitted. It names no
    evidence, so there is nothing whose tier could vouch for the title — the
    same reasoning as :func:`_admit_by_derived_tier`'s empty case, reached one
    step earlier because ``ALL`` would otherwise admit the fail-closed
    ``INTIMATE`` that reduction produces.

    Args:
        praxis: Every praxis loaded from disk.
        fragment_tiers: ``{fragment id: tier}`` over the admitted fragments.

    Returns:
        ``(admitted praxis, their derived tiers)``, positionally parallel.
    """
    admitted: list[Praxis] = []
    tiers: list[PrivacyTier] = []
    for item in praxis:
        if not item.derived_from:
            continue
        if any(frag_id not in fragment_tiers for frag_id in item.derived_from):
            continue
        admitted.append(item)
        tiers.append(
            max_source_tier(fragment_tiers[frag_id] for frag_id in item.derived_from),
        )
    return admitted, tiers


def _admitted_liminal_notes(
    folder: Path,
    override: PrivacyTierOverride,
) -> list[tuple[str, PrivacyTier]]:
    """Return admitted ``(file stem, tier)`` pairs under one liminal subfolder.

    ``10-Liminal/Unnamed`` notes are fragments and carry ``privacy_tier``;
    ``10-Liminal/Paradoxes`` notes are ``type: paradox`` and have no tier field
    in their model at all. Reading the *raw* frontmatter through
    :func:`~creek.classify.privacy_filter.within_ceiling` is what lets one gate
    serve both: the paradox note fails closed to ``intimate`` because nobody
    vouched for it, not because it was classified.

    Sorting is unchanged from FEAT-007 and is applied before admission so the
    displayed order does not shift with the ceiling. ``st_mtime`` is
    best-effort — CI containers and fresh clones flatten it to the checkout
    time — so ``p.name`` keeps the order deterministic when the primary key
    ties.

    Args:
        folder: ``<vault>/10-Liminal/<subfolder>``.
        override: The admission ceiling.

    Returns:
        Admitted notes, newest first.
    """
    if not folder.exists():
        return []
    files = [p for p in folder.glob("*.md") if p.is_file()]
    files.sort(key=lambda p: (-p.stat().st_mtime, p.name))
    admitted: list[tuple[str, PrivacyTier]] = []
    for path in files:
        post = _safe_post(path)
        if post is None:
            continue
        raw = dict(post.metadata)
        if within_ceiling(raw, override):
            admitted.append((path.stem, raw_privacy_tier(raw)))
    return admitted


def _load_liminal_watch(
    vault_path: Path,
    override: PrivacyTierOverride,
) -> tuple[dict[str, tuple[str, ...]], list[PrivacyTier]]:
    """Collect the liminal watch's admitted stems and the tiers behind them.

    Args:
        vault_path: Root of the Obsidian vault.
        override: The admission ceiling.

    Returns:
        ``({subfolder: (stem, ...)}, [tier, ...])``. The tiers are flattened
        across subfolders because the stamp only ever reduces over them.
    """
    stems: dict[str, tuple[str, ...]] = {}
    tiers: list[PrivacyTier] = []
    for sub in _LIMINAL_SUBDIRS:
        entries = _admitted_liminal_notes(vault_path / _LIMINAL_ROOT / sub, override)
        stems[sub] = tuple(stem for stem, _tier in entries)
        tiers.extend(tier for _stem, tier in entries)
    return stems, tiers


def _load_synchronicities(root: Path) -> list[_SynchronicityRow]:
    """Read synchronicity notes from ``10-Liminal/Synchronicities``."""
    if not root.exists():
        return []
    rows: list[_SynchronicityRow] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        meta = post.metadata
        if meta.get("type") != "synchronicity":
            continue
        sync_id = meta.get("id")
        frag_a = meta.get("fragment_a_id")
        frag_b = meta.get("fragment_b_id")
        similarity = meta.get("similarity")
        time_gap = meta.get("time_gap_days")
        if not (
            isinstance(sync_id, str)
            and isinstance(frag_a, str)
            and isinstance(frag_b, str)
            and isinstance(similarity, (int, float))
            and isinstance(time_gap, int)
        ):
            logger.debug("Skipping malformed synchronicity: %s", md_file)
            continue
        rows.append(
            _SynchronicityRow(
                sync_id=sync_id,
                fragment_a_id=frag_a,
                fragment_b_id=frag_b,
                similarity=float(similarity),
                time_gap_days=int(time_gap),
            ),
        )
    return rows


def _load_latest_yield(log_path: Path) -> dict[str, object] | None:
    """Return the last non-empty line of the pre-LLM yield JSONL log.

    The whole file is read into memory because the report only needs the
    final line — operationally the log is a short one-line-per-run
    stream. Rotation is the writer's responsibility (see FEAT-005's
    ``write_yield_summary``); this reader does not cap the input size.
    """
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("Could not read run-summary log: %s", log_path)
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:  # noqa: TRY101  # Separate failure modes: file IO vs JSON parsing each return None with distinct debug logs.
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        logger.debug("Last run-summary line is not valid JSON")
        return None
    # JSON allows non-object roots (e.g. an array) — reject them so the
    # rest of the pipeline can rely on dict-shaped yields.
    return payload if isinstance(payload, dict) else None


def _load_vault_state(
    vault_path: Path,
    override: PrivacyTierOverride,
) -> _VaultState:
    """Snapshot every collection the report needs, already narrowed by *override*.

    Composition only: each collection's admission rule lives in the helper
    named beside it, so this function stays a readable statement of *what* the
    report is a view over and the interesting decisions stay individually
    testable.

    Args:
        vault_path: Root of the Obsidian vault.
        override: The admission ceiling. ``ALL`` admits everything, which is
            what "no ceiling declared" means for this module's callers.

    Returns:
        The admitted :class:`_VaultState`.
    """
    frags = _load_fragments_admitted(vault_path / _FRAGMENTS_SUBDIR, override)
    threads, thread_tiers = _admit_by_derived_tier(
        _load_typed_models(
            vault_path / _THREADS_SUBDIR,
            type_tag="thread",
            cls=Thread,
        ),
        frags.thread_tiers,
        override,
    )
    eddies, eddy_tiers = _admit_by_derived_tier(
        _load_typed_models(
            vault_path / _EDDIES_SUBDIR,
            type_tag="eddy",
            cls=Eddy,
        ),
        frags.eddy_tiers,
        override,
    )
    praxis, praxis_tiers = _admit_praxis(
        _load_typed_models(
            vault_path / _PRAXIS_SUBDIR,
            type_tag="praxis",
            cls=Praxis,
        ),
        frags.tiers_by_id(),
    )
    liminal, liminal_tiers = _load_liminal_watch(vault_path, override)
    return _VaultState(
        fragments=frags.admitted,
        threads=threads,
        eddies=eddies,
        praxis=praxis,
        synchronicities=_load_synchronicities(vault_path / _SYNCHRONICITIES_SUBDIR),
        latest_yield=_load_latest_yield(_yield_log_path(vault_path)),
        tiers=_TierIndex(
            override=override,
            content_tiers=(
                *frags.tiers,
                *thread_tiers,
                *eddy_tiers,
                *praxis_tiers,
                *liminal_tiers,
            ),
            admitted_paths=frags.paths,
            liminal=liminal,
        ),
    )


def _frequency_sort_key(freq_value: Frequency | str) -> tuple[int, str]:
    """Return a sort key that orders frequencies by their enum position.

    The canonical order on the report is the order in which the enum
    is declared (F1..F10, then UNCLASSIFIED). Unknown values sort after
    every known one, ordered alphabetically among themselves.
    """
    raw = freq_value.value if isinstance(freq_value, Frequency) else str(freq_value)
    position = _FREQUENCY_ORDER.get(raw, len(_FREQUENCY_ORDER))
    return (position, raw)


def _frequency_label(freq_value: Frequency | str) -> str:
    """Return a human-readable label for a frequency.

    Accepts either a :class:`Frequency` enum member directly or a string.
    Storing enum members at the call site (rather than ``str(member)``)
    avoids a fragile round-trip if :class:`Frequency` ever loses its
    :class:`enum.StrEnum` base.
    """
    if isinstance(freq_value, Frequency):
        freq = freq_value
    else:
        try:
            freq = Frequency(freq_value)
        except ValueError:
            return freq_value
    name = FREQUENCY_NAMES.get(freq)
    return f"{freq.value} ({name})" if name else freq.value


def _wikilink_targets(wikilinks: list[str]) -> list[str]:
    """Return the inner targets of ``[[...]]`` wikilink strings.

    Plain strings (no brackets) are returned unchanged so eddy fields
    stored without ``[[...]]`` syntax still match.
    """
    targets: list[str] = []
    for raw in wikilinks:
        match = _WIKILINK_PATTERN.search(raw)
        targets.append(match.group(1).strip() if match else raw.strip())
    return targets


def _section(header: str, body_lines: list[str]) -> str:
    """Render a section as ``header\\n\\n<body>``; placeholder when empty."""
    if not body_lines:
        return f"{header}\n\n{EMPTY_PLACEHOLDER}"
    return header + "\n\n" + "\n".join(body_lines)


def _level_annotation(level: str) -> str:
    """FEAT-025: render the "level used" note that opens hierarchy-aware sections.

    Italicised so it visually sits below the section header without
    competing with body content; the trailing period keeps the line a
    full sentence so downstream tools (state-budget, voice-proxy) don't
    treat it as a heading.
    """
    return f"_Counted at: {level}._"


def _vault_relative(path_str: str, vault_path: Path) -> str:
    """Render a scanner-reported absolute path relative to the vault root.

    ``OrphanScanner`` and ``BrokenLinkScanner`` both return ``str(path)`` for
    paths they built from the caller's ``vault_path``, so on a normal call that
    is an **absolute** path under the operator's home directory — which the
    drift section rendered verbatim until #969, contradicting
    ``docs/generation.md``'s standing claim that the artifact "does not leak an
    operator's home directory".

    This closes the drift section's half of that claim. The other half was
    ``## Lint summary``: ``creek/lint/checks/broken_links.py`` rendered the
    *same* scanner's absolute source into the Processing-Log artifact that
    :meth:`StateReportGenerator.section_lint_summary` appends verbatim, and was
    the only lint check not already using ``relative_to(vault_path)``. It does
    now, so the claim is true of the whole document rather than of one section.

    ``relative_to`` cannot raise here: every caller filters to the admitted
    fragment paths first, and those were built from ``vault_path /
    "01-Fragments"`` by the same lexical join the scanners use.

    Args:
        path_str: A scanner-reported path.
        vault_path: Root of the Obsidian vault.

    Returns:
        The path relative to *vault_path*.
    """
    return str(Path(path_str).relative_to(vault_path))


def _lint_summary_is_populated(sections: Iterable[str]) -> bool:
    """Whether the rendered ``## Lint summary`` section carries a report body.

    Computed as a pure function over the **rendered section strings** rather
    than by re-asking :func:`creek.lint.latest_lint_report`, so the artifact
    stamp cannot disagree with the document it stamps: there is no ordering
    hazard between the section methods and the stamp, and two consecutive
    renders provably stamp the same.

    Args:
        sections: The rendered sections, in :data:`SECTION_ORDER`.

    Returns:
        ``True`` when the lint section rendered something other than the
        empty-state placeholder — which means an untierable verbatim copy of a
        Processing-Log artifact is in the document and the stamp must escalate
        to ``intimate``.
    """
    placeholder = _section(_HEADER_LINT_SUMMARY, [])
    return any(
        section.startswith(_HEADER_LINT_SUMMARY) and section != placeholder
        for section in sections
    )


def _seed_as_prompt(seed: IdeaSeed) -> str:
    """Format a phase-filtered :class:`IdeaSeed` as one bullet of prompt text.

    The seed's title carries the essay-worthy framing; the brief
    description supplies the "why now" so the prompt is actionable
    without opening the linked compiled pages.
    """
    if seed.brief_description:
        return f"{seed.title} — {seed.brief_description}"
    return seed.title


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class StateReportGenerator:
    """Render the ``creek state`` audit report for an existing vault.

    The generator is a *view* — every method reads existing vault files
    and returns markdown. It never invokes the classification, linking,
    or compile passes.

    The tier ceiling (#969) is deliberately **not** a generator attribute: it
    is recorded on the loaded snapshot (``self._state.tiers``), which both
    keeps this class at pylint's seven-attribute ``R0902`` limit and states the
    truer thing — a snapshot is only meaningful together with the ceiling it
    was taken under.

    Attributes:
        vault_path: Root of the Obsidian vault.
        today: Date used to derive the ISO-week filename. Defaults to
            today (UTC) so the same week's report is overwritten on
            successive runs.
    """

    def __init__(
        self,
        vault_path: Path,
        *,
        today: date | None = None,
        current_phase: str | None = None,
        override: PrivacyTierOverride = PrivacyTierOverride.ALL,
    ) -> None:
        """Load the vault snapshot and stash *vault_path* for later use.

        Args:
            vault_path: Root of the Obsidian vault.
            today: Optional injected date for tests / week-pinning.
                Defaults to ``datetime.now(UTC).date()``.
            current_phase: Optional override for the dominant phase used
                by the suggested-questions filter. When ``None`` the
                generator derives the phase from the vault via
                :func:`current_phase_summary`. Tests use this hook to
                pin phase behaviour without fabricating fragments.
            override: Tier ceiling for every gated section (#969). Defaults
                to :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
                i.e. "no ceiling declared", so every pre-#969 caller — the CLI
                operator included, who *is* the vault owner — keeps today's
                output. That default is safe because both production callers
                state a ceiling explicitly: ``creek_mcp.tools.state`` converts
                the MCP ``privacy_tier_ceiling`` (itself defaulting to the
                strictest ``open``), and ``creek state --include-tier`` is
                parsed straight through.
        """
        self.vault_path = vault_path
        self.today = today or datetime.now(tz=UTC).date()
        self._state = _load_vault_state(vault_path, override)
        self._current_phase_override = current_phase
        self._wavelength_summary: CurrentPhaseSummary | None = None
        self._leaf_fragments: list[Fragment] | None = None
        self._leaf_ids: frozenset[str] | None = None

    # ---- Sections ---------------------------------------------------

    def section_wavelength_snapshot(self) -> str:
        """Render section 0: current wavelength phase, mode, dosage shares.

        FEAT-007 places this section first. The dominant phase context
        interprets every other section — Karpathy's ``index.md`` discipline
        adapted to Creek. The snapshot is descriptive only: no
        prescriptive ranking, no "should" language.

        FEAT-025: the snapshot reads at ``documents`` granularity —
        sentence- or paragraph-level wavelength readings would be noise
        next to whole-source phase signal.

        #969: the counts survey the *admitted* corpus. A per-tier count is
        content — ``creek.wheel`` is a frequency tally and is ``GATED`` for
        exactly this reason — so a report that omitted every above-ceiling
        title while printing ``Fragments observed: 100`` would still disclose
        the existence of what the rest of it was careful to leave out.

        Falls back to the empty-state placeholder when the vault has no
        admitted classified fragments in the 28-day window.
        """
        summary = self._resolve_wavelength_summary()
        if summary.fragment_count == 0:
            return _section(_HEADER_WAVELENGTH, [])
        body = [
            _level_annotation("documents"),
            "",
            f"- Phase: **{summary.phase}** (confidence {summary.confidence:.2f})",
            f"- Mode: **{summary.mode}**",
            f"- Fragments observed: {summary.fragment_count} "
            f"(last {DEFAULT_CURRENT_PHASE_WINDOW_DAYS} days)",
            f"- Medicine share: {summary.medicine_percent * 100:.1f}% | "
            f"Toxic share: {summary.toxic_percent * 100:.1f}%",
        ]
        if summary.transitions:
            body.extend(("", "**Recent transitions**"))
            for trans in summary.transitions:
                body.append(
                    f"- {trans.from_date.isoformat()} → {trans.to_date.isoformat()}: "
                    f"{trans.from_phase} → {trans.to_phase}",
                )
        return _section(_HEADER_WAVELENGTH, body)

    def section_liminal_watch(self) -> str:
        """Render the Liminal Watch section: fresh Unnamed + Paradox surfaces.

        Synchronicities live in ``## Surprising connections`` and are
        excluded here to avoid duplication. Items are sorted by file
        modification time (newest first), capped at
        :data:`_LIMINAL_WATCH_TOP_N` **per subfolder** (so a vault with
        both Unnamed and Paradoxes surfaces will list up to
        ``2 * _LIMINAL_WATCH_TOP_N`` rows total). The per-subfolder cap
        is intentional: a quiet week in one subfolder shouldn't starve
        the other.

        #969: the rendered name is a file *stem*, which for an ``Unnamed``
        note is the operator's own words. Admission is decided at load time by
        :func:`_admitted_liminal_notes`; the consequence worth stating is that
        a ``Paradoxes`` note carries no ``privacy_tier`` field in its model at
        all, so it fails closed and is dropped below ``ceiling=intimate``.
        """
        body: list[str] = []
        for sub in _LIMINAL_SUBDIRS:
            entries = self._state.tiers.liminal.get(sub, ())
            if not entries:
                continue
            body.append(f"**{sub}**")
            body.extend(f"- {name}" for name in entries[:_LIMINAL_WATCH_TOP_N])
            body.append("")
        # Drop the trailing blank line so _section's placeholder logic
        # treats a no-content result correctly.
        while body and not body[-1]:
            body.pop()
        return _section(_HEADER_LIMINAL_WATCH, body)

    def section_suggested_questions(self) -> str:
        """Render section: phase-aware suggested questions (FEAT-007).

        Pulls 4-5 prompts from
        :func:`creek.generate.mining.phase_filtered_seeds`. Down phases
        (Bottoming Out / Diminishing / Withdrawal) surface compost and
        synchronicity prompts; Rising and Peaking surface drafting and
        mining prompts; Restoration mixes both.

        #969 gates this in three ways, because the miner's own filtering is
        neither uniform across its corpus nor sufficient on the axes it does
        cover.

        1. **The whole section is dropped below ``ceiling=personal``.** The
           miner filters fragments through
           :func:`~creek.classify.privacy_filter.filter_fragments_by_tier`,
           which *summarises* a personal fragment as
           ``[Personal-tier summary: <title>]`` rather than excluding it — so
           at ``ceiling=open`` a personal **title** would enter the mining
           corpus and ride out inside a prompt.
        2. **The ceiling is handed to the miner**, so the fragment and liminal
           halves of its corpus walk are filtered at the source.
        3. **The corpus it walks is narrowed first** — see
           :meth:`_mining_corpus`. Threads and eddies are *not* gated by (2)
           at all: ``creek.generate.mining._load_typed`` takes no override, so
           ``mine_thread_terminus`` will title a seed with a thread whose every
           member fragment is above the ceiling.

        The miner is not constructed at all below that ceiling: it walks the
        vault and appends to ``compile-gaps.jsonl`` on the way, and an
        artifact written under a ceiling that dropped the section would be one
        more file to reason about for no benefit. :meth:`_mining_corpus` is
        guarded by the same predicate for the same reason.
        """
        if not self._seeds_admitted():
            return _section(_HEADER_SUGGESTED_QUESTIONS, [])
        seeds = phase_filtered_seeds(
            self.vault_path,
            self._resolve_current_phase(),
            # ``privacy_override`` here is inert, deliberately, and is kept as a
            # belt: ``IdeaMiner._resolve_snapshot`` consults it only when
            # ``snapshot is None``, and the snapshot below is never None. The
            # filtering that actually happens is :meth:`_mining_corpus`'s own
            # ``_load_mining_snapshot(privacy_override=...)`` plus the narrowing
            # it applies afterwards. Passing it anyway means a future edit that
            # drops the ``snapshot=`` argument degrades to the miner's own
            # ceiling rather than to no ceiling at all.
            miner=IdeaMiner(privacy_override=self._state.tiers.override),
            snapshot=self._mining_corpus(),
        )
        body = [f"- {_seed_as_prompt(seed)}" for seed in seeds]
        return _section(_HEADER_SUGGESTED_QUESTIONS, body)

    def _seeds_admitted(self) -> bool:
        """Whether ``## Suggested questions`` renders at all under the ceiling.

        Read by both the section and :meth:`_content_tier`, so the document
        and the stamp cannot disagree about whether a mining corpus was
        consulted.
        """
        return tier_within_override(
            PrivacyTier.PERSONAL,
            self._state.tiers.override,
        )

    def _mining_corpus(self) -> MiningSnapshot:
        """Return the mining snapshot, narrowed to what this report admitted.

        Only valid once :meth:`_seeds_admitted` answers ``True``; memoised on
        the snapshot so the section and the stamp share one corpus and one
        vault walk.

        **Threads and eddies are replaced outright** with
        ``self._state.threads`` / ``self._state.eddies``. The narrowing has to
        happen here rather than in :mod:`creek.generate.mining` because
        :class:`~creek.models.Thread` and :class:`~creek.models.Eddy` carry no
        ``privacy_tier`` field: a raw-frontmatter gate down there would have
        no evidence to read and would fail closed on every thread and eddy,
        silently deleting two of ``creek mine``'s strategies for every caller
        below ``ceiling=intimate``. That is a decision about a different tool
        with its own test suite. This generator already holds the *derived*
        tiers — the maximum over the fragments naming each title — so it can
        narrow precisely.

        **Fragments are intersected by id** with the admitted slice. The miner
        reads ``privacy_tier`` off the validated :class:`~creek.models.Fragment`,
        which defaults a *missing* key to ``unclassified`` and so admits at
        ``ceiling=personal`` a file this report's raw-frontmatter cutoff failed
        closed to ``intimate``. Without the intersection a wavelength-window
        seed could carry such a fragment's **title**. Note the intersection is
        by id and not by object: the miner's copy of an admitted fragment may
        carry a summarised body, which is stricter and is kept.

        **Liminal fragments are left as the miner filtered them**, and that is
        the one axis this report cannot narrow: they come from ``10-Liminal``
        subfolders (notably ``Compost``) that ``_load_liminal_watch`` does not
        walk, so there is no admitted list to intersect against. They are
        instead accounted for on the stamp — see :meth:`_content_tier`.

        Returns:
            The snapshot to hand :func:`phase_filtered_seeds`.
        """
        corpus = self._state.mining_corpus
        if corpus is None:
            loaded = _load_mining_snapshot(
                self.vault_path,
                privacy_override=self._state.tiers.override,
            )
            admitted_ids = {fragment.id for fragment in self._state.fragments}
            corpus = replace(
                loaded,
                fragments=tuple(
                    entry for entry in loaded.fragments if entry[0].id in admitted_ids
                ),
                threads=tuple(self._state.threads),
                eddies=tuple(self._state.eddies),
            )
            self._state.mining_corpus = corpus
        return corpus

    def _resolve_wavelength_summary(self) -> CurrentPhaseSummary:
        """Compute the wavelength summary once per generator instance.

        FEAT-025: reads at ``documents`` granularity so sentence- and
        paragraph-level wavelength readings don't drown out the phase
        signal a whole-source document carries.

        #969: the ceiling is threaded into
        :func:`~creek.generate.wavelength.load_fragments_from_vault`, whose
        raw-frontmatter cutoff #968 already implemented — so this is one
        argument, not a subsystem change.
        """
        if self._wavelength_summary is None:
            self._wavelength_summary = current_phase_summary(
                self.vault_path,
                today=self.today,
                level_policy="documents",
                override=self._state.tiers.override,
            )
        return self._wavelength_summary

    def _leaves(self) -> list[Fragment]:
        """Return the leaf-level slice of loaded fragments (memoised).

        FEAT-025: a fragment is a "leaf" in the current vault when none
        of its ``child_ids`` are present among the loaded fragments —
        the same semantics used by :func:`creek.hierarchy.select_by_policy`.
        """
        if self._leaf_fragments is None:
            self._leaf_fragments = select_by_policy(self._state.fragments, "leaves")
            self._leaf_ids = frozenset(f.id for f in self._leaf_fragments)
        return self._leaf_fragments

    def _is_leaf(self, fragment_id: str) -> bool:
        """Whether *fragment_id* is a leaf in the current vault load.

        Returns ``False`` for IDs that aren't loaded — a fragment we
        can't see can't be verified as a leaf, and conservatively
        excluding it keeps cross-set rollups (synchronicities) honest.

        #969 makes that fail-closed answer carry the ceiling for free: the
        loaded set is now the *admitted* set, so a synchronicity endpoint above
        the ceiling is simply not a leaf and its row is dropped. That is why
        ``## Surprising connections`` needed no gate of its own — adding one
        would have been a second admission rule to keep in step with this.
        """
        self._leaves()
        leaf_ids = self._leaf_ids or frozenset()
        return fragment_id in leaf_ids

    def _resolve_current_phase(self) -> str:
        """Return the active phase value (override > derived > unclassified)."""
        if self._current_phase_override is not None:
            return self._current_phase_override
        return self._resolve_wavelength_summary().phase

    def section_vault_summary(self) -> str:
        """Render section 1: vault summary (counts + frequency distribution).

        Bypasses :func:`_section` deliberately: section 1 always has
        content (the count lines) so the empty-state placeholder is
        unreachable.

        #969: every count is over the *admitted* collections, so this section
        cannot contradict the eddy and thread sections below it, and a count
        cannot disclose the existence of content the rest of the report
        omitted.
        """
        state = self._state
        body = [
            f"- Fragments: {len(state.fragments)}",
            f"- Eddies: {len(state.eddies)}",
            f"- Threads: {len(state.threads)}",
        ]
        # Store the enum member itself rather than ``str(member)`` — the
        # round-trip is fragile if :class:`Frequency` ever stops being a
        # :class:`enum.StrEnum`. ``_frequency_label`` accepts either form.
        distribution = Counter(frag.frequency.primary for frag in state.fragments)
        if distribution:
            body.extend(("", "**Frequency distribution**"))
            for freq, count in sorted(
                distribution.items(),
                key=lambda pair: _frequency_sort_key(pair[0]),
            ):
                body.append(f"- {_frequency_label(freq)}: {count}")
        return _HEADER_VAULT_SUMMARY + "\n\n" + "\n".join(body)

    def section_pre_llm_yield(self) -> str:
        """Render section 2: pre-LLM yield from the latest pipeline run."""
        latest = self._state.latest_yield
        if latest is None:
            return _section(_HEADER_PRE_LLM_YIELD, [])
        deterministic = latest.get("deterministic_classified", "?")
        local_model = latest.get("local_model_processed", "?")
        residue = latest.get("residue", "?")
        body = [
            f"- Run: `{latest.get('run_id', '?')}` "
            f"(timestamp `{latest.get('timestamp', '?')}`)",
            f"- Deterministic: {deterministic} classified",
            f"- Local-model: {local_model} embedded/OCR'd",
            f"- Residue: {residue} (would go to LLM if Pass-3 enabled)",
            f"- `--no-llm`: {'yes' if latest.get('no_llm') else 'no'}",
        ]
        return _HEADER_PRE_LLM_YIELD + "\n\n" + "\n".join(body)

    def section_active_eddies(self) -> str:
        """Render section 3: top eddies by leaf-counted fragments (capped at ten).

        FEAT-025: when any leaves are loaded, state recounts how many
        link to each eddy via the fragment's ``eddies`` wikilinks — the
        stored :attr:`creek.models.Eddy.fragment_count` is ignored. On
        a vault with eddies but no loaded fragments at all, the stored
        count is the only signal available and we fall back to it so
        the placeholder behaviour of FEAT-006 is preserved.

        #969: ``self._state.eddies`` is already the admitted slice —
        :func:`_admit_by_derived_tier` cut it off against the maximum tier of
        the fragments naming each title, because :class:`~creek.models.Eddy`
        carries no ``privacy_tier`` of its own. Note that the
        ``has_leaves=False`` fallback above is reachable only for eddies that
        cleared that cutoff, and an eddy with no member fragments has no tier
        evidence at all, so it clears only at ``ceiling=intimate`` or broader.
        """
        leaf_counts = self._leaf_counts_by_link("eddies")
        has_leaves = bool(self._leaves())
        eddies = sorted(
            self._state.eddies,
            key=lambda e: (
                -self._eddy_count(e, leaf_counts, has_leaves=has_leaves),
                e.title,
            ),
        )[:_TOP_N]
        rows = [
            f"- {eddy.title} — "
            f"{self._eddy_count(eddy, leaf_counts, has_leaves=has_leaves)} "
            "fragment(s)"
            for eddy in eddies
        ]
        return _section(
            _HEADER_ACTIVE_EDDIES,
            self._prepend_annotation(rows, "leaves"),
        )

    def section_active_threads(self) -> str:
        """Render section 4: most-recent threads by ``last_seen`` (capped at ten).

        FEAT-025: the fragment count next to each thread is leaf-counted
        the same way as :meth:`section_active_eddies`. The sort key
        (``last_seen``) is unchanged — recency is independent of level.

        #969 admits threads on the same derived tier, for the same reason:
        a thread title is built from its member fragments and the model
        carries no tier field.
        """
        leaf_counts = self._leaf_counts_by_link("threads")
        has_leaves = bool(self._leaves())
        threads = sorted(
            self._state.threads,
            key=lambda t: (t.last_seen, t.title),
            reverse=True,
        )[:_TOP_N]
        rows = [
            f"- {thread.title} — last seen {thread.last_seen.isoformat()} "
            f"({self._thread_count(thread, leaf_counts, has_leaves=has_leaves)} "
            "fragment(s))"
            for thread in threads
        ]
        return _section(
            _HEADER_ACTIVE_THREADS,
            self._prepend_annotation(rows, "leaves"),
        )

    def section_synchronicities(self) -> str:
        """Render section 5: surprising connections (synchronicities).

        FEAT-025: synchronicities whose endpoints are not both leaves
        are filtered out — a sync from a parent fragment is rhetorical
        structure, not the surprising cross-source resonance the
        section is meant to surface.
        """
        rows = sorted(
            (
                row
                for row in self._state.synchronicities
                if self._is_leaf(row.fragment_a_id) and self._is_leaf(row.fragment_b_id)
            ),
            key=lambda r: (-r.similarity, r.sync_id),
        )[:_TOP_N]
        body = [
            f"- `{row.fragment_a_id}` ↔ `{row.fragment_b_id}` — "
            f"similarity {row.similarity:.2f}, gap {row.time_gap_days}d"
            for row in rows
        ]
        return _section(
            _HEADER_SYNCHRONICITIES,
            self._prepend_annotation(body, "leaves"),
        )

    @staticmethod
    def _prepend_annotation(rows: list[str], level: str) -> list[str]:
        """Insert the level annotation when *rows* has any content.

        Keeps the FEAT-006 empty-state placeholder reachable: when the
        section would otherwise be empty, ``_section`` renders
        :data:`EMPTY_PLACEHOLDER` and the annotation is suppressed.
        """
        if not rows:
            return rows
        return [_level_annotation(level), "", *rows]

    def _leaf_counts_by_link(self, attr: str) -> dict[str, int]:
        """Count leaves grouped by their ``fragment.<attr>`` wiki-link targets.

        Args:
            attr: ``"eddies"`` or ``"threads"`` — the fragment attribute
                holding the per-target wiki-link strings.

        Returns:
            ``{target_title: leaf_count}`` derived from the leaves slice.
        """
        counts: Counter[str] = Counter()
        for frag in self._leaves():
            for target in _wikilink_targets(getattr(frag, attr)):
                counts[target] += 1
        return dict(counts)

    @staticmethod
    def _eddy_count(
        eddy: Eddy,
        leaf_counts: dict[str, int],
        *,
        has_leaves: bool,
    ) -> int:
        """Resolve the displayed eddy count.

        When the vault has any loaded leaf fragments, the leaf count
        wins — including zero, which is the *honest* answer when no
        leaves currently link to this eddy. When the vault has no
        loaded fragments at all, the stored ``fragment_count`` is the
        only signal we have and is used as a fallback.
        """
        if has_leaves:
            return leaf_counts.get(eddy.title, 0)
        return eddy.fragment_count

    @staticmethod
    def _thread_count(
        thread: Thread,
        leaf_counts: dict[str, int],
        *,
        has_leaves: bool,
    ) -> int:
        """Resolve the displayed thread count (see :meth:`_eddy_count`)."""
        if has_leaves:
            return leaf_counts.get(thread.title, 0)
        return thread.fragment_count

    def section_hyperedges(self) -> str:
        """Render section 6: praxis whose source fragments span 2+ eddies.

        Ranked by ``(-len(spanning), praxis.title)`` so the praxis that
        bridges the most eddies appears first; matches the deterministic
        ordering of sections 3-5 instead of falling back to filesystem
        order. The em-dash separator matches the convention used by the
        other list sections.

        #969: ``self._state.praxis`` holds only praxis whose ``derived_from``
        ids *all* resolved inside the admitted fragment index, and
        :meth:`_fragment_to_eddies` intersects each of those fragments'
        ``eddies`` wikilinks with the titles of ``self._state.eddies`` — the
        eddies admitted on their own *derived* tier. So neither an
        above-ceiling praxis title nor an above-ceiling eddy title can ride in
        through this row.

        The intersection deliberately happens *before* the
        ``len(spanning) >= 2`` rank cut below, because an excluded eddy has
        two ways to reach the artifact and both had to close: it can be
        rendered in ``spans:``, and — worse, because it is silent — it can pad
        the span count and thereby promote a row that would not otherwise
        qualify. It also had a third, indirect effect: an eddy that renders
        but is not in ``self._state.eddies`` contributes nothing to
        ``content_tiers``, so the artifact stamp under-reported and
        ``creek.state.read`` served the document below the tier it actually
        carried. A section that emits above-ceiling content without
        contributing to the stamp makes the read gate fail open.
        """
        fragment_to_eddies = self._fragment_to_eddies()
        candidates = [
            (praxis, self._praxis_spans(praxis, fragment_to_eddies))
            for praxis in self._state.praxis
        ]
        ranked = sorted(
            ((p, s) for p, s in candidates if len(s) >= 2),
            key=lambda pair: (-len(pair[1]), pair[0].title),
        )[:_TOP_N]
        body = [
            f"- {praxis.title} — spans: {', '.join(sorted(spanning))}"
            for praxis, spanning in ranked
        ]
        return _section(_HEADER_HYPEREDGES, body)

    def section_lint_summary(self) -> str:
        """Render section 11: the most recent ``creek lint`` summary (FEAT-008).

        The state report appends the lint report verbatim. If no lint has
        run yet, the section still renders with the empty-state placeholder
        so the header is never silently dropped.

        #969 admits the section only at ``ceiling=intimate`` or broader, and
        the whole section or none of it. ``latest_lint_report`` returns a
        **verbatim copy** of a ``00-Creek-Meta/Processing-Log`` artifact that
        embeds titles and tag names, and ``creek/lint/checks/tags.py``
        deliberately surveys the whole vault at ``PrivacyTierOverride.ALL`` so
        it cannot report "no orphan tags" about a vault that has them. There is
        no row-level tier to filter on, so a partially-admitted copy would be a
        silently truncated report presented as a complete one; dropping the
        section and saying so with the standard placeholder is the honest
        alternative.

        This closes the live caveat ``creek_mcp/read_gate.py`` records under
        ``creek.lint``: that its Processing-Log artifact "is unreachable
        through MCP today". ``creek state`` is the thing that serves it back.
        Appending it verbatim is also why ``creek/lint/checks/broken_links.py``
        had to start rendering its sources ``relative_to(vault_path)`` like
        every sibling check: the copy arrives here unedited, so an absolute
        path in the lint report is an absolute path in this artifact — at
        ``ceiling=intimate`` and, since the CLI default is ``all``, on a plain
        ``creek state``.
        """
        if not tier_within_override(PrivacyTier.INTIMATE, self._state.tiers.override):
            return _section(_HEADER_LINT_SUMMARY, [])
        from creek.lint import latest_lint_report

        body = latest_lint_report(self.vault_path)
        if not body:
            return _section(_HEADER_LINT_SUMMARY, [])
        return _HEADER_LINT_SUMMARY + "\n\n" + body.strip()

    def section_drift_warnings(self) -> str:
        """Render section 7: broken wiki-links + stale fragments.

        #969 gates this section by *source*, not by target, and renders every
        path vault-relative.

        The blanket alternative — dropping the section below
        ``ceiling=intimate`` on the grounds that a broken link's target has no
        note and therefore no frontmatter to tier — is true but not decisive.
        The target string is text authored **inside** the source fragment, so
        it carries the source's tier, and the source is gated here. Every
        rendered row is then fully attributable to an admitted fragment, which
        makes the gate complete rather than partial, and keeps the report's
        most operationally useful section alive at the MCP default ceiling.

        Both scanners report ``str(path)`` for absolute paths, and the drift
        section rendered them verbatim until now — leak (1) of the three #969
        reproduced, since an orphan's filename is its fragment's slugified
        title. :func:`_vault_relative` closes this section's half of
        ``docs/generation.md``'s standing "never the absolute path" claim; the
        other half was ``## Lint summary``, and is recorded there.
        """
        admitted = self._state.tiers.admitted_paths
        broken = BrokenLinkScanner().scan(self.vault_path)
        orphans = OrphanScanner(age_days=_STALE_FRAGMENT_AGE_DAYS).scan(
            self.vault_path,
        )
        # Filter first, cap second: capping the raw scan would let ten
        # above-ceiling sources crowd out an admitted eleventh.
        sources = [
            (source, targets)
            for source, targets in broken.broken_links.items()
            if source in admitted
        ][:_TOP_N]
        stale = [path for path in orphans.orphan_paths if path in admitted][:_TOP_N]
        body = [
            f"- Broken links in `{_vault_relative(source, self.vault_path)}`: "
            f"{', '.join(targets)}"
            for source, targets in sources
        ]
        if stale:
            if body:
                body.append("")
            body.append("**Stale fragments**")
            body.extend(
                f"- `{_vault_relative(path, self.vault_path)}`" for path in stale
            )
        return _section(_HEADER_DRIFT_WARNINGS, body)

    # ---- Render and write -------------------------------------------

    def _sections(self) -> list[str]:
        """Render all eleven sections in :data:`SECTION_ORDER`.

        Split out of :meth:`render` so :meth:`write` can compute the artifact
        stamp from the *rendered strings* rather than by re-deriving what each
        section decided — see :meth:`_content_tier`.

        Returns:
            One rendered markdown block per section.
        """
        return [
            self.section_wavelength_snapshot(),
            self.section_vault_summary(),
            self.section_pre_llm_yield(),
            self.section_liminal_watch(),
            self.section_active_eddies(),
            self.section_active_threads(),
            self.section_synchronicities(),
            self.section_hyperedges(),
            self.section_drift_warnings(),
            self.section_suggested_questions(),
            self.section_lint_summary(),
        ]

    def _document(self, sections: Sequence[str]) -> str:
        """Join the header and *sections* into the full markdown document.

        Args:
            sections: Rendered sections in :data:`SECTION_ORDER`.

        Returns:
            The unstamped document.
        """
        return self._document_header() + "\n\n" + "\n\n".join(sections) + "\n"

    def _content_tier(self, sections: Sequence[str]) -> PrivacyTier:
        """Return the highest tier the render actually admitted (#969).

        The reduction runs over ``self._state.tiers.content_tiers`` — one entry
        per admitted fragment, eddy, thread, praxis and liminal-watch note —
        plus two additions. The drift section needs no separate contribution:
        its rows are attributable to admitted fragments, whose tiers are
        already in that set.

        **Addition one is the suggested-questions seed corpus**, and it is
        narrower than it looks. After :meth:`_mining_corpus` narrows the
        snapshot, four of its five content axes are already accounted for:
        fragments are intersected with the admitted slice, threads and eddies
        are *replaced* by the admitted lists, and published essay titles,
        synchronicity ids and compiled-page provenance are read by the miner
        but never rendered — ``_seed_as_prompt`` emits only ``title`` and
        ``brief_description``. The fifth axis is not accounted for: the
        miner's liminal corpus walks all of ``10-Liminal`` (minus
        ``Synchronicities``), while ``content_tiers`` covers only the two
        subfolders :data:`_LIMINAL_SUBDIRS` names. A ``10-Liminal/Compost``
        note therefore reaches ``## Suggested questions`` — its id is rendered
        inside a liminal-cross-eddy prompt — with nothing to answer for it on
        the stamp, which is the same fail-open the hyperedge intersection
        closed. Its tier is added here instead.

        That tier is read off the validated model rather than the raw
        frontmatter, because the snapshot no longer holds the file. The
        divergence is one case, and it does not fail open: a liminal note with
        *no* ``privacy_tier`` key resolves to ``unclassified`` here where a raw
        read would say ``intimate``, and
        :func:`~creek.classify.privacy_filter.tier_sensitivity` ranks
        ``unclassified`` *with* ``personal`` — so the stamp still refuses the
        artifact at ``ceiling=open``, which is the only ceiling at which the
        section did not render.

        **Addition two is the lint summary**, derived from the *rendered
        strings* rather than from a flag the section sets. A flag would create
        an ordering hazard between the section methods and the stamp;
        comparing against the empty-section rendering keeps that part a pure
        function of the document. The seed corpus cannot be recovered the same
        way — a tier is not readable back out of rendered markdown — so it
        goes through the memoised :meth:`_mining_corpus` instead, which is
        order-independent for the same reason a memo is: whether the section
        built it first or this method does, both see one corpus from one walk.

        By construction the result is within the render's own ceiling: every
        contributor cleared a cutoff on its way in, and the lint summary
        escalates to ``intimate`` only on a ceiling that already admits
        ``intimate``.

        Args:
            sections: The rendered sections about to be written.

        Returns:
            The tier to stamp, ``OPEN`` when the render admitted nothing.
        """
        tiers = list(self._state.tiers.content_tiers)
        if self._seeds_admitted():
            tiers.extend(
                tier_of(fragment)
                for fragment, _body, _kind in self._mining_corpus().liminal_fragments
            )
        if _lint_summary_is_populated(sections):
            tiers.append(PrivacyTier.INTIMATE)
        return max_admitted_tier(tiers)

    def render(self) -> str:
        """Return the full markdown report in the FEAT-007 section order.

        Deliberately **unstamped**: the stamp is a property of the artifact on
        disk, not of the rendering, and every existing caller of ``render()``
        wants the document. :meth:`write` adds it.
        """
        return self._document(self._sections())

    def write(self) -> Path:
        """Render, stamp, and write the report under ``00-Creek-Meta/State``.

        Also refreshes ``latest.md`` (symlink where supported, copy on
        Windows or when symlink creation fails). Returns the path of the
        ISO-week file. Re-running in the same ISO week overwrites the
        existing file.

        The stamp (#969) is what turns ``latest.md`` from a cross-ceiling cache
        into a self-describing artifact: it records the highest tier this
        render admitted, which is what ``creek.state.read`` compares a caller's
        ceiling against. Both write paths carry it, because ``latest.md`` is
        the documented session-start context and therefore the file almost
        every reader actually opens — an unstamped copy there would look like a
        pre-#969 artifact and be refused at every ceiling below ``intimate``.

        Returns:
            The ISO-week report path.
        """
        state_dir = self.vault_path.joinpath(*_STATE_SUBPATH)
        state_dir.mkdir(parents=True, exist_ok=True)
        iso_year, iso_week, _ = self.today.isocalendar()
        target = state_dir / f"{iso_year}-W{iso_week:02d}.md"
        sections = self._sections()
        target.write_text(
            stamp_report(
                self._document(sections),
                content_tier=self._content_tier(sections),
                override=self._state.tiers.override,
            ),
            encoding="utf-8",
        )
        _refresh_latest(state_dir, target)
        return target

    # ---- Internal helpers -------------------------------------------

    def _document_header(self) -> str:
        """Render the document title and generation metadata block.

        The vault path is intentionally rendered as the leaf directory
        name only, never the absolute path. If the audit file is ever
        committed or shared, this avoids leaking an operator's home
        directory into the artefact.

        That was only ever true of *this* block until #969: two other places
        put an absolute path into the same document — the drift section, which
        rendered the hygiene scanners' paths verbatim (see
        :func:`_vault_relative`), and the verbatim lint report appended by
        :meth:`section_lint_summary`, whose ``broken-links`` check rendered the
        same scanner's absolute source. Both now render vault-relative, so the
        claim holds for the document and not merely for the header.
        """
        iso_year, iso_week, _ = self.today.isocalendar()
        generated = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        vault_label = self.vault_path.name or str(self.vault_path)
        return (
            f"# Creek state — {iso_year}-W{iso_week:02d}\n\n"
            f"_Generated {generated} from `{vault_label}`._"
        )

    def _fragment_to_eddies(self) -> dict[str, set[str]]:
        """Return ``{fragment_id: {admitted eddy title, ...}}`` from frontmatter.

        Named in the direction of the mapping (fragment -> eddies) to
        match Python ``dict`` semantics; ``_eddies_by_fragment`` was
        ambiguous about which side was the key.

        #969: the wikilink targets are intersected with the titles of
        ``self._state.eddies``, the eddies the ceiling admitted. Being named
        by an admitted fragment is *not* sufficient evidence on its own — an
        eddy with one ``open`` and one ``intimate`` member derives ``intimate``
        and is excluded, yet its title is written in the ``open`` member's
        frontmatter, so the unintersected union rendered it at ``ceiling=open``.
        A target matching no admitted title is dropped rather than passed
        through, which is the fail-closed reading: an eddy nobody admitted is
        an eddy this report cannot name.
        """
        admitted_titles = {eddy.title for eddy in self._state.eddies}
        mapping: dict[str, set[str]] = {}
        for fragment in self._state.fragments:
            targets = set(_wikilink_targets(fragment.eddies)) & admitted_titles
            if targets:
                mapping[fragment.id] = targets
        return mapping

    @staticmethod
    def _praxis_spans(
        praxis: Praxis,
        fragment_to_eddies: dict[str, set[str]],
    ) -> set[str]:
        """Return the union of eddies touched by a praxis's source fragments."""
        spanning: set[str] = set()
        for frag_id in praxis.derived_from:
            spanning.update(fragment_to_eddies.get(frag_id, set()))
        return spanning


# ---------------------------------------------------------------------------
# latest.md handling
# ---------------------------------------------------------------------------


def _refresh_latest(state_dir: Path, target: Path) -> Path:
    """Point ``state_dir/latest.md`` at *target* (symlink or copy fallback).

    Symlinks are preferred so the operator's editor can navigate to the
    underlying ISO-week file in one click. Windows lacks an unprivileged
    symlink path for non-developers, and some networked filesystems
    reject symlinks with ``EPERM``; those cases fall back to a copy.
    """
    latest = state_dir / "latest.md"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
    except OSError:
        # Warn rather than debug: an unlink failure here can mask a
        # permissions issue and leave a dangling symlink behind, so the
        # operator should see it in default-level logs.
        logger.warning("Could not unlink existing latest.md")
    if os.name != "nt":
        try:
            latest.symlink_to(target.name)
        except OSError:
            logger.debug("Symlink unsupported on this filesystem; copying")
        else:
            return latest
    latest.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    return latest
