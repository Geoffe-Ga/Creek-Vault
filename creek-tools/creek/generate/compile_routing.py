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
from creek.vault.links import declared_names, read_header_meta
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


COMPILE_GAPS_RELPATH: Path = Path("00-Creek-Meta/Processing-Log/compile-gaps.jsonl")
"""Vault-relative path of the compile-gaps log."""

_THREAD_DIR: Path = Path("02-Threads")
_EDDY_DIR: Path = Path("03-Eddies")
_FREQ_DIR: Path = Path("06-Frequencies")

_LINKER_TYPES: Final[dict[str, str]] = {"thread": "thread", "eddy": "eddy"}
"""``target_kind`` → the ``type:`` a *linker*-written page of that kind carries.

``creek link`` writes ``02-Threads/`` and ``03-Eddies/`` pages from the
:class:`~creek.models.Thread` / :class:`~creek.models.Eddy` models, whose
``type`` defaults are the literals ``"thread"`` and ``"eddy"``. Those pages
carry no provenance, so they are *known* to this module without ever being
*returned* by it. ``frequency_index`` has no linker-written form and is
deliberately absent.
"""


def _normalise_target(raw: str) -> str:
    """Reduce a compiled-layer reference to the bare name it points at.

    The reference reaching :meth:`CompiledPageIndex.thread` and
    :meth:`~CompiledPageIndex.eddy` is frequently a **wiki-link, not an id**:
    ``creek/link/eddies.py`` builds ``f"[[{eddy.title}]]"`` and merges that
    string into every member fragment's ``eddies``, from where
    :class:`~creek.generate.mining.IdeaSeed` copies it verbatim. A plain
    ``dict.get`` keyed by ``target_id`` therefore missed on every such
    reference, and a **fully compiled vault logged a ``missing`` compile gap
    for every eddy its fragments named** (#881).

    Args:
        raw: A reference in any of the forms that reach this module —
            ``eddy-abc123``, ``[[Messages]]``, ``[[eddy-abc123|Display]]``,
            ``[[eddy-abc123#Section]]``, or any of those with padding.

    Returns:
        The reference with surrounding whitespace, one level of ``[[ ]]``, and
        any ``|display`` / ``#heading`` suffix removed.
    """
    target = raw.strip()
    if target.startswith("[[") and target.endswith("]]"):
        target = target[2:-2]
    target = target.split("|", 1)[0].split("#", 1)[0]
    return target.strip()


@dataclass(frozen=True)
class _NameIndex:
    """Every name a compiled-layer surface of one kind can be referenced by.

    Names are mapped to a **canonical id**, never to a page: whether that id
    has a compiled page is a separate question, and conflating the two is what
    would break mining's provenance fallback.

    Precedence mirrors :func:`creek.vault.links.build_link_index` exactly — a
    four-level ladder of exact id, exact declared name, folded id, folded
    declared name, with the first page in sorted path order winning inside a
    level. The two resolvers must not be able to disagree about which page a
    name means.

    Attributes:
        by_name: Exact-case name → canonical id.
        by_folded: Case-folded name → canonical id.
    """

    by_name: dict[str, str] = field(default_factory=dict)
    by_folded: dict[str, str] = field(default_factory=dict)

    def canonical(self, name: str) -> str | None:
        """Return the id *name* refers to, or ``None`` if no page claims it."""
        exact = self.by_name.get(name)
        if exact is not None:
            return exact
        return self.by_folded.get(name.casefold())


@dataclass(frozen=True)
class CompiledPageIndex:
    """Compiled-layer pages keyed by ``(target_kind, target_id)``.

    Two questions this index answers, and they are deliberately distinct:

    * :meth:`thread` / :meth:`eddy` / :meth:`frequency_index` return a page
      **only** when a provenance-bearing ``type: compiled_page`` exists for
      the target. Admitting a linker-written page here would silently break
      :mod:`creek.generate.mining`, which switches to
      :meth:`fragment_ids_for` the moment a lookup returns non-``None`` — and
      a linker page records no provenance, so ``IdeaSeed.source_fragments``
      would collapse to empty.
    * :meth:`page_exists` reports whether a page of that kind exists *at all*,
      compiled or merely linked. That is what separates a gap that is
      ``missing`` from one that is ``uncompiled``.

    Attributes:
        threads: ``{thread_id: CompiledPage}`` for every page under
            ``02-Threads/``.
        eddies: ``{eddy_id: CompiledPage}`` for every page under
            ``03-Eddies/``.
        frequency_indexes: ``{frequency_id: CompiledPage}`` for every
            page under ``06-Frequencies/``.
        names: ``target_kind`` → the :class:`_NameIndex` resolving that
            kind's titles and aliases to canonical ids. An unknown kind is
            simply absent, so it resolves to nothing.
        bypassed: ``True`` when the index was constructed in bypass
            mode — every lookup returns a miss and miss-logging is
            suppressed (the ``--bypass-compiled`` escape hatch).
    """

    threads: dict[str, CompiledPage] = field(default_factory=dict)
    eddies: dict[str, CompiledPage] = field(default_factory=dict)
    frequency_indexes: dict[str, CompiledPage] = field(default_factory=dict)
    names: dict[str, _NameIndex] = field(default_factory=dict)
    bypassed: bool = False

    def _canonical(self, target_kind: str, target: str) -> str | None:
        """Return the canonical id *target* names for *target_kind*."""
        name_index = self.names.get(target_kind)
        if name_index is None:
            return None
        return name_index.canonical(_normalise_target(target))

    def _lookup(
        self,
        store: dict[str, CompiledPage],
        target_kind: str,
        target: str,
    ) -> CompiledPage | None:
        """Return the compiled page *target* names in *store*, or ``None``."""
        if self.bypassed:
            return None
        page = store.get(_normalise_target(target))
        if page is not None:
            return page
        canonical = self._canonical(target_kind, target)
        return None if canonical is None else store.get(canonical)

    def thread(self, target_id: str) -> CompiledPage | None:
        """Return the compiled thread page for *target_id*, or ``None``."""
        return self._lookup(self.threads, "thread", target_id)

    def eddy(self, target_id: str) -> CompiledPage | None:
        """Return the compiled eddy page for *target_id*, or ``None``."""
        return self._lookup(self.eddies, "eddy", target_id)

    def frequency_index(self, target_id: str) -> CompiledPage | None:
        """Return the compiled frequency-index page, or ``None``."""
        return self._lookup(self.frequency_indexes, "frequency_index", target_id)

    def page_exists(self, target_kind: str, target: str) -> bool:
        """Return whether *target* names a page of *target_kind* at all.

        True for a provenance-bearing compiled page **and** for the
        ``type: thread`` / ``type: eddy`` page ``creek link`` writes. False
        for an unknown kind, for a bypassed index, and for a reference no page
        claims by id, title, or alias.

        Args:
            target_kind: ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
            target: A reference in any form :func:`_normalise_target` accepts.
        """
        if self.bypassed:
            return False
        return self._canonical(target_kind, target) is not None

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
    roots = {
        "thread": vault_path / _THREAD_DIR,
        "eddy": vault_path / _EDDY_DIR,
        "frequency_index": vault_path / _FREQ_DIR,
    }
    return CompiledPageIndex(
        threads=_load_pages(roots["thread"], "thread"),
        eddies=_load_pages(roots["eddy"], "eddy"),
        frequency_indexes=_load_pages(roots["frequency_index"], "frequency_index"),
        names={kind: _load_names(root, kind) for kind, root in roots.items()},
    )


def _page_identity(meta: dict[str, object], target_kind: str) -> str | None:
    """Return the canonical id *meta* claims for *target_kind*, or ``None``.

    A ``type: compiled_page`` header contributes its ``target_id`` (and only
    when its ``target_kind`` matches). The linker-written ``type: thread`` /
    ``type: eddy`` headers contribute their ``id``. Anything else — a
    fragment, a hand-written note, a header with no ``type`` — contributes
    nothing.
    """
    declared_type = meta.get("type")
    linker_type = _LINKER_TYPES.get(target_kind)
    if declared_type == "compiled_page":
        if meta.get("target_kind") != target_kind:
            return None
        raw = meta.get("target_id")
    elif linker_type is not None and declared_type == linker_type:
        raw = meta.get("id")
    else:
        return None
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _load_names(root: Path, target_kind: str) -> _NameIndex:
    """Build the name → canonical-id index for *target_kind* under *root*.

    Header-only (:func:`creek.vault.links.read_header_meta`), so a vault with
    35k pages does not pay to parse a body just to learn a page's aliases.
    Both compiled and linker-written pages are indexed here — knowing that
    ``[[Messages]]`` names a real eddy is what lets a gap be reported as
    ``uncompiled`` rather than falsely as ``missing``.
    """
    if not root.exists():
        return _NameIndex()
    pages = [
        (page_id, declared_names(meta))
        for md_file in sorted(root.rglob("*.md"))
        for meta in (read_header_meta(md_file),)
        for page_id in (_page_identity(meta, target_kind),)
        if page_id is not None
    ]
    by_name: dict[str, str] = {}
    by_folded: dict[str, str] = {}

    def _register(name: str, page_id: str) -> None:
        """Claim *name* for *page_id* unless an earlier page already holds it."""
        by_name.setdefault(name, page_id)
        by_folded.setdefault(name.casefold(), page_id)

    for page_id, _ in pages:
        _register(page_id, page_id)
    for page_id, names in pages:
        for name in names:
            _register(name, page_id)
    return _NameIndex(by_name=by_name, by_folded=by_folded)


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


def record_compile_gap(
    vault_path: Path,
    index: CompiledPageIndex,
    *,
    target_kind: str,
    target_id: str,
    surfaced_by: str,
) -> None:
    """Append a compile gap, naming the reason *index* can actually justify.

    Every routing miss still reaches the backlog — that is not negotiable, and
    the guard suite ``tests/test_compile_gap_signal.py`` exists to keep it
    that way. #881 was never "log less"; it was "stop lying about what is
    absent". So the reason is now derived rather than hard-coded:

    * ``"uncompiled"`` — a page of this kind exists (a ``creek link``-written
      thread/eddy, say) but has never been through ``creek compile``. A real
      operator task, honestly named.
    * ``"missing"`` — nothing in the vault claims this target by id, title, or
      alias. Byte-identical to what was logged before #881.

    Args:
        vault_path: Vault root.
        index: The index the miss came from, so the reason and the lookup
            cannot disagree about which pages exist.
        target_kind: ``"thread"``, ``"eddy"``, ``"frequency_index"``, or any
            other surface the caller names — an unrecognised kind simply has
            no page index and reports ``"missing"``.
        target_id: The reference as the caller holds it, logged verbatim so
            the operator sees the spelling their vault actually contains.
        surfaced_by: Verb that hit the gap, plus an optional strategy tag.
    """
    reason = "uncompiled" if index.page_exists(target_kind, target_id) else "missing"
    log_compile_gap(
        vault_path,
        target_kind=target_kind,
        target_id=target_id,
        surfaced_by=surfaced_by,
        reason=reason,
    )
