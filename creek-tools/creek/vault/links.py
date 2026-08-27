"""Wiki-link resolution for the vault — one definition, shared by every check.

A vault page is linkable by more than its filename. Since #730 the eddy and
thread linkers write date-prefixed files (``2020-09-26-Messages.md``) and put
the human-readable name in ``aliases:``; fragments then link the alias form,
``[[Messages]]``. Two lint checks nevertheless resolved links against filename
stems alone — ``BrokenLinkScanner`` in :mod:`creek.clean.hygiene` and the
orphan-compiled check — each with its own copy of that answer.

The result (issue #887, demo vault, 2026-07-22) was a lint report that was
almost entirely wrong: ``broken-links`` flagged 66,380 links of which 65,879
(99.2%) resolved through some page's ``aliases`` or ``title``, and
``orphan-compiled`` flagged a thread page carrying roughly 30,000 inbound
``[[Messages]]`` links. A check wrong 99.2% of the time is worse than one
that does not exist, because its output still has to be read.

This module is the single answer to "does this wiki-link resolve, and to
what". Callers build one :class:`LinkIndex` per run and query it.

``OrphanScanner`` — ``creek clean``'s own orphan check, distinct from the
``orphan-compiled`` lint check — was the last stem-based holdout and adopted
this index in #1225.

Resolution follows Obsidian: an exact-case match wins, and a case-insensitive
match is the fallback. The frontmatter is read **header-only** — a 35k-file
vault must never pay to load every body into memory just to learn a page's
aliases. :func:`read_header_meta` exposes that same header-only read to
callers who need more of the header than its linkable names.

Targets and sources are deliberately asymmetric (#1344)
-------------------------------------------------------

Every ``*.md`` in the vault remains a valid link **target**:
:func:`build_link_index` walks the whole tree and is unchanged. The set
surveyed as link **sources** — :func:`iter_link_sources` — is a strict named
subset that withholds Creek's own machine-written documents *about* the vault.
The asymmetry is the point, not an oversight to be simplified away: a scanner
that reads its own report back as content inflates its findings on every run
(measured 1 → 2 → 3 over three ``creek lint`` passes on a vault holding one
genuine broken link).

A fourth prefix is admissible only if a file there **quotes the scanner's own
output, or documents the link syntax rather than using it**. That criterion
deliberately excludes generated pages that quote *fragment body text* — an
``08-Decisions`` brief, say. A dangling link inside a quoted excerpt is
genuinely dangling and worth reporting, and the duplication it causes is
bounded by the number of quoting pages rather than compounding run over run.

The ``Ontology`` prefix is the weakest of the three: it is withheld only
because link extraction is not code-span aware, so a backticked
``[[note-name]]`` example reads as a link. Issue #1460 tracks that root-cause
fix, after which the prefix can be dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

_FENCE: str = "---"
"""Delimiter opening and closing a YAML frontmatter block."""

_MAX_HEADER_LINES: int = 200
"""Give up on a header longer than this rather than scan a whole file.

A frontmatter block this long is malformed — almost certainly an unclosed
fence — and following it would read the entire body, which is the cost this
module exists to avoid.
"""

_MAX_HEADER_BYTES: int = 64 * 1024
"""Byte ceiling on a single header, for the same reason as the line cap."""

_UNSURVEYED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("00-Creek-Meta", "Processing-Log"),
    ("00-Creek-Meta", "State"),
    ("00-Creek-Meta", "Ontology"),
)
"""Vault-relative directories whose files are never surveyed as link sources.

Each entry is a tuple of **path components**, not a string prefix, so a
sibling whose name merely starts the same way — ``State-Machine-Notes.md``,
``Processing-Logs-Archive.md`` — keeps its links surveyed. See
:func:`iter_link_sources` for the argument admitting each one.
"""


@dataclass(frozen=True)
class LinkIndex:
    """Every name a vault page can be linked by, mapped to that page.

    Attributes:
        by_name: Exact-case name → page path.
        by_folded: Case-folded name → page path, the Obsidian-style fallback
            consulted only when no exact-case entry matches.
    """

    by_name: dict[str, Path]
    by_folded: dict[str, Path]

    def resolve(self, target: str) -> Path | None:
        """Return the page *target* names, or ``None`` if nothing matches.

        Args:
            target: A wiki-link target, with any ``#heading`` or ``|display``
                suffix already stripped by the caller's link regex.

        Returns:
            The resolved page path, or ``None`` when the link is genuinely
            dangling.
        """
        name = target.strip()
        exact = self.by_name.get(name)
        if exact is not None:
            return exact
        return self.by_folded.get(name.casefold())

    def __contains__(self, target: object) -> bool:
        """Return whether *target* is a string naming some page in the vault."""
        return isinstance(target, str) and self.resolve(target) is not None


WIKILINK_PATTERN: re.Pattern[str] = re.compile(
    r"\[\[([^\]|#]+?)(?:[#|][^\]]*?)?\]\]",
)
"""Matches Obsidian wiki-links, capturing only the file portion of the target.

Heading anchors (``[[Note#Heading]]``) and aliases (``[[Note|alias]]``) are
excluded from the captured target, and same-file anchors (``[[#Heading]]``)
produce no target at all -- they name no file to resolve (issue #835).

This lives beside :class:`LinkIndex` because the two are one contract:
:meth:`LinkIndex.resolve` documents that its argument arrives with any
``#heading`` or ``|display`` suffix already stripped, and this is the pattern
that strips them. A caller that pairs a looser regex with this index gets
false "broken link" reports for every anchor and alias in the vault, which is
exactly what #1518 found in ``creek/clean/filters/markdown.py``.
"""


def extract_wikilinks(content: str) -> list[str]:
    """Extract wiki-link targets from markdown content.

    Targets are the file portions only: heading anchors and aliases are
    stripped by the pattern, and same-file anchor links (``[[#Heading]]``)
    yield no target (issue #835).

    Args:
        content: Raw markdown text.

    Returns:
        List of wiki-link target strings, ready to hand to
        :meth:`LinkIndex.resolve`.
    """
    return WIKILINK_PATTERN.findall(content)


def _read_header_block(path: Path) -> str | None:
    """Return the raw YAML frontmatter text of *path*, or ``None``.

    Reads line by line and stops at the closing fence, so the body is never
    pulled into memory. Returns ``None`` for a file with no frontmatter, an
    unterminated header, or one exceeding the size caps.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            if handle.readline().strip() != _FENCE:
                return None
            lines: list[str] = []
            size = 0
            for _ in range(_MAX_HEADER_LINES):
                line = handle.readline()
                if not line:
                    return None
                if line.strip() == _FENCE:
                    return "".join(lines)
                size += len(line)
                if size > _MAX_HEADER_BYTES:
                    return None
                lines.append(line)
    except OSError:
        return None
    return None


def declared_names(meta: dict[str, object]) -> list[str]:
    """Extract the linkable names a frontmatter mapping declares.

    Public because the compiled-layer name index in
    :mod:`creek.generate.compile_routing` resolves the *same* declared names
    against the *same* header-only read. Two copies of "what counts as a name
    a page declares" is exactly how the two resolvers drift apart.

    ``title`` contributes one name; ``aliases`` contributes each entry, and
    is accepted as either a scalar or a list because Obsidian permits both.
    Non-string and blank entries are ignored rather than stringified — an
    accidental ``aliases: [null]`` should not create a page named ``None``.
    """
    names: list[str] = []
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        names.append(title.strip())
    raw = meta.get("aliases")
    entries = [raw] if isinstance(raw, str) else raw
    if isinstance(entries, list):
        names.extend(
            entry.strip()
            for entry in entries
            if isinstance(entry, str) and entry.strip()
        )
    return names


def read_header_meta(path: Path) -> dict[str, object]:
    """Return the YAML frontmatter mapping of *path*, header-only.

    This is the one frontmatter reader in this module, and it reads no
    further than the closing ``---`` fence. Callers that want a page's
    declared ``type`` must use this rather than ``frontmatter.load``, which
    parses the body too — on a 35k-file vault that is precisely the expense
    this module exists to avoid.

    Args:
        path: Markdown file to read.

    "Malformed YAML" is caught wider than ``yaml.YAMLError``, because PyYAML
    does not raise one for every way a header can be malformed: its
    constructors call ``int()`` and ``datetime.date()`` on the scanned text
    and let those raise ``ValueError`` straight through. ``n: <5000 digits>``
    trips CPython's 4300-digit int-parsing limit and ``created: 2020-13-45``
    resolves as a timestamp and then fails to construct — both well inside the
    size caps, and ``created`` is written by the ingest parsers from export
    metadata nobody in this system authored. Since :func:`build_link_index`
    reads the header of every ``*.md`` in the vault, one such file aborted the
    whole of ``creek lint`` and ``creek state`` rather than costing itself its
    aliases (#1344).

    Args:
        path: Markdown file to read.

    Returns:
        The parsed header mapping, or an empty dict for any of: an unreadable
        file, no opening fence, an unterminated header, a header past the size
        caps, malformed YAML, or a header that parses to something other than
        a mapping. A whole-vault walk must not lose its run to one bad file.
    """
    block = _read_header_block(path)
    if block is None:
        return {}
    try:
        meta = yaml.safe_load(block)
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError):
        return {}
    if not isinstance(meta, dict):
        return {}
    return meta


def _header_names(path: Path) -> list[str]:
    """Return the ``title`` and ``aliases`` names declared by *path*.

    Any failure — unreadable file, malformed YAML, a header that is not a
    mapping — yields no names rather than raising. Lint walks every file in
    the vault, so one bad header must not cost the whole run its index.
    """
    return declared_names(read_header_meta(path))


def page_names(path: Path) -> list[str]:
    """Return every exact-case name *path* can be linked by.

    The filename stem **and** the declared names, which is the pairing
    :func:`build_link_index` registers and the pairing a caller has to
    reproduce to answer "does any page claim this spelling". Public
    because ``creek purge`` needs exactly that question answered without
    building a whole index: a wikilink scrub that knows only
    ``post["title"]`` leaves a link written by the private *filename*
    standing after a right-to-be-forgotten request (#903).

    Only the header is read, never the body, so asking it of every file
    in a large vault costs one short read apiece.

    Args:
        path: A vault markdown file.

    Returns:
        The stem first, then the declared names in declaration order.
        Duplicates are not removed; the stem and a declared ``title``
        are routinely the same string.
    """
    return [path.stem, *_header_names(path)]


def _is_unsurveyed(relative: Path) -> bool:
    """Return whether *relative* lies under a withheld source directory.

    Args:
        relative: A path relative to the vault root.

    Returns:
        True when the path's leading components match any entry of
        :data:`_UNSURVEYED_PREFIXES`.
    """
    parts = relative.parts
    return any(parts[: len(prefix)] == prefix for prefix in _UNSURVEYED_PREFIXES)


def iter_link_sources(vault_path: Path) -> list[Path]:
    """List every vault page whose **outbound** links Creek surveys.

    The whole vault, minus three vault-relative directories holding Creek's
    own machine-written documents *about* the vault:

    ``00-Creek-Meta/Processing-Log/``
        ``creek lint`` writes its report here and renders every finding as
        ``- `src` → `[[target]]` ``, so the next run reads its own output back
        as vault content. Measured on a vault with ONE genuine broken link:
        three successive runs reported 1, then 2, then 3.

    ``00-Creek-Meta/State/``
        ``creek state`` echoes the same targets (``section_drift_warnings``)
        and appends the whole lint report verbatim
        (``section_lint_summary``).

    ``00-Creek-Meta/Ontology/``
        The canonical spec ``creek init`` deploys; its line 746 *documents*
        wiki-link syntax with a backticked ``[[note-name]]`` example. Measured:
        the single finding a whole-vault scan produces on a fresh 32-file
        ``creek init`` vault. Issue #1460 tracks the code-span-aware extraction
        that would let this prefix be dropped.

    Nothing else is withheld. ``00-Creek-Meta/Tag-Garden.md`` emits real
    ``[[fragment-id]]`` links (``creek/generate/tags.py``) and must stay a
    source; ``00-Creek-Meta/Skills/``, ``AGENTS.md`` and ``11-Other-Authors/``
    carry no links today and stay in scope regardless.

    Args:
        vault_path: Root of the Obsidian vault. A path that does not exist
            yields no sources rather than raising.

    Returns:
        A sorted list of ``*.md`` paths — materialised, matching the contract
        of the fragment listing it replaces, and deterministic so two runs on
        one vault report identically.
    """
    if not vault_path.is_dir():
        return []
    return [
        path
        for path in sorted(vault_path.rglob("*.md"))
        if not _is_unsurveyed(path.relative_to(vault_path))
    ]


def build_link_index(vault_path: Path) -> LinkIndex:
    """Build the name → page index for every markdown file under *vault_path*.

    Resolution follows a strict four-level ladder; the first level with a hit
    wins, and within a level the first page in sorted path order wins:

    1. exact-case filename stem
    2. exact-case declared name (frontmatter ``title`` or an ``aliases`` entry)
    3. case-folded filename stem
    4. case-folded declared name

    In one sentence: a page's own filename stem outranks any *other* page's
    declared name at the same case-match quality; an exact-case match still
    outranks a folded one regardless of provenance; ties inside a level go to
    the first page in sorted path order.

    The ladder falls out of two registration passes over one pre-sorted list
    (stems first, then declared names) combined with the exact-before-folded
    lookup in :meth:`LinkIndex.resolve` — no provenance is stored.

    Why (#1224): a page's stem is a fact about that page's identity, the name
    the operator sees in the Obsidian sidebar and the one Obsidian itself
    resolves first. An alias is a courtesy name some *different* page
    volunteered. Before this change the winner was whichever page happened to
    sort first, so a fragment aliasing ``Messages`` beat the eddy page
    literally named ``Messages.md``.

    The one case where a foreign alias still shadows a stem is level 2 over
    level 3 — an exact-case alias beats a folded stem. That is deliberate:
    Obsidian's exact-before-folded rule is preserved, and the ladder only
    reorders *within* a case-match quality.

    Args:
        vault_path: Root of the Obsidian vault.

    Returns:
        A :class:`LinkIndex` covering the whole vault.
    """
    pages = [
        (md_file, md_file.stem, _header_names(md_file))
        for md_file in sorted(vault_path.rglob("*.md"))
    ]
    by_name: dict[str, Path] = {}
    by_folded: dict[str, Path] = {}

    def _register(name: str, md_file: Path) -> None:
        """Claim *name* for *md_file* unless an earlier page already holds it."""
        by_name.setdefault(name, md_file)
        by_folded.setdefault(name.casefold(), md_file)

    for md_file, stem, _ in pages:
        _register(stem, md_file)
    for md_file, _, declared in pages:
        for name in declared:
            _register(name, md_file)
    return LinkIndex(by_name=by_name, by_folded=by_folded)
