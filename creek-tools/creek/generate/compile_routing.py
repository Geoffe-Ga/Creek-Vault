"""Compiled-layer routing helpers (FEAT-004).

``creek mine`` and ``creek draft`` route through the compiled layer
first — Threads, Eddies, and per-frequency index notes — falling back
to fragments only when the compiled page is missing or insufficient.
The fallback is an operational signal, not silent: every miss appends
a ``compile-needed`` entry to ``00-Creek-Meta/Processing-Log/
compile-gaps.jsonl`` so ``creek lint`` can surface the gap later.

This module is the single read-side surface for the compiled layer.
Mining and drafting both consume it; the CLI's ``--bypass-compiled``
escape hatch short-circuits the index load entirely.

Trust assumption: the scanned directories ``02-Threads/``,
``03-Eddies/``, and ``06-Frequencies/`` are treated as trusted output
written by ``creek compile`` (FEAT-003). ``_load_pages`` uses
``rglob("*.md")``, which follows symlinks; the ``type: compiled_page``
sentinel plus Pydantic validation make planted non-pages a no-op in
practice, but the routing layer should not be pointed at untrusted
content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # runtime use in dataclass / type hints
from typing import TYPE_CHECKING, Final

import frontmatter
from pydantic import ValidationError

from creek.models import CompiledPage
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


COMPILE_GAPS_RELPATH: Path = Path("00-Creek-Meta/Processing-Log/compile-gaps.jsonl")
"""Vault-relative path of the compile-gaps log."""

_THREAD_DIR: Path = Path("02-Threads")
_EDDY_DIR: Path = Path("03-Eddies")
_FREQ_DIR: Path = Path("06-Frequencies")


@dataclass(frozen=True)
class CompiledPageIndex:
    """Compiled-layer pages keyed by ``(target_kind, target_id)``.

    Attributes:
        threads: ``{thread_id: CompiledPage}`` for every page under
            ``02-Threads/``.
        eddies: ``{eddy_id: CompiledPage}`` for every page under
            ``03-Eddies/``.
        frequency_indexes: ``{frequency_id: CompiledPage}`` for every
            page under ``06-Frequencies/``.
        bypassed: ``True`` when the index was constructed in bypass
            mode — every lookup returns a miss and miss-logging is
            suppressed (the ``--bypass-compiled`` escape hatch).
    """

    threads: dict[str, CompiledPage] = field(default_factory=dict)
    eddies: dict[str, CompiledPage] = field(default_factory=dict)
    frequency_indexes: dict[str, CompiledPage] = field(default_factory=dict)
    bypassed: bool = False

    def thread(self, target_id: str) -> CompiledPage | None:
        """Return the compiled thread page for *target_id*, or ``None``."""
        if self.bypassed:
            return None
        return self.threads.get(target_id)

    def eddy(self, target_id: str) -> CompiledPage | None:
        """Return the compiled eddy page for *target_id*, or ``None``."""
        if self.bypassed:
            return None
        return self.eddies.get(target_id)

    def frequency_index(self, target_id: str) -> CompiledPage | None:
        """Return the compiled frequency-index page, or ``None``."""
        if self.bypassed:
            return None
        return self.frequency_indexes.get(target_id)

    def fragment_ids_for(self, page: CompiledPage) -> tuple[str, ...]:
        """Return the unique fragment IDs traced by *page*'s provenance.

        Uses ``dict.fromkeys`` for O(n) deduplication; insertion order
        is guaranteed by the Python 3.7+ language spec.
        """
        return tuple(
            dict.fromkeys(
                fid for entry in page.provenance for fid in entry.fragment_ids if fid
            ),
        )


def empty_index(*, bypassed: bool = False) -> CompiledPageIndex:
    """Return an empty :class:`CompiledPageIndex`.

    Args:
        bypassed: When ``True``, the returned index reports every
            lookup as a miss without recording a gap. Used by the
            ``--bypass-compiled`` escape hatch.
    """
    return CompiledPageIndex(bypassed=bypassed)


@dataclass(frozen=True)
class CompiledSources:
    """What a prompt's compiled thread/eddy sections were synthesised from.

    Attributes:
        fragment_ids: Every fragment id the named pages' provenance records,
            de-duplicated, in page-then-entry order.
        opaque: ``True`` when at least one named thread or eddy contributes
            prompt text whose sources cannot be enumerated. **Opaque is not
            empty**: the caller must fail closed on it rather than reduce over
            the ids it did manage to collect.
    """

    fragment_ids: tuple[str, ...]
    opaque: bool


NO_COMPILED_SOURCES: Final[CompiledSources] = CompiledSources(
    fragment_ids=(),
    opaque=False,
)
"""The survey of a prompt that renders no compiled section at all.

Distinct from an opaque survey, and the distinction is the whole point: a
prompt naming no thread and no eddy carries no unaccountable compiled text, so
it must not be pushed onto the local model for no privacy gain.
"""


def compiled_source_ids(
    index: CompiledPageIndex,
    *,
    thread_ids: Iterable[str],
    eddy_ids: Iterable[str],
) -> CompiledSources:
    """Return the fragment ids behind a prompt's compiled sections (#1013, #1538).

    The ``## Threads`` and ``## Eddies`` blocks of a draft prompt render
    compiled-page *bodies*, and neither :class:`~creek.models.Thread` nor
    :class:`~creek.models.Eddy` carries a ``privacy_tier`` — so those sections
    cannot be tier-*filtered*, only tier-*accounted*. What they do carry is
    :attr:`~creek.models.CompiledPage.provenance`, the fragment ids each claim
    was synthesised from, which is exactly the evidence a source-tier survey
    needs in order to see them.

    This is the **one** survey behind both draft surfaces:
    :meth:`creek.generate.drafts.DraftGenerator._bind_routing_tier` (the
    ``creek draft`` CLI) and :func:`creek_mcp.tools.draft._compiled_source_ids`
    (the MCP tool). It lives here rather than beside
    :func:`creek.classify.privacy_filter.source_tiers` — where the rest of the
    tier survey lives — for one mechanical reason: ``privacy_filter`` is
    imported by :mod:`creek.generate.drafts`, which
    :mod:`creek.generate`'s package ``__init__`` imports, so a module-level
    import of the compiled layer *from* ``privacy_filter`` would close an
    import cycle. This module is already the single read-side surface for the
    compiled layer and already owns
    :meth:`CompiledPageIndex.fragment_ids_for`, so the survey belongs here and
    the tier reduction stays in ``privacy_filter``.

    **A page that cannot be enumerated is opaque, not empty.** A missing page
    and a page recording no provenance are reported identically, and callers
    must fail closed to :attr:`~creek.models.PrivacyTier.INTIMATE` on either.
    Reading "no provenance" as "no sources" would reopen the laundering hole
    for every page compiled before provenance was recorded. Equally, a caller
    must not skip an unresolvable id and reduce over the rest, which would
    clear a prompt on the strength of its safe half.

    A *bypassed* index (``creek draft --bypass-compiled``) reports every lookup
    as a miss, so every named thread or eddy comes back opaque. That is the
    correct answer rather than an accident: bypass makes the prompt render the
    thread's or eddy's frontmatter *description* instead, text with no
    provenance record anywhere.

    Args:
        index: The compiled-page index the prompt was (or will be) rendered
            from — the same one, so the survey and the render cannot disagree
            about which pages exist.
        thread_ids: Thread ids the seed names.
        eddy_ids: Eddy ids the seed names.

    Returns:
        A :class:`CompiledSources` carrying the de-duplicated provenance ids
        and the ``opaque`` flag.
    """
    ids: list[str] = []
    opaque = False
    for lookup, target_ids in (
        (index.thread, thread_ids),
        (index.eddy, eddy_ids),
    ):
        for target_id in target_ids:
            page = lookup(target_id)
            page_ids = index.fragment_ids_for(page) if page is not None else ()
            if not page_ids:
                opaque = True
                continue
            ids.extend(page_ids)
    return CompiledSources(fragment_ids=tuple(dict.fromkeys(ids)), opaque=opaque)


def load_compiled_pages(vault_path: Path) -> CompiledPageIndex:
    """Scan *vault_path* and return every compiled-layer page.

    Pages are loaded from ``02-Threads/``, ``03-Eddies/``, and
    ``06-Frequencies/``. Files missing a ``type: compiled_page``
    sentinel or failing schema validation are skipped silently.

    Args:
        vault_path: Vault root.

    Returns:
        A populated :class:`CompiledPageIndex`. When the relevant
        directories do not exist the index is empty (``bypassed=False``
        so misses are still logged downstream).
    """
    return CompiledPageIndex(
        threads=_load_pages(vault_path / _THREAD_DIR, "thread"),
        eddies=_load_pages(vault_path / _EDDY_DIR, "eddy"),
        frequency_indexes=_load_pages(vault_path / _FREQ_DIR, "frequency_index"),
    )


def _load_pages(root: Path, target_kind: str) -> dict[str, CompiledPage]:
    """Load every compiled page of *target_kind* under *root*."""
    if not root.exists():
        return {}
    out: dict[str, CompiledPage] = {}
    for md_file in sorted(root.rglob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except FRONTMATTER_LOAD_ERRORS:
            logger.debug("Skipping unreadable compiled page: %s", md_file)
            continue
        metadata = post.metadata.copy()
        if metadata.get("type") != "compiled_page":
            continue
        if metadata.get("target_kind") != target_kind:
            continue
        metadata.setdefault("body", post.content)
        try:
            page = CompiledPage.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid compiled page: %s", md_file)
            continue
        out[page.target_id] = page
    return out


def log_compile_gap(
    vault_path: Path,
    *,
    target_kind: str,
    target_id: str,
    surfaced_by: str,
    reason: str,
) -> None:
    """Append a ``compile-needed`` entry to ``compile-gaps.jsonl``.

    The log is plain JSONL (no chain hash) — it's an operational
    backlog ``creek lint`` reads later, not a tamper-evidence record.

    Args:
        vault_path: Vault root.
        target_kind: ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
        target_id: Stable ID of the missing compiled-layer surface.
        surfaced_by: Verb that hit the gap (``"mine"`` or ``"draft"``)
            plus an optional strategy/context tag.
        reason: Short human-readable reason — typically
            ``"missing"`` or ``"insufficient"``.
    """
    record = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "target_kind": target_kind,
        "target_id": target_id,
        "surfaced_by": surfaced_by,
        "reason": reason,
    }
    log_path = vault_path / COMPILE_GAPS_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
