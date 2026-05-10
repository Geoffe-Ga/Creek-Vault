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
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

import frontmatter
import yaml
from pydantic import ValidationError

from creek.clean.hygiene import BrokenLinkScanner, OrphanScanner
from creek.generate.indexes import FREQUENCY_NAMES
from creek.models import Eddy, Fragment, Frequency, Praxis, Thread

logger = logging.getLogger(__name__)


SECTION_ORDER: tuple[str, ...] = (
    "## Vault summary",
    "## Pre-LLM yield",
    "## Active eddies",
    "## Active threads",
    "## Surprising connections",
    "## Hyperedges",
    "## Drift warnings",
    "## Lint summary",
)
"""Section headers in the order pinned by FEAT-006.

FEAT-007 inserts a wavelength snapshot and a suggested-questions
section between ``## Vault summary`` and ``## Pre-LLM yield``. FEAT-008
appends the latest ``creek lint`` summary as the final section.
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
_SYNCHRONICITIES_SUBDIR: str = "10-Liminal/Synchronicities"
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


@dataclass
class _VaultState:
    """Snapshot of every collection :class:`StateReportGenerator` reads.

    Attributes:
        fragments: Fragment models from ``01-Fragments``.
        threads: Thread models from ``02-Threads``.
        eddies: Eddy models from ``03-Eddies``.
        praxis: Praxis models from ``04-Praxis``.
        synchronicities: Synchronicity rows from ``10-Liminal/Synchronicities``.
        latest_yield: Most recent line of
            ``00-Creek-Meta/Processing-Log/run-summary.jsonl``, or
            ``None`` when the log is missing or empty.
    """

    fragments: list[Fragment] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    eddies: list[Eddy] = field(default_factory=list)
    praxis: list[Praxis] = field(default_factory=list)
    synchronicities: list[_SynchronicityRow] = field(default_factory=list)
    latest_yield: dict[str, object] | None = None


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
    cls: type[Fragment] | type[Thread] | type[Eddy] | type[Praxis],
) -> list[Fragment | Thread | Eddy | Praxis]:
    """Walk *root* and return every model whose ``type`` frontmatter matches.

    Files that fail YAML parsing or schema validation are skipped at
    DEBUG level rather than aborting the report — drift in a single
    fragment must not block the whole audit view.
    """
    if not root.exists():
        return []
    collected: list[Fragment | Thread | Eddy | Praxis] = []
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
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        logger.debug("Last run-summary line is not valid JSON")
        return None
    # JSON allows non-object roots (e.g. an array) — reject them so the
    # rest of the pipeline can rely on dict-shaped yields.
    return payload if isinstance(payload, dict) else None


def _load_vault_state(vault_path: Path) -> _VaultState:
    """Snapshot every collection the report needs from *vault_path*.

    The ``isinstance`` guards in each comprehension are a mypy narrow,
    not a runtime safety check: :func:`_load_typed_models` only appends
    after ``cls.model_validate`` succeeds, so the runtime type is
    already correct. The guard exists because the helper's return type
    is the union ``list[Fragment | Thread | Eddy | Praxis]``, which
    mypy cannot narrow per-call. Removing the guards triggers
    ``assignment`` errors under strict mode.
    """
    fragments_root = vault_path / _FRAGMENTS_SUBDIR
    threads_root = vault_path / _THREADS_SUBDIR
    eddies_root = vault_path / _EDDIES_SUBDIR
    praxis_root = vault_path / _PRAXIS_SUBDIR
    fragments = [
        f
        for f in _load_typed_models(fragments_root, type_tag="fragment", cls=Fragment)
        if isinstance(f, Fragment)
    ]
    threads = [
        t
        for t in _load_typed_models(threads_root, type_tag="thread", cls=Thread)
        if isinstance(t, Thread)
    ]
    eddies = [
        e
        for e in _load_typed_models(eddies_root, type_tag="eddy", cls=Eddy)
        if isinstance(e, Eddy)
    ]
    praxis = [
        p
        for p in _load_typed_models(praxis_root, type_tag="praxis", cls=Praxis)
        if isinstance(p, Praxis)
    ]
    return _VaultState(
        fragments=fragments,
        threads=threads,
        eddies=eddies,
        praxis=praxis,
        synchronicities=_load_synchronicities(vault_path / _SYNCHRONICITIES_SUBDIR),
        latest_yield=_load_latest_yield(_yield_log_path(vault_path)),
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


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class StateReportGenerator:
    """Render the ``creek state`` audit report for an existing vault.

    The generator is a *view* — every method reads existing vault files
    and returns markdown. It never invokes the classification, linking,
    or compile passes.

    Attributes:
        vault_path: Root of the Obsidian vault.
        today: Date used to derive the ISO-week filename. Defaults to
            today (UTC) so the same week's report is overwritten on
            successive runs.
    """

    def __init__(self, vault_path: Path, *, today: date | None = None) -> None:
        """Load the vault snapshot and stash *vault_path* for later use.

        Args:
            vault_path: Root of the Obsidian vault.
            today: Optional injected date for tests / week-pinning.
                Defaults to ``datetime.now(UTC).date()``.
        """
        self.vault_path = vault_path
        self.today = today or datetime.now(tz=UTC).date()
        self._state = _load_vault_state(vault_path)

    # ---- Sections ---------------------------------------------------

    def section_vault_summary(self) -> str:
        """Render section 1: vault summary (counts + frequency distribution).

        Bypasses :func:`_section` deliberately: section 1 always has
        content (the count lines) so the empty-state placeholder is
        unreachable.
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
            body.append("")
            body.append("**Frequency distribution**")
            for freq, count in sorted(
                distribution.items(),
                key=lambda pair: _frequency_sort_key(pair[0]),
            ):
                body.append(f"- {_frequency_label(freq)}: {count}")
        return SECTION_ORDER[0] + "\n\n" + "\n".join(body)

    def section_pre_llm_yield(self) -> str:
        """Render section 2: pre-LLM yield from the latest pipeline run."""
        latest = self._state.latest_yield
        if latest is None:
            return _section(SECTION_ORDER[1], [])
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
        return SECTION_ORDER[1] + "\n\n" + "\n".join(body)

    def section_active_eddies(self) -> str:
        """Render section 3: top eddies by ``fragment_count`` (capped at ten)."""
        eddies = sorted(
            self._state.eddies,
            key=lambda e: (-e.fragment_count, e.title),
        )[:_TOP_N]
        body = [
            f"- {eddy.title} — {eddy.fragment_count} fragment(s)" for eddy in eddies
        ]
        return _section(SECTION_ORDER[2], body)

    def section_active_threads(self) -> str:
        """Render section 4: most-recent threads by ``last_seen`` (capped at ten)."""
        threads = sorted(
            self._state.threads,
            key=lambda t: (t.last_seen, t.title),
            reverse=True,
        )[:_TOP_N]
        body = [
            f"- {thread.title} — last seen {thread.last_seen.isoformat()} "
            f"({thread.fragment_count} fragment(s))"
            for thread in threads
        ]
        return _section(SECTION_ORDER[3], body)

    def section_synchronicities(self) -> str:
        """Render section 5: surprising connections (synchronicities)."""
        rows = sorted(
            self._state.synchronicities,
            key=lambda r: (-r.similarity, r.sync_id),
        )[:_TOP_N]
        body = [
            f"- `{row.fragment_a_id}` ↔ `{row.fragment_b_id}` — "
            f"similarity {row.similarity:.2f}, gap {row.time_gap_days}d"
            for row in rows
        ]
        return _section(SECTION_ORDER[4], body)

    def section_hyperedges(self) -> str:
        """Render section 6: praxis whose source fragments span 2+ eddies.

        Ranked by ``(-len(spanning), praxis.title)`` so the praxis that
        bridges the most eddies appears first; matches the deterministic
        ordering of sections 3-5 instead of falling back to filesystem
        order. The em-dash separator matches the convention used by the
        other list sections.
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
        return _section(SECTION_ORDER[5], body)

    def section_lint_summary(self) -> str:
        """Render section 8: the most recent ``creek lint`` summary (FEAT-008).

        The state report appends the lint report verbatim. If no lint has
        run yet, the section still renders with the empty-state placeholder
        so the header is never silently dropped.
        """
        from creek.lint import latest_lint_report

        body = latest_lint_report(self.vault_path)
        if not body:
            return _section(SECTION_ORDER[7], [])
        return SECTION_ORDER[7] + "\n\n" + body.strip()

    def section_drift_warnings(self) -> str:
        """Render section 7: broken wiki-links + stale fragments."""
        broken = BrokenLinkScanner().scan(self.vault_path)
        orphans = OrphanScanner(age_days=_STALE_FRAGMENT_AGE_DAYS).scan(
            self.vault_path,
        )
        body: list[str] = []
        for source, targets in list(broken.broken_links.items())[:_TOP_N]:
            label = ", ".join(targets)
            body.append(f"- Broken links in `{source}`: {label}")
        if orphans.orphan_paths:
            if body:
                body.append("")
            body.append("**Stale fragments**")
            for path in orphans.orphan_paths[:_TOP_N]:
                body.append(f"- `{path}`")
        return _section(SECTION_ORDER[6], body)

    # ---- Render and write -------------------------------------------

    def render(self) -> str:
        """Return the full markdown report (all seven sections in order)."""
        sections = [
            self.section_vault_summary(),
            self.section_pre_llm_yield(),
            self.section_active_eddies(),
            self.section_active_threads(),
            self.section_synchronicities(),
            self.section_hyperedges(),
            self.section_drift_warnings(),
            self.section_lint_summary(),
        ]
        return self._document_header() + "\n\n" + "\n\n".join(sections) + "\n"

    def write(self) -> Path:
        """Render the report and write it under ``00-Creek-Meta/State``.

        Also refreshes ``latest.md`` (symlink where supported, copy on
        Windows or when symlink creation fails). Returns the path of the
        ISO-week file. Re-running in the same ISO week overwrites the
        existing file.
        """
        state_dir = self.vault_path.joinpath(*_STATE_SUBPATH)
        state_dir.mkdir(parents=True, exist_ok=True)
        iso_year, iso_week, _ = self.today.isocalendar()
        target = state_dir / f"{iso_year}-W{iso_week:02d}.md"
        target.write_text(self.render(), encoding="utf-8")
        _refresh_latest(state_dir, target)
        return target

    # ---- Internal helpers -------------------------------------------

    def _document_header(self) -> str:
        """Render the document title and generation metadata block.

        The vault path is intentionally rendered as the leaf directory
        name only, never the absolute path. If the audit file is ever
        committed or shared, this avoids leaking an operator's home
        directory into the artefact.
        """
        iso_year, iso_week, _ = self.today.isocalendar()
        generated = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        vault_label = self.vault_path.name or str(self.vault_path)
        return (
            f"# Creek state — {iso_year}-W{iso_week:02d}\n\n"
            f"_Generated {generated} from `{vault_label}`._"
        )

    def _fragment_to_eddies(self) -> dict[str, set[str]]:
        """Return ``{fragment_id: {eddy_title, ...}}`` from fragment frontmatter.

        Named in the direction of the mapping (fragment -> eddies) to
        match Python ``dict`` semantics; ``_eddies_by_fragment`` was
        ambiguous about which side was the key.
        """
        mapping: dict[str, set[str]] = {}
        for fragment in self._state.fragments:
            targets = set(_wikilink_targets(fragment.eddies))
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
            return latest
        except OSError:
            logger.debug("Symlink unsupported on this filesystem; copying")
    latest.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    return latest
