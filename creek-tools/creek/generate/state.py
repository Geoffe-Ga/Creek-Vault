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

This first slice (PR 1 of 2) implements section 1 (vault summary) and
section 2 (pre-LLM yield), plus the section-ordering and empty-state
contracts that every section must honour. The remaining five sections
are wired into :data:`SECTION_ORDER` and :meth:`StateReportGenerator.render`
as explicit empty-state placeholders so tests can pin the documented
layout now; PR 2 will replace those placeholders with real content,
add the ``write()`` / ``latest.md`` plumbing, and ship the ``creek state``
CLI command. FEAT-007 inserts wavelength snapshot + suggested questions
between sections 1 and 2 once those sections exist.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.generate.indexes import FREQUENCY_NAMES
from creek.models import Eddy, Fragment, Frequency, Thread

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


SECTION_ORDER: tuple[str, ...] = (
    "## Vault summary",
    "## Pre-LLM yield",
    "## Active eddies",
    "## Active threads",
    "## Surprising connections",
    "## Hyperedges",
    "## Drift warnings",
)
"""Section headers in the order pinned by FEAT-006.

Wavelength snapshot + suggested questions (FEAT-007) will be inserted
between ``## Vault summary`` and ``## Pre-LLM yield``; this module
renders only the seven structural sections.
"""

EMPTY_PLACEHOLDER: str = "_No surfacing this week._"
"""Body used when a section has no content to surface.

FEAT-006 acceptance criteria require an explicit empty-state note —
the section header must always render, never silently disappear.
"""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
_THREADS_SUBDIR: str = "02-Threads"
_EDDIES_SUBDIR: str = "03-Eddies"
_YIELD_SUBPATH: tuple[str, ...] = (
    "00-Creek-Meta",
    "Processing-Log",
    "run-summary.jsonl",
)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


@dataclass
class _VaultState:
    """Snapshot of every collection :class:`StateReportGenerator` reads.

    Each section method consumes a subset of these fields directly, so
    helpers can be exercised in isolation. The fields land here even
    when their section has not yet been implemented (PR 2 fills in the
    remaining sections without changing the snapshot shape).

    Attributes:
        fragments: Fragment models from ``01-Fragments``.
        threads: Thread models from ``02-Threads``.
        eddies: Eddy models from ``03-Eddies``.
        latest_yield: Most recent line of
            ``00-Creek-Meta/Processing-Log/run-summary.jsonl``, or
            ``None`` when the log is missing or empty.
    """

    fragments: list[Fragment] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    eddies: list[Eddy] = field(default_factory=list)
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
    cls: type[Fragment] | type[Thread] | type[Eddy],
) -> list[Fragment | Thread | Eddy]:
    """Walk *root* and return every model whose ``type`` frontmatter matches.

    Files that fail YAML parsing or schema validation are skipped at
    DEBUG level rather than aborting the report — drift in a single
    fragment must not block the whole audit view.

    Args:
        root: Directory to scan.
        type_tag: Required ``type`` frontmatter value.
        cls: Pydantic model class used to validate the metadata.

    Returns:
        A list of validated models in filesystem order.
    """
    if not root.exists():
        return []
    collected: list[Fragment | Thread | Eddy] = []
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


def _load_latest_yield(log_path: Path) -> dict[str, object] | None:
    """Return the last non-empty line of the pre-LLM yield JSONL log.

    The whole file is read into memory because the report only needs the
    final line — operationally the log is a short one-line-per-run
    stream, but rotation is the writer's responsibility (see FEAT-005's
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
    """Snapshot every collection the (PR 1) report needs from *vault_path*.

    The ``isinstance`` guards in each comprehension are a mypy narrow,
    not a runtime safety check — :func:`_load_typed_models` only
    appends to its result after ``cls.model_validate`` succeeds, so the
    object's runtime type is already correct. The guard exists because
    the helper's return type is the union ``list[Fragment | Thread |
    Eddy]``, which mypy cannot narrow per-call. Removing the guards
    triggers ``assignment`` errors under strict mode.
    """
    fragments = [
        f
        for f in _load_typed_models(
            vault_path / _FRAGMENTS_SUBDIR,
            type_tag="fragment",
            cls=Fragment,
        )
        if isinstance(f, Fragment)
    ]
    threads = [
        t
        for t in _load_typed_models(
            vault_path / _THREADS_SUBDIR,
            type_tag="thread",
            cls=Thread,
        )
        if isinstance(t, Thread)
    ]
    eddies = [
        e
        for e in _load_typed_models(
            vault_path / _EDDIES_SUBDIR,
            type_tag="eddy",
            cls=Eddy,
        )
        if isinstance(e, Eddy)
    ]
    latest_yield = _load_latest_yield(vault_path.joinpath(*_YIELD_SUBPATH))
    return _VaultState(
        fragments=fragments,
        threads=threads,
        eddies=eddies,
        latest_yield=latest_yield,
    )


def _frequency_label(freq_value: str) -> str:
    """Return a human-readable label for a Frequency enum value."""
    try:
        freq = Frequency(freq_value)
    except ValueError:
        return freq_value
    name = FREQUENCY_NAMES.get(freq)
    return f"{freq.value} ({name})" if name else freq.value


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
    """

    def __init__(self, vault_path: Path) -> None:
        """Load the vault snapshot and stash *vault_path* for later use.

        Args:
            vault_path: Root of the Obsidian vault.
        """
        self.vault_path = vault_path
        self._state = _load_vault_state(vault_path)

    # ---- Implemented sections ----------------------------------------

    def section_vault_summary(self) -> str:
        """Render section 1: vault summary (counts + frequency distribution).

        Bypasses :func:`_section` deliberately: section 1 always has
        content (the count lines) so the empty-state placeholder is
        unreachable. An empty vault renders three ``: 0`` rows, not the
        ``_No surfacing this week._`` body — the regression test pins
        this contract.
        """
        state = self._state
        body = [
            f"- Fragments: {len(state.fragments)}",
            f"- Eddies: {len(state.eddies)}",
            f"- Threads: {len(state.threads)}",
        ]
        distribution = Counter(str(frag.frequency.primary) for frag in state.fragments)
        if distribution:
            body.append("")
            body.append("**Frequency distribution**")
            for freq, count in sorted(distribution.items()):
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

    # ---- Sections deferred to PR 2 -----------------------------------

    def section_active_eddies(self) -> str:
        """Render section 3: top eddies by ``fragment_count`` (PR 2)."""
        return _section(SECTION_ORDER[2], [])

    def section_active_threads(self) -> str:
        """Render section 4: most-recent threads by ``last_seen`` (PR 2)."""
        return _section(SECTION_ORDER[3], [])

    def section_synchronicities(self) -> str:
        """Render section 5: surprising connections — synchronicities (PR 2)."""
        return _section(SECTION_ORDER[4], [])

    def section_hyperedges(self) -> str:
        """Render section 6: praxis spanning multiple eddies (PR 2)."""
        return _section(SECTION_ORDER[5], [])

    def section_drift_warnings(self) -> str:
        """Render section 7: broken wiki-links + stale fragments (PR 2)."""
        return _section(SECTION_ORDER[6], [])

    # ---- Render -----------------------------------------------------

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
        ]
        return self._document_header() + "\n\n" + "\n\n".join(sections) + "\n"

    def _document_header(self) -> str:
        """Render the document title and generation metadata block."""
        generated = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"# Creek state\n\n_Generated {generated} from `{self.vault_path}`._"
