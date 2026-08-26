"""Purge engine — right-to-be-forgotten deletion operations.

Implements the five purge operations required by issue #46:

- **purge_fragment**: delete a single fragment and clean all references.
- **purge_source**: delete every fragment from a given source platform.
- **purge_classifications**: reset classification fields to unclassified.
- **purge_daterange**: delete fragments created within a date range.
- **purge_vault**: destroy all vault content, preserving folder structure.

Every operation supports a dry-run mode that previews changes without
writing to disk. Each call emits a **pair** of audit entries (GAP-002):
an ``intent`` line *before* the first destructive op, then an
``outcome`` line *after* the body completes (``status="complete"``) or
after an exception aborts it (``status="partial"``). The pair shares a
UUID4 ``operation_id`` so a crash recovery tool can reconstruct what
was being attempted from the intent line alone.

The engine is **not** transactional: a SIGKILL between two ``unlink``
calls leaves the filesystem in a partial state. The intent log is the
recovery contract — an operator inspecting
``<vault>/00-Creek-Meta/audit/purge.jsonl`` after a crash will see the
intent entry naming what was being attempted, and (if the process got
that far) an outcome entry with ``status="partial"`` recording how far
the work had progressed. A staging-directory rename pattern would
deliver real atomicity at the cost of doubling the I/O budget for
every purge; it is deferred to a future hardening pass.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote

import frontmatter
from pydantic import BaseModel, Field

from creek._containment import escaping_child
from creek.clean.hygiene import is_external_target
from creek.ingest.journal_staging import ADEPTHOOD_STAGING_RELDIRS
from creek.ingest.ledger import forget_fragment_ids
from creek.ingest.source_unit import split_source_unit
from creek.purge.audit import PurgeAuditEntry, PurgeAuditLog, PurgeOutcomeStatus
from creek.purge.dryrun import DryRunLedger
from creek.purge.meta import META_RELDIR, prune_empty_meta_dirs, sweep_unkept_meta
from creek.vault.links import page_names, read_header_meta
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

_VAULT_CONTENT_FOLDERS: tuple[str, ...] = (
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
)
"""Top-level vault folders whose contents can be purged by ``purge_vault``."""

VAULT_PURGE_CONFIRMATION = "I understand this is irreversible"
"""Exact phrase required to confirm a full-vault purge."""

_VOICE_RELDIR: str = "07-Voice"
"""Vault folder holding the voice subsystem's derived artifacts (#1211)."""

_VOICE_SAMPLES_RELDIR: tuple[str, ...] = ("07-Voice", "Register-Samples")
"""Where ``save_exemplars`` copies exemplar fragment files byte for byte."""

_VOICE_LEXICON_RELDIR: tuple[str, ...] = ("07-Voice", "Lexicon")
"""Where the glossary and per-domain metaphor notes quote source sentences."""

_VOICE_PROFILE_GLOB: str = "*-profile.md"
"""Top-level ``07-Voice`` notes whose ``### Sample Passages`` are exemplar bodies."""

_UNNAMED_FRAGMENT = "<no-id>"
"""Stand-in used when a partial-sweep report has no fragment id to name.

A constant rather than a path or a body excerpt: the purge subsystem's
logs and results are read by operators who are not entitled to the
content being erased, so they name ids and constants only.
"""

_VOICE_BODY_UNDECODABLE = "UnicodeDecodeError"
"""``failure_reason`` recorded when a body could not be decoded strictly.

The exception **type name**, matching :meth:`PurgeEngine._run_audited`'s
rule that an audit line carries the type and never the message.
"""

_FRONTMATTER_RESOURCE_ERRORS: tuple[type[Exception], ...] = (
    RecursionError,
    MemoryError,
)
"""Parser resource exhaustion the purge's *delete-decision* loaders tolerate.

:data:`~creek.vault.reader.FRONTMATTER_LOAD_ERRORS` — ``OSError``,
``TypeError``, ``ValueError``, ``yaml.YAMLError`` — covers the
hand-edited-vault cases and is shared by a dozen call sites across the
tree. It does **not** cover what PyYAML raises when the document itself
is the attack: ``RecursionError`` from deeply nested flow collections
(``[[[[…``, and ``RecursionError`` is a ``RuntimeError``, so nothing in
that tuple catches it), and ``MemoryError`` from alias expansion, since
``SafeLoader`` honours anchors and aliases and a billion-laughs
frontmatter is therefore live.

Both escaped the purge path and aborted the whole operation (#1455).
That is a **denial-of-erasure primitive**: one corrupt or hostile file
in ``01-Fragments/`` permanently blocked ``creek purge vault``, which is
the operator's fallback when everything else has failed — and a vault
that ingests other people's exports does not author all of its own
frontmatter.

Deliberately a *separate, local* tuple rather than a widening of the
shared one. Everywhere else in the tree a ``RecursionError`` or a
``MemoryError`` is a genuine resource failure that must keep
propagating — swallowing it in ``creek classify`` or the lint walk would
turn an exhausted interpreter into a silently short result. Only the
loaders that exist to answer "does this file match, so that it can be
``unlink``-ed" may treat it as "matches nothing", and only because that
answer is the *restrictive* one: the file is left on disk, exactly as
for every other unparseable file, and the passes that key off ids
rather than content still run.

The write-safe :meth:`PurgeEngine._load_frontmatter` is deliberately
**not** widened. There a parse failure protects the operator's bytes
from a lossy rewrite, and there is no erasure for it to veto.
"""

VAULT_MARKER_RELPATH = ("00-Creek-Meta", "creek_config.yaml")
"""Relative path to the file that proves a directory is a Creek vault (GAP-003).

The vault marker is the per-vault config file ``creek init`` deploys at
``<vault>/00-Creek-Meta/creek_config.yaml``. It survives every purge
operation (``purge_vault`` preserves ``00-Creek-Meta/`` and the audit
log is mandated non-purgeable), so its absence is a reliable signal
that the supplied path was never ``creek init``-ed — most likely a
typo on ``--vault`` that points at an unrelated directory with
coincidentally numeric-prefix folders.
"""

_CLASSIFICATION_RESET_FIELDS: tuple[str, ...] = (
    "frequency",
    "wavelength",
    "voice",
)
"""Frontmatter fields wiped by ``purge_classifications``."""

_FILE_DELETING_OPERATIONS: frozenset[str] = frozenset(
    {"fragment", "source", "source-path", "daterange", "vault"},
)
"""Explicit allowlist of operations that remove files from disk.

Operations not listed here (currently only ``classifications``, which
resets metadata in place) record ``fragments_deleted=0`` regardless of
``fragments_affected``. Any new operation type added to this engine
must be classified explicitly here — defaulting unknown operations to
"deletes files" would risk over-counting deletions in audit entries.
"""

PURGED_MARKER = "[purged]"
"""GAP-004 replacement string for scrubbed fragment-ID references.

Used by :meth:`PurgeEngine._scrub_references` for both YAML frontmatter
list entries (e.g. ``source_fragments: [...]`` in drafts) and bare
fragment-ID mentions in body text. A literal placeholder leaves a
forensic trail — the user can see *that* a reference existed — without
exposing the original ID, satisfying the RTBF contract.

YAML flow-list side-effect (caveat): the marker is bracket-flavoured,
which is YAML-meaningful inside a flow sequence. A text-level
substitution turns ``source_fragments: [frag-A, frag-B]`` into
``source_fragments: [[purged], frag-B]``. A YAML parser then re-reads
the scrubbed entry as a one-element nested list (``["purged"]``),
yielding a ``list[str | list[str]]`` rather than the original flat
``list[str]``. The grep-based forensic intent is satisfied, but a
purged draft's ``source_fragments`` is therefore **not safe to
round-trip** through code that expects a flat string list. Today's
readers do not rely on that flat shape — the mining strategies build
``source_fragments`` from compiled-page provenance and raw-fragment
membership rather than re-reading a purged draft's frontmatter, and
the compiled-page loader validates provenance through a typed model.
If a future reader does parse purged ``source_fragments`` directly it
must tolerate nested-list entries (or skip purged drafts). The
regression test ``test_purged_marker_reparses_as_nested_list_in_yaml``
pins this trade-off so a future marker swap has to update the test and
this docstring together.
"""


def _warn_frontmatter_defeated_the_parser(path: Path, exc: BaseException) -> None:
    """Report a file whose frontmatter exhausted the YAML parser (#1455).

    Louder than the ordinary unparseable-file warning, and deliberately
    so: an ``OSError`` or a ``yaml.YAMLError`` is a hand-edited vault,
    while a ``RecursionError`` or a ``MemoryError`` means the *document*
    beat the parser — a nesting bomb or an alias bomb — which in a vault
    that ingests other people's exports is a thing somebody may have
    authored on purpose. The purge continues past it (the alternative
    was aborting the entire erasure), so the operator has to be told
    which file to go and look at.

    The path is named, matching the two sibling warnings in this module's
    match loaders; the exception **type** is named and its message is
    not, matching :meth:`PurgeEngine._run_audited` — a parser message
    quotes the offending source, which here is vault content.

    Args:
        path: The markdown file whose frontmatter would not parse.
        exc: The exhaustion that was caught, for its type name.
    """
    logger.warning(
        "Frontmatter of %s exhausted the YAML parser (%s) — a nesting or "
        "alias bomb, not an ordinary malformed file. It matches no purge "
        "criteria and is left untouched; the rest of the purge continues. "
        "Inspect the file by hand.",
        path,
        type(exc).__name__,
    )


def _str_list(value: object) -> list[str]:
    """Coerce a frontmatter list value into a list of strings.

    Args:
        value: Raw value from a :class:`frontmatter.Post` field.

    Returns:
        A list of strings; empty when *value* is not a list.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _read_bytes_for_match(path: Path) -> bytes:
    """Read *path* as raw bytes for a containment test, never for rewriting.

    Bytes rather than text on purpose: the caller is deciding whether a
    derived artifact still holds a purged fragment's content, and a
    ``read_text`` would raise on the first undecodable byte — turning a
    file that *might* hold the erased body into one the sweep skips
    silently. An unreadable file yields ``b""``, which matches nothing,
    and is logged so a skipped artifact stays distinguishable from a
    clean one.

    Args:
        path: File to read.

    Returns:
        The file's bytes, or ``b""`` when it cannot be read.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        # Type name only, never str(exc): an OSError message can quote a
        # title-derived filename, and through it vault content.
        logger.warning(
            "Skipping unreadable derived artifact during purge: %s (%s)",
            path,
            type(exc).__name__,
        )
        return b""


def _regular_files_under(entry: Path) -> list[str]:
    """List the regular files *entry* stands for, for a deletion record.

    A file stands for itself; a directory stands for every regular file
    it contains, recursively; anything else (an empty directory, a
    broken symlink) stands for nothing. That last case is the point of
    the helper: ``purge_vault`` deletes whole top-level entries, but the
    record it leaves behind has to name destroyed *content*, and an
    empty directory destroyed none (#1340).

    A symlink is never walked *through*. ``rglob`` will happily scandir
    a symlinked directory it is anchored on, so without the guard a
    vault holding ``01-Fragments/ext -> /home/someone/Documents`` had
    every file behind that link enumerated into the erasure record —
    naming out-of-vault paths in an audit log that survives the purge,
    and previewing deletions the apply run cannot make (``shutil.rmtree``
    refuses a symlinked directory outright). A symlink *to a file* is
    still named, because ``unlink`` really does destroy that alias; a
    symlink to a directory stands for nothing, because nothing behind it
    is this purge's to claim. The ``is_symlink`` test has to come first:
    ``is_file`` follows links and would answer for the target.

    Args:
        entry: A top-level entry the vault wipe is about to remove.

    Returns:
        Sorted path strings, empty when nothing readable is destroyed.
    """
    if entry.is_symlink():
        return [str(entry)] if entry.is_file() else []
    if entry.is_file():
        return [str(entry)]
    return sorted(str(path) for path in entry.rglob("*") if path.is_file())


def _contained_leaf(candidate: Path, resolved_vault: Path) -> bool:
    """Report whether a walked leaf stays inside the vault, logging a refusal.

    The module's single leaf verdict, shared by :func:`_contained_md_files`
    and :meth:`PurgeEngine._voice_profiles_quoting` so the two walks
    cannot drift into disagreeing about what "inside" means — the same
    argument :func:`creek._containment.escaping_child` records one level
    down.

    The warning names *candidate* exactly as walked. The resolved target
    is never logged: that is the exfiltration oracle #1087 closed, and a
    purge run that printed where a planted link pointed would hand back
    precisely the path it had just refused to read.

    Args:
        candidate: A path the walk produced, exactly as walked.
        resolved_vault: The vault root, already resolved once by the caller.

    Returns:
        ``True`` when the leaf may be read; ``False`` — refuse — when it is
        a symlink whose target is not inside the vault, or whose
        containment cannot be established at all.
    """
    if not escaping_child(candidate, resolved_vault):
        return True
    logger.warning(
        "Skipping a vault file whose symlink leaves the vault: %s",
        candidate,
    )
    return False


def _contained_md_files(root: Path, *, vault_path: Path) -> list[Path]:
    """List every ``.md`` file under *root* that does not leave the vault.

    The engine's one walk primitive (#1454), replacing every
    ``rglob("*.md")`` in the module — the fragment census
    (:meth:`PurgeEngine._list_fragment_files`), the vault-wide reference
    scrub (:meth:`PurgeEngine._list_vault_md_files`), the thread/eddy
    count update (:meth:`PurgeEngine._decrement_counts`) and the voice
    lexicon sweep (:meth:`PurgeEngine._voice_lexicon_notes`). Until this
    existed those were the module's unguarded walks, while every
    pointer-following path beside them (``_resolve_pointer_in_vault``,
    ``_in_any_staging_root``, ``_contained_voice_artifact``) was already
    fail-closed. The count update is why the walks kept mattering more
    than they looked: it is the one whose match ends in a
    ``write_bytes``, which follows a link and writes the target, so the
    "purge only ever ``unlink``s, and ``unlink`` drops the alias" reading
    of #1454 was never true of the whole module.

    New callers belong here rather than beside here. Three separate
    misses, each declaring the population closed, is what a per-site
    check costs.

    Two link shapes, and the glob mishandled both:

    * A symlinked **directory**. ``rglob`` scandirs one it is anchored on
      — on 3.11/3.12. Since gh-77609, 3.13's ``rglob`` does not, so the
      same planted link changed what purge read from one interpreter to
      the next. ``os.walk(..., followlinks=False)`` answers identically on
      every supported version, and is the idiom
      :func:`creek._containment.inspect_tree` already uses.
    * A symlinked ``.md`` **leaf**, judged by the single shared predicate
      :func:`creek._containment.escaping_child` rather than a fourth
      hand-rolled copy of "is this inside?".

    Fail-closed, per that module's policy: an unprovable containment — a
    cycle, an unreadable component — is an escape, not an admission.

    Containment is judged against the VAULT root, not *root*, so an
    ordinary intra-vault alias such as ``01-Fragments/a.md ->
    09-Reference/b.md`` is still yielded. What is refused is leaving the
    vault, not being a link.

    ``name.endswith(".md")`` matches ``rglob("*.md")``'s case sensitivity
    exactly — the same argument
    :func:`creek.ingest.markdown._enumerate_markdown_paths` records:
    lowercasing here would silently widen what purge claims to erase.

    Args:
        root: The subtree to walk.
        vault_path: The vault root, as the engine holds it.

    Returns:
        Sorted paths, named as walked and never resolved.
    """
    # Resolved exactly once, above the walk: the policy is
    # resolve-the-root and ``lstat``-the-leaf.
    resolved_vault = vault_path.resolve(strict=False)
    if escaping_child(root, resolved_vault):
        # Checked before the walk, because ``os.walk`` would follow a
        # symlinked *root* even with ``followlinks=False``.
        logger.warning("Refusing a purge walk root that leaves the vault: %s", root)
        return []
    kept: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            # ``followlinks=False`` already stops the walk descending an
            # escaping directory link, so this changes nothing about what
            # is returned — it only stops the refusal being silent, which
            # is what let a directory link out of the vault sit in a
            # tree with no trace in the run's log. The same entries
            # :func:`creek._containment.inspect_tree` reports.
            candidate = Path(dirpath) / name
            if escaping_child(candidate, resolved_vault):
                logger.warning(
                    "Not descending a vault directory whose symlink leaves "
                    "the vault: %s",
                    candidate,
                )
        for name in filenames:
            if not name.endswith(".md"):
                continue
            candidate = Path(dirpath) / name
            if _contained_leaf(candidate, resolved_vault):
                kept.append(candidate)
    return sorted(kept)


_MAX_LINK_HOPS: int = 40
"""How many symlink hops :func:`_chain_enters_wiped_content` will follow.

A symlink loop has no end, and this walk cannot lean on the kernel's
``ELOOP`` because it never dereferences anything — it reads one link at
a time. The cap is the termination condition, and exhausting it answers
"not broken", the fail-open direction the predicate's caller documents.
Forty is the same order as the kernel's own limit (Linux allows 40,
macOS 32), so no chain a filesystem can resolve is cut short by it.
"""


def _same_directory(left: Path, right: Path) -> bool:
    """Report whether two paths name the same directory on disk.

    ``Path.samefile`` compares device and inode, so it sees through a
    symlinked prefix, a ``..`` that has not been normalised, and — the
    case that motivated it (#1562) — a filesystem that treats
    ``01-fragments`` and ``01-Fragments`` as one directory while a
    string comparison treats them as two.

    Args:
        left: One path.
        right: The other path.

    Returns:
        ``True`` when both exist and are the same directory entry, or —
        when either cannot be stat'ed — when they are textually equal.
    """
    try:
        return left.samefile(right)
    except OSError:
        return left == right


def _inside_a_wiped_folder(path: Path, roots: list[Path]) -> bool:
    """Report whether *path* names something strictly inside a content folder.

    Every ancestor of *path* is compared, which is what makes an
    un-normalised target work: ``01-Fragments/Journal/sub/../../..``
    climbs back out of the vault, so its *destination* is untouched by
    the wipe, but resolving it needs ``sub`` to exist and the wipe
    removes it. Naming any component the wipe destroys is enough.

    The folder root itself is deliberately not a match: the wipe empties
    the ten content folders without removing them, so a link *to*
    ``01-Fragments`` outlives the purge while a link *into* it does not.

    Args:
        path: A symlink target, absolute but not necessarily normalised.
        roots: The ten vault content folders.

    Returns:
        ``True`` when some proper ancestor of *path* is a content folder.
    """
    return any(
        _same_directory(ancestor, root) for ancestor in path.parents for root in roots
    )


def _chain_enters_wiped_content(link: Path, roots: list[Path]) -> bool:
    """Report whether resolving *link* passes through a wiped content folder.

    Walks the chain one hop at a time with ``os.readlink`` rather than
    asking ``Path.resolve()`` for the destination. ``resolve()``
    collapses every hop and every ``..`` into a single final path, which
    answers "survives" for three chains the content wipe really does
    break (#1562): one reached through an intermediate alias that lives
    inside a content folder, one whose target *path* traverses a content
    directory before climbing back out, and one naming a content folder
    in a case only the filesystem accepts.

    Args:
        link: A symlink under ``00-Creek-Meta/``.
        roots: The ten vault content folders.

    Returns:
        ``True`` when any hop of the chain names something the wipe
        destroys; ``False`` on an ``OSError`` or an exhausted hop cap,
        because this predicate only ever widens what the sweep destroys.
    """
    current = link
    for _ in range(_MAX_LINK_HOPS):
        try:
            if not current.is_symlink():
                return False
            target = os.readlink(current)
        except OSError:
            return False
        current = current.parent / target
        if _inside_a_wiped_folder(current, roots):
            return True
    return False


_WIKILINK_RE: re.Pattern[bytes] = re.compile(rb"\[\[([^\[\]]*)\]\]")
"""Any wikilink at all, capturing everything between the brackets.

Stage one of a two-stage matcher (#903). A single regex built from the
purged title cannot express the relation Obsidian resolves links by:
``re.escape(title)`` is exact-case and misses ``[[secret title]]``,
while ``re.IGNORECASE`` is neither ``str.casefold`` (they disagree on
``ß``/``SS`` and on the Turkish dotted I) nor the exact-before-folded
priority :meth:`creek.vault.links.LinkIndex.resolve` applies. So the
regex only *finds* links; :class:`_WikilinkScrub` decides about them.

Compiled against **bytes** because the whole scrub pipeline is (#948).
"""


_MDLINK_RE: re.Pattern[bytes] = re.compile(rb"\[[^\]]*\]\((?!#)([^)]*)\)")
"""A markdown link, capturing only its target.

The bytes mirror of :data:`creek.clean.hygiene._RELATIVE_LINK_PATTERN`,
which is what ``BrokenLinkScanner`` already counts a fragment's outgoing
links with — so the vault's link graph has always regarded
``[text](Secret Title.md)`` as a link to that page while the purge scrub
did not (#1622). The display text is matched but deliberately *not*
captured: only the target decides, and removing the whole link takes the
text with it either way.

``(?!#)`` drops same-file anchors (``[text](#Heading)``), which name no
file to resolve, exactly as the hygiene pattern does.
"""


def _remove_matching_links(
    pattern: re.Pattern[bytes],
    decides: Callable[[bytes], bool],
    data: bytes,
) -> tuple[bytes, int]:
    """Remove every link *pattern* finds whose captured target *decides* accepts.

    Shared by both link syntaxes so the count means the same thing on
    each: links actually removed, never links merely inspected —
    ``re.subn`` would report the latter, since every link goes through
    the replacement callback.

    Args:
        pattern: A bytes regex whose group 1 is the link target.
        decides: Predicate on those target bytes.
        data: One file's raw bytes.

    Returns:
        A ``(scrubbed_bytes, links_removed)`` pair.
    """
    removed = 0

    def _replace(match: re.Match[bytes]) -> bytes:
        """Blank a link that names the purged page, else keep it verbatim."""
        nonlocal removed
        if decides(match.group(1)):
            removed += 1
            return b""
        return match.group(0)

    return pattern.sub(_replace, data), removed


_LINK_TITLE_RE: re.Pattern[str] = re.compile(r"^(?P<url>.*?)\s+(?:\"[^\"]*\"|'[^']*')$")
"""A destination followed by a CommonMark link title, split at the title.

Whether ``a b`` is one path or a destination plus a title is not a
matter of taste: CommonMark only ends the destination at whitespace
when what follows is a **title**, and a title is quote- or
paren-delimited. ``Alpha Notes.md`` therefore has exactly one reading —
a path — and splitting it anyway is what destroyed a link to a
surviving page (see :func:`_link_target_urls`).

Paren-delimited titles are deliberately absent: :data:`_MDLINK_RE` stops
its capture at the first ``)``, so a ``(title)`` can never reach this
pattern intact and a branch for it would be unreachable.

The quotes are anchored to the END of the target, and ``url`` is
non-greedy, so the split lands at the whitespace before the title
rather than at the first whitespace anywhere. Splitting at the first
whitespace only worked when the path had no internal space of its own:
``Secret Title.md "note"`` cut into ``Secret`` and ``Title.md "note"``,
the second half failed to look like a title, the whole string was kept
as a single reading, and — because it ends in a quote rather than
``.md`` — it never reduced to a page name. The link survived the purge
while the audit certified it complete, which is the leak #1622 exists
to close, reached through a different target shape.
"""


def _link_target_urls(target: str) -> tuple[str, ...]:
    """Return the URLs a markdown link target could be read as.

    Usually one. A second reading is offered only for the shape that
    genuinely has two: CommonMark ends the destination at whitespace
    when a ``"title"`` follows, so ``path.md "note"`` links to
    ``path.md``, while Obsidian writes ``[t](Secret Title.md)`` and
    means the whole thing. Reading only the first token there would
    leave the private title on disk, so both readings are offered and
    the caller removes the link if *either* resolves to the purged page
    (#1622).

    The second reading is **conditional on the remainder actually being
    a title**, and that condition is load-bearing. Offered
    unconditionally, the first-token reading resolves
    ``[read them](Alpha Notes.md)`` to the page ``Alpha`` and deletes an
    operator's live link to a page this purge never touched — the
    over-match direction #903 exists to prevent, on a target shape
    (an unencoded space) that this very function documents Obsidian as
    writing routinely. :func:`creek.clean.hygiene._extract_relative_links`
    does take the first token unconditionally, but it only *counts*
    links; here the same guess destroys content.

    A ``<...>``-wrapped target is unambiguous — the angle brackets exist
    to let a URL contain spaces — so it yields exactly one reading.

    Args:
        target: The decoded text between ``(`` and ``)``.

    Returns:
        One or two candidate URLs, or none for an empty target.
    """
    raw = target.strip()
    if not raw:
        return ()
    if raw.startswith("<"):
        return (raw[1:].partition(">")[0],)
    titled = _LINK_TITLE_RE.match(raw)
    if titled is None:
        return (raw,)
    return (raw, titled.group("url"))


def _link_target_page_name(url: str) -> str:
    """Reduce a markdown link URL to the page name it resolves to.

    A URL is a *path*; ``folded_names`` holds page *names*. Four things
    stand between them, and every one of them is a shape Obsidian
    actually writes (#1622): an ``#anchor`` or ``?query`` suffix, a
    percent-encoded space, a folder prefix, and the ``.md`` extension —
    which Obsidian omits as often as it writes it.

    Percent-decoding is the one that matters most in practice: Obsidian
    writes ``Secret%20Title.md`` whenever it generates the link itself,
    so a scrub that skipped :func:`urllib.parse.unquote` would miss the
    single most common real-world spelling.

    The extension is stripped by name rather than by
    :attr:`~pathlib.PurePosixPath.suffix`, because a page titled
    ``Notes v1.2`` linked without its extension would otherwise be
    reduced to ``Notes v1`` and never match.

    Args:
        url: One candidate URL from :func:`_link_target_urls`.

    Returns:
        The exact-case page name, or ``""`` for a target that names no
        local page — an external URL, or nothing at all.
    """
    path = url.partition("#")[0].partition("?")[0].strip()
    if not path or is_external_target(path):
        return ""
    name = PurePosixPath(unquote(path)).name
    if name.casefold().endswith(".md"):
        name = name[: -len(".md")]
    return name


@dataclass(frozen=True)
class _WikilinkScrub:
    """Decide, link by link, whether a wikilink names the purged page.

    Stage two of the #903 matcher, and the single place the decision is
    auditable. It reproduces
    :meth:`creek.vault.links.LinkIndex.resolve` rather than approximating
    it: the ``#heading``/``|alias`` suffixes are stripped exactly as that
    method documents its caller doing, an **exact-case** spelling some
    surviving page claims wins outright, and only then does the
    ``casefold()`` fallback apply.

    That exact-first guard is what keeps the fix from over-matching. Two
    vault pages may differ only in case, and when one of them is being
    purged, ``[[ALPHA]]`` names the survivor while ``[[Alpha]]`` names
    the target. A case-blind scrub deletes an operator's live link to a
    page this purge never touched — the mirror failure of leaving the
    private title behind, and just as unacceptable on a compliance path.

    Attributes:
        folded_names: Case-folded names the purged page is linkable by —
            its declared title, its filename stem, *and* each of its
            declared ``aliases``, because this vault's paths are
            title-derived and a link written by the filename leaks the
            same private string (#903), while an alias is a name the
            operator chose precisely so it would be written instead of
            the title (#1622).
        protected: Exact-case spellings that some **surviving** page
            claims *and the purged page does not*. A link spelled that
            way resolves to the survivor, so it is left alone whatever
            its fold. The purged page's own spellings are excluded by
            :meth:`PurgeEngine._build_wikilink_scrub`: sheltering those
            would let any doomed sibling, stub or staging file claiming
            the same string switch the primary scrub off.
    """

    folded_names: frozenset[str]
    protected: frozenset[str]

    def _claims(self, name: str) -> bool:
        """Report whether *name* is a spelling that resolves to the purged page.

        The single decision both link syntaxes route through, so the
        exact-before-folded priority is stated once and cannot drift
        between them (#1622). A second, case-blind copy of it on the
        markdown path would delete the very link
        :meth:`PurgeEngine._surviving_claimants` exists to shelter.

        Args:
            name: An exact-case page name a link resolved to, already
                stripped of any suffix its syntax carries.

        Returns:
            ``True`` when a link spelled that way should be removed.
        """
        if not name or name in self.protected:
            return False
        return name.casefold() in self.folded_names

    def _names_the_purged_page(self, target: bytes) -> bool:
        """Report whether a wikilink target resolves to the purged page.

        Only the captured *target* is decoded, and a failure to decode
        it is answered ``False`` rather than repaired (#948). A link
        target that is not valid UTF-8 cannot equal a title, which is a
        ``str``; refusing it here is what lets the surrounding rewrite
        stay byte-exact without ever constructing a U+FFFD.

        Args:
            target: The raw bytes between ``[[`` and ``]]``.

        Returns:
            ``True`` when this link should be removed.
        """
        try:
            decoded = target.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return self._claims(decoded.split("|", 1)[0].split("#", 1)[0].strip())

    def _targets_the_purged_page(self, target: bytes) -> bool:
        """Report whether a markdown link's *URL* resolves to the purged page.

        The URL, never the display text. ``[Secret Title](https://…)``
        points at something the purge did not touch, and rewriting it
        would destroy the operator's own content while leaving the same
        private string on disk as prose — which is exactly the shape
        :func:`creek.clean.hygiene._extract_relative_links` already
        discards the text of (#1622).

        Decoding follows #948's discipline unchanged: a target that is
        not valid UTF-8 cannot equal a page name, so it is refused
        rather than repaired.

        Args:
            target: The raw bytes between ``(`` and ``)``.

        Returns:
            ``True`` when this link should be removed.
        """
        try:
            decoded = target.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return any(
            self._claims(_link_target_page_name(url))
            for url in _link_target_urls(decoded)
        )

    def apply(self, data: bytes) -> tuple[bytes, int]:
        """Remove every wikilink naming the purged page.

        Args:
            data: One file's raw bytes.

        Returns:
            A ``(scrubbed_bytes, wikilinks_removed)`` pair.
        """
        return _remove_matching_links(_WIKILINK_RE, self._names_the_purged_page, data)

    def apply_markdown(self, data: bytes) -> tuple[bytes, int]:
        """Remove every markdown link targeting the purged page.

        Args:
            data: One file's raw bytes.

        Returns:
            A ``(scrubbed_bytes, markdown_links_removed)`` pair.
        """
        return _remove_matching_links(_MDLINK_RE, self._targets_the_purged_page, data)


_OPENING_FENCE_RE: re.Pattern[bytes] = re.compile(rb"---[ \t]*\r?\n")
"""The ``---`` that opens a frontmatter header, matched at offset zero."""

_CLOSING_FENCE_RE: re.Pattern[bytes] = re.compile(rb"^---[ \t]*\r?$", re.MULTILINE)
"""The ``---`` that closes it. Searched from the end of the opening fence."""

_COUNT_SCALAR_RE: re.Pattern[bytes] = re.compile(
    rb"(?m)^(fragment_count:[ \t]*)(['\"]?)(\d+)(['\"]?)([^\n]*)$"
)
"""A top-level ``fragment_count`` holding an integer, in five pieces.

The key and its spacing, an optional opening quote, the digits, an
optional closing quote, and whatever follows — a trailing comment,
trailing whitespace, and the ``\r`` of a CRLF line ending. Only the
digits are replaced, so the other four come back byte-identical.

Matching the fifth group is not the same as accepting it. The digit
run is greedy but not anchored to the end of the scalar, so
``fragment_count: 3.5`` matches with ``3`` as the digits and ``.5`` as
the trailer — and splicing that writes ``2.5``, corrupting a value the
function was supposed to decline. :data:`_INERT_TRAILER_RE` is what
makes the digit run *the whole scalar*: the trailer is accepted only
when it carries no value of its own.

The quote characters are captured separately rather than swallowed into
the value so they can be written back verbatim: a quoted
``fragment_count: '3'`` is a YAML *string*, and rewriting it as a bare
``2`` would change the scalar's type as a side effect of decrementing
it — an edit nobody asked for, in the one function whose whole contract
is that it makes no such edit. Column zero is required: an indented
``fragment_count`` belongs to a nested mapping and is not this file's own.
"""


_INERT_TRAILER_RE: re.Pattern[bytes] = re.compile(rb"[ \t]*(?:#[^\n]*)?\r?")
"""Everything a ``fragment_count`` line may carry after its value.

Optional spacing, an optional trailing comment, and the ``\r`` of a CRLF
line ending — none of which is part of the value. Anything else means the
digits matched only a *prefix* of the scalar (``3.5``, ``3abc``), and the
splice must decline rather than rewrite a number it did not fully parse.
"""


def _splice_fragment_count(data: bytes, new_count: int) -> bytes | None:
    """Return *data* with its header ``fragment_count`` set to *new_count*.

    A byte-level splice of one scalar, replacing the
    ``frontmatter.dumps`` round trip this path used to perform (#949).
    That round trip rewrote the operator's whole header on every purge
    that touched the file: PyYAML drops comments, alphabetises keys,
    normalises quoting and strips trailing whitespace, so decrementing
    one integer silently reformatted metadata the purge was never asked
    about. It also could not run at all on a file whose *body* is not
    valid UTF-8, which left the count wrong there instead (#910).

    Nothing is ever appended, deleted, or retyped. Quoting is part of
    that promise, not an incidental detail: a quoted ``'3'`` comes back
    as a quoted ``'2'``, because dropping the quotes would silently turn
    a YAML string into an integer. When the header holds no
    spliceable ``fragment_count`` — the key is absent, or it carries a
    list, a null, a multi-line scalar, a lopsided pair of quotes, or a
    value the digits only partly cover such as ``3.5`` —
    this returns ``None`` rather than guessing: inserting a key beside a
    value this function did not understand risks a duplicate key, and a
    corrupt header on a thread file is a worse outcome than a stale
    count. The caller falls back to a full reserialisation for those,
    which is what wrote them in the first place.

    Args:
        data: The file's current bytes.
        new_count: The value to write.

    Returns:
        The spliced bytes, or ``None`` when *data* has no frontmatter
        header, or no ``fragment_count`` at its top level whose value is
        an integer *and nothing else*.
    """
    opening = _OPENING_FENCE_RE.match(data)
    if opening is None:
        return None
    closing = _CLOSING_FENCE_RE.search(data, opening.end())
    if closing is None:
        return None
    header, body = data[: closing.start()], data[closing.start() :]
    match = _COUNT_SCALAR_RE.search(header)
    if match is None:
        return None
    opening_quote, closing_quote = match.group(2), match.group(4)
    if opening_quote != closing_quote:
        return None
    trailer = match.group(5)
    if _INERT_TRAILER_RE.fullmatch(trailer) is None:
        return None
    spliced = (
        match.group(1)
        + opening_quote
        + str(new_count).encode()
        + closing_quote
        + match.group(5)
    )
    return header[: match.start()] + spliced + header[match.end() :] + body


def _apply_scrubs(
    data: bytes,
    wiki_scrub: _WikilinkScrub | None,
    prov_pattern: re.Pattern[bytes] | None,
) -> tuple[bytes, int, int, int]:
    """Apply the link and provenance substitutions to one file's bytes.

    Pure and mode-blind: the dry-run/apply distinction lives entirely in
    where :meth:`PurgeEngine._scrub_one_file` gets these bytes from and
    what it does with the result.

    Byte-native throughout (#948). Decoding first would be lossy on a
    file that is not valid UTF-8 — the scrub used to skip those
    entirely, leaving the purged title and ID standing — and lossy in a
    quieter way on one that *is*: ``read_text`` translates newlines, so
    removing one wikilink from a CRLF note rewrote every line ending in
    it. There is one path here, and it moves no byte it was not asked
    to.

    Both link syntaxes are driven by the one matcher, and the markdown
    pass runs *before* the provenance one for the same reason the
    wiki-link pass does: the provenance substitution rewrites a
    fragment ID into ``[purged]``, and a link whose target is that ID
    would stop being recognisable as a link to the purged page (#1622).

    Args:
        data: The file's current bytes.
        wiki_scrub: Link matcher, or ``None`` to skip both link passes.
        prov_pattern: Fragment-ID regex, or ``None`` to skip that pass.

    Returns:
        A ``(scrubbed_bytes, wikilinks_removed, markdown_links_removed,
        provenance_scrubbed)`` quadruple.
    """
    wiki_count = md_count = prov_count = 0
    if wiki_scrub is not None:
        data, wiki_count = wiki_scrub.apply(data)
        data, md_count = wiki_scrub.apply_markdown(data)
    if prov_pattern is not None:
        # NOTE: PURGED_MARKER is bracket-flavoured, so substituting it
        # inside a YAML flow sequence (source_fragments: [a, b]) nests
        # the scrubbed entry on re-parse — see PURGED_MARKER.
        data, prov_count = prov_pattern.subn(PURGED_MARKER.encode(), data)
    return data, wiki_count, md_count, prov_count


@dataclass(frozen=True)
class _VaultFragmentCensus:
    """What a full-vault wipe is about to destroy, read before it runs.

    Carried as a value rather than written straight onto a
    :class:`PurgeResult` so that :meth:`PurgeEngine.purge_vault` can read
    the vault *before* the wipe while committing the numbers *after* it —
    see that method for why an abort must not leave the audit log
    certifying an erasure that never happened.

    Attributes:
        file_count: Every ``.md`` file under ``01-Fragments/``, including
            those carrying no usable id.
        fragment_ids: The subset of those files that declared a string
            ``id`` in frontmatter.
    """

    file_count: int
    fragment_ids: list[str]


class PurgeResult(BaseModel):
    """Outcome of a single purge operation.

    Attributes:
        operation: Name of the operation (``fragment``, ``source``, ...).
        target: The purge target (fragment id, source type, date range, ...).
        criteria: Structured representation of *target* used for the
            audit log (e.g. ``{"fragment_id": "frag-abc"}``).
        affected_fragment_ids: IDs of fragments touched by the operation.
        dry_run: Whether deletions were only previewed.
        deleted_files: Paths of the regular **files** deleted (or that
            would be deleted). Never a directory: a removed directory is
            represented by the files it contained, so an empty one
            contributes no entry at all while still being removed from
            disk. A deletion record for a right-to-be-forgotten request
            has to name the content that was destroyed, and a directory
            path names none of it (#1340).
        fragments_affected: Number of fragments directly affected.
        wikilinks_removed: Number of wiki-link references scrubbed.
        markdown_links_removed: Number of ``[text](target)`` markdown
            links removed because their **target** resolved to a purged
            page (#1622). Counted apart from
            :attr:`wikilinks_removed` rather than added into it: that
            field's name, its docstring, the CLI line that prints it and
            the ``references_scrubbed`` key it is audited under all say
            wiki-link, and three surfaces would start saying something
            untrue. Matching is on the target only — an external link
            whose display text happens to spell the purged title is left
            byte-identical, as are prose mentions of the title, which
            this scrub has never been in the business of removing.
        threads_updated: Thread files whose metadata changed.
        eddies_updated: Eddy files whose metadata changed.
        classifications_reset: Fragments whose classifications were wiped.
        embeddings_removed: Real count of embedding cache rows that were
            removed (or would be removed in a dry run). Zero for
            metadata-only operations and for runs where the cache had
            no matching rows (GAP-001).
        provenance_scrubbed: Number of fragment-ID mentions replaced
            with ``[purged]`` across the vault (GAP-004). Counts YAML
            list entries (e.g. ``source_fragments``) and bare body-text
            mentions in every ``.md`` file; the wiki-link count stays
            on :attr:`wikilinks_removed`.
        intimate_stubs_removed: Number of intimate-body stub files
            deleted under ``10-Liminal/Compost/intimate-stubs/`` because
            a purged note carried a ``saved_from.intimate_body_pointer``
            at them (GAP-012). Counted-only in a dry run.
        journal_staged_removed: Number of staged Adepthood source files
            deleted under ``00-Creek-Meta/adepthood/journal/`` or
            ``00-Creek-Meta/adepthood/uploads/`` because the fragment
            they produced was purged (issues #845, #1023). The staged
            file holds the entry's full plaintext body or the uploaded
            document's bytes, so leaving it behind would defeat the
            RTBF request. Counted-only in a dry run. The field keeps its
            journal-era name because it is serialised into the
            append-only ``purge.jsonl``, where a rename would break
            every existing log.
        voice_artifacts_removed: Number of derived ``07-Voice/`` notes
            deleted because they carried the purged fragment's own
            content (#1211) — the ``Register-Samples`` copy of its file,
            the ``<register>-profile.md`` quoting its body, and the
            ``Lexicon`` notes quoting its sentences. Counted-only in a
            dry run. Deliberately **not** appended to
            :attr:`deleted_files` and never inflates
            :attr:`fragments_affected`: these are derived copies, not
            fragments, and the same rule already governs
            :attr:`journal_staged_removed`.
        ledger_rows_removed: Number of ingest-ledger **rows** physically
            erased from ``00-Creek-Meta/State/ingest/*.jsonl`` because
            they named a purged fragment, or named a source unit a
            purged fragment came from (#1453). Rows, not files: one
            source unit accumulates an appended row per ingest, so a
            single erased fragment routinely takes several rows with it,
            and the count an operator needs is of *mappings destroyed*.
            Set by the scoped purges only — a whole-vault purge destroys
            the ledger files outright, where they are counted as files
            on :attr:`meta_artifacts_removed` instead of being counted
            twice.
        meta_artifacts_removed: Number of **files** destroyed by the
            deny-by-default sweep of ``00-Creek-Meta/`` during a
            whole-vault purge (#1453). Files, not rows: the sweep does
            not read what it deletes, and cannot — its whole premise is
            that it need not understand a future artifact to refuse to
            let it survive an erasure. Zero for every scoped purge,
            which sweeps nothing.
        embeddings_cache_undeleted: ``True`` when the embeddings cache was
            still on disk after the erasure tried to remove it — a real
            shortfall, because the cache holds vectors this file's own module
            docstring calls partially invertible back to the purged content.
            Distinct from "the cache could not be *parsed*", which is not a
            shortfall: an unreadable cache that is then deleted took its rows
            with it.
        voice_body_undecodable: Ids of the purged fragments whose body
            could not be decoded as strict UTF-8, so the content-keyed
            ``<register>-profile.md`` pass could not run for them
            (#1211, hazard #910). Non-empty means **the erasure is
            incomplete**: a profile may still quote that fragment. The
            operation's audit ``outcome`` line is downgraded to
            ``status="partial"`` accordingly, and the CLI says so. Ids
            only — never a path and never body text.
    """

    operation: str
    target: str
    criteria: dict[str, object] = Field(default_factory=dict)
    affected_fragment_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False
    deleted_files: list[str] = Field(default_factory=list)
    fragments_affected: int = 0
    wikilinks_removed: int = 0
    markdown_links_removed: int = 0
    threads_updated: int = 0
    eddies_updated: int = 0
    classifications_reset: int = 0
    embeddings_removed: int = 0
    provenance_scrubbed: int = 0
    intimate_stubs_removed: int = 0
    journal_staged_removed: int = 0
    voice_artifacts_removed: int = 0
    ledger_rows_removed: int = 0
    meta_artifacts_removed: int = 0
    voice_body_undecodable: list[str] = Field(default_factory=list)
    embeddings_cache_undeleted: bool = False

    @property
    def outcome_status(self) -> PurgeOutcomeStatus:
        """Whether this result describes a complete erasure or a partial one.

        "The operation finished" and "everything it promised to erase is
        gone" are different claims. A body that returned normally can
        still have left a derived copy behind: an undecodable fragment
        body skips the content-keyed voice sweep, so a
        ``07-Voice/<register>-profile.md`` may still quote it.

        This property is the single definition of that distinction for
        the two surfaces that report a *verdict* — the audit ``outcome``
        line (:meth:`PurgeEngine._run_audited`) and the MCP tool payload
        (#1246), which disagreed for as long as each carried its own
        copy of the predicate. The CLI reports the same shortfall by
        naming the ids straight off :attr:`voice_body_undecodable`,
        because a human reading it needs *which fragments*, not a
        one-word verdict.

        A raising body is *also* partial, but that verdict cannot be
        read off a result — the exception aborts before the accumulator
        is complete — so :meth:`PurgeEngine._run_audited` records it
        directly. This property describes results that survived to be
        returned.

        Returns:
            ``"partial"`` when any fragment is named in
            :attr:`voice_body_undecodable`, or when
            :attr:`embeddings_cache_undeleted` is set; otherwise
            ``"complete"``.
        """
        # Two shortfalls, one verdict. The embeddings arm was missed when
        # #1480 stopped a corrupt cache vetoing the purge: the handler logged
        # "this erasure is PARTIAL" while this property still answered
        # "complete", so the audit line and the MCP payload both certified an
        # erasure that had provably left an artifact on disk — the same
        # over-claim #1481 fixed for staged files, in the sibling artifact.
        #
        # Note the contrast with the unparseable-frontmatter arm, which is
        # deliberately NOT a shortfall: there the fragment file is destroyed
        # whether or not its frontmatter could be read, so "complete" is true.
        # Here the file is still there.
        if self.voice_body_undecodable or self.embeddings_cache_undeleted:
            return "partial"
        return "complete"


class PurgeEngine:
    """Execute right-to-be-forgotten deletions against a vault.

    Every public method returns a :class:`PurgeResult` summarising what
    changed (or would change, when ``dry_run=True``) and appends an
    entry to the audit log.

    Args:
        vault_path: Path to the Obsidian vault root.
        dry_run: When ``True``, no filesystem writes occur; results
            describe the hypothetical outcome.
    """

    def __init__(self, vault_path: Path, *, dry_run: bool = False) -> None:
        """Initialise the engine with the target vault path.

        Args:
            vault_path: Path to the Obsidian vault root.
            dry_run: If ``True``, preview changes without applying them.
        """
        self.vault_path = vault_path
        self.dry_run = dry_run
        self.audit_log = PurgeAuditLog(vault_path)
        # Re-created per operation by :meth:`_run_audited`; built here so
        # the attribute always exists, including for the direct callers
        # of the private helpers.
        self._ledger = DryRunLedger()

    # -- Fragment purge ---------------------------------------------------

    def purge_fragment(self, fragment_id: str) -> PurgeResult:
        """Delete a fragment and remove every reference to it.

        Searches ``01-Fragments/`` for a markdown file whose frontmatter
        ``id`` matches ``fragment_id``. If found, the file is deleted,
        all wiki-links pointing at its title are scrubbed from every
        markdown file in the vault, and the ``fragment_count`` on any
        thread/eddy listed in its frontmatter is decremented.

        Args:
            fragment_id: The fragment's unique ID (e.g. ``frag-abc123``).

        Returns:
            A :class:`PurgeResult` describing the deletion.
        """
        result = PurgeResult(
            operation="fragment",
            target=fragment_id,
            criteria={"fragment_id": fragment_id},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the per-fragment destructive ops, mutating ``result``."""
            frag_file, post = self._find_fragment_by_id(fragment_id)
            if frag_file is None or post is None:
                return
            title = str(post.get("title", frag_file.stem))
            thread_ids = _str_list(post.get("threads"))
            eddy_ids = _str_list(post.get("eddies"))
            result.deleted_files.append(str(frag_file))
            result.affected_fragment_ids.append(fragment_id)
            result.fragments_affected = 1
            # Strictly before the scrub: the lexicon's only link back to
            # this fragment is a ``[[<id>]]`` wikilink, and the scrub
            # rewrites it to ``[[[purged]]]``. Swept afterwards, the
            # sweep would be blind to exactly the notes quoting the body.
            self._purge_voice_artifacts(fragment_id, frag_file, result)
            # One vault walk applies both the wiki-link and provenance scrubs.
            wiki_count, md_count, prov_count = self._scrub_references(
                title=title,
                fragment_id=fragment_id,
                exclude=frag_file,
            )
            result.wikilinks_removed = wiki_count
            result.markdown_links_removed = md_count
            result.provenance_scrubbed += prov_count
            result.threads_updated = self._decrement_counts(
                "02-Threads",
                thread_ids,
            )
            result.eddies_updated = self._decrement_counts(
                "03-Eddies",
                eddy_ids,
            )
            self._purge_intimate_stub(post, result)
            self._purge_staged_source_entry(post, result)
            if self.dry_run:
                self._ledger.mark_removed(frag_file)
            else:
                frag_file.unlink()
            self._purge_scoped_tail(result)

        return self._run_audited(result, body)

    # -- Source purge -----------------------------------------------------

    def purge_source(self, source_type: str) -> PurgeResult:
        """Delete every fragment ingested from a given source platform.

        Args:
            source_type: Source platform identifier
                (e.g. ``claude``, ``discord``).

        Returns:
            A :class:`PurgeResult` summarising the deletions.
        """
        result = PurgeResult(
            operation="source",
            target=source_type,
            criteria={"source_type": source_type},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the per-source destructive ops, mutating ``result``."""
            matches = self._fragments_from_source(source_type)
            for frag_file, post in matches:
                self._purge_single(frag_file, post, result)
            self._purge_scoped_tail(result)

        return self._run_audited(result, body)

    def count_fragments_from_source(self, source_type: str) -> int:
        """Count fragments currently attributed to a source platform.

        Args:
            source_type: Source platform identifier.

        Returns:
            Number of matching fragment files.
        """
        return len(self._fragments_from_source(source_type))

    def purge_source_path(
        self,
        source_path: str,
        *,
        match: str = "exact",
    ) -> PurgeResult:
        """Delete every fragment whose ``source.original_file`` matches (INC-008).

        Args:
            source_path: The path (or substring / regex) to match against
                each fragment's ``source.original_file``.
            match: ``"exact"`` (full-string equality, default),
                ``"substring"`` (Python ``in``), or ``"regex"``
                (re.search). An invalid regex raises ``ValueError``
                fast rather than silently mismatching.

        Returns:
            A :class:`PurgeResult` summarising the deletions and
            recording the match mode in its criteria.

        Raises:
            ValueError: When ``match`` is unrecognised, or when
                ``match="regex"`` and ``source_path`` is not a valid
                regex.
        """
        result = PurgeResult(
            operation="source-path",
            target=source_path,
            criteria={"source_path": source_path, "match": match},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the source-path destructive ops, mutating ``result``."""
            matches = self._fragments_from_source_path(source_path, match=match)
            for frag_file, post in matches:
                self._purge_single(frag_file, post, result)
            self._purge_scoped_tail(result)

        return self._run_audited(result, body)

    def count_fragments_from_source_path(
        self,
        source_path: str,
        *,
        match: str = "exact",
    ) -> int:
        """Count fragments whose ``source.original_file`` matches (INC-008).

        Args:
            source_path: The path / substring / regex to match.
            match: One of ``"exact"`` / ``"substring"`` / ``"regex"``.

        Returns:
            Number of matching fragment files.
        """
        return len(self._fragments_from_source_path(source_path, match=match))

    # -- Classifications purge -------------------------------------------

    def purge_classifications(self) -> PurgeResult:
        """Reset classification fields on every fragment to unclassified.

        Preserves source metadata, timestamps, threads, eddies, and body
        content; only ``frequency``, ``wavelength``, and ``voice``
        frontmatter blocks are reset to defaults.

        Returns:
            A :class:`PurgeResult` recording how many fragments changed.
        """
        result = PurgeResult(
            operation="classifications",
            target="all",
            criteria={"scope": "all"},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Reset classification fields on every fragment, mutating ``result``."""
            for frag_file in self._list_fragment_files():
                post = self._load_frontmatter(frag_file)
                if post is None:
                    continue
                if self._reset_classifications(post):
                    result.classifications_reset += 1
                    frag_id = post.get("id")
                    if isinstance(frag_id, str):
                        result.affected_fragment_ids.append(frag_id)
                    if not self.dry_run:
                        frag_file.write_text(
                            frontmatter.dumps(post),
                            encoding="utf-8",
                        )
            result.fragments_affected = result.classifications_reset

        return self._run_audited(result, body)

    # -- Daterange purge -------------------------------------------------

    def purge_daterange(
        self,
        start: date,
        end: date,
    ) -> PurgeResult:
        """Delete fragments whose ``created`` date falls within a range.

        Args:
            start: Inclusive start date (UTC).
            end: Inclusive end date (UTC).

        Returns:
            A :class:`PurgeResult` summarising the deletions.

        Raises:
            ValueError: If ``end`` is before ``start``.
        """
        if end < start:
            msg = f"End date {end} is before start date {start}"
            raise ValueError(msg)

        target = f"{start.isoformat()}..{end.isoformat()}"
        result = PurgeResult(
            operation="daterange",
            target=target,
            criteria={"start": start.isoformat(), "end": end.isoformat()},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Run the date-range destructive ops, mutating ``result``."""
            for frag_file in self._list_fragment_files():
                post = self._load_frontmatter_for_match(frag_file)
                if post is None:
                    continue
                created = _coerce_date(post.get("created"))
                if created is None or not (start <= created <= end):
                    continue
                self._purge_single(frag_file, post, result)
            self._purge_scoped_tail(result)

        return self._run_audited(result, body)

    # -- Vault purge ------------------------------------------------------

    def purge_vault(self, confirmation: str) -> PurgeResult:
        """Destroy every fragment, thread, eddy, and related file.

        Folder structure is preserved; only the *contents* of the
        top-level vault content folders are removed. The purge log
        itself is preserved.

        Every fragment is enumerated and its frontmatter read **before**
        the wipe begins, so the result (and through it the compliance
        audit line) reports how many fragments were destroyed and names
        their ids. That read is the operation's one scaling cost: it is
        O(fragments) in both file reads and YAML parses, where the wipe
        itself is a handful of ``rmtree`` calls. It cannot be deferred —
        read the ids after the wipe and there is nothing left to read
        them from (#1340).

        Args:
            confirmation: Must equal :data:`VAULT_PURGE_CONFIRMATION`
                exactly.

        Returns:
            A :class:`PurgeResult` describing the destruction.

        Raises:
            ValueError: If ``confirmation`` does not match, or if
                ``self.vault_path`` does not look like a Creek vault
                (no ``00-Creek-Meta/creek_config.yaml`` marker file —
                GAP-003).
        """
        if confirmation != VAULT_PURGE_CONFIRMATION:
            msg = (
                f"Vault purge requires explicit confirmation text: "
                f"{VAULT_PURGE_CONFIRMATION!r}"
            )
            raise ValueError(msg)
        self._require_vault_marker()

        result = PurgeResult(
            operation="vault",
            target="entire vault",
            criteria={"scope": "entire vault"},
            dry_run=self.dry_run,
        )

        def body() -> None:
            """Wipe every vault-content folder, mutating ``result``."""
            # Read strictly BEFORE the wipe — the files are about to stop
            # existing — but commit to ``result`` strictly AFTER it.
            #
            # The order is a compliance property, not a style choice. On
            # an abort, ``_run_audited`` writes a ``status="partial"``
            # outcome line from whatever ``result`` holds at that moment.
            # Assigning the census up front made that line certify
            # ``fragments_deleted: N`` — naming every id — for a vault
            # where the very first ``rmtree`` had failed and all N files
            # were still on disk. An RTBF record that over-claims an
            # erasure is worse than one that under-claims it, so the
            # counts land only once the destructive section has run.
            census = self._census_fragments_for_vault_purge()
            for folder in _VAULT_CONTENT_FOLDERS:
                folder_path = self.vault_path / folder
                if not folder_path.is_dir():
                    continue
                self._wipe_folder_contents(folder_path, result)
            self._wipe_adepthood_staging(result)
            self._wipe_meta_artifacts(result)
            result.fragments_affected = census.file_count
            result.affected_fragment_ids.extend(census.fragment_ids)
            result.embeddings_removed = self._delete_cache_file()
            result.embeddings_cache_undeleted = self._cache_survived_deletion()

        return self._run_audited(result, body)

    # -- Private helpers -------------------------------------------------

    def _skip_as_removed(self, path: Path) -> bool:
        """Report whether a dry run should treat *path* as already gone.

        The single gate on the dry-run ledger (#1340). Every consultation
        of :attr:`_ledger` for a *removal* routes through here, so the
        safety property — "the ledger is populated on every run but
        queried only under ``dry_run``" — is structural rather than a
        promise that has to be re-audited at four call sites. Poison the
        ledger and an apply run still deletes and rewrites exactly what
        it always did, because it never asks.

        Ordering at each call site is load-bearing in the other
        direction: this check must come **after** the containment guards
        (``_resolve_pointer_in_vault``, ``_in_any_staging_root``, the
        intimate-stub-root check, ``_contained_voice_artifact``), never
        before. Those are fail-closed security controls whose whole point
        is to run before anything is counted.

        Args:
            path: The file a later pass in this operation is about to
                consider.

        Returns:
            ``True`` only in a dry run, and only for a path an apply run
            would already have unlinked by this point.
        """
        return self.dry_run and self._ledger.is_removed(path)

    def _census_fragments_for_vault_purge(self) -> _VaultFragmentCensus:
        """Read what a full-vault wipe is about to destroy (#1340).

        ``purge_vault`` used to report ``len(deleted_files)``, which held
        the *top-level entries* of each content folder — so a vault of
        500 fragments in ``01-Fragments/Conversations`` certified "3
        fragments deleted, affected_fragments=[]" to the compliance log
        while destroying all 500.

        Deliberately **pure**: it reads the vault and returns, rather
        than writing through to the caller's :class:`PurgeResult`. The
        census has to run before the wipe (the files are about to stop
        existing) but must not reach the audit log before the wipe has
        actually happened, or an abort mid-wipe writes a
        ``status="partial"`` line certifying an erasure that did not
        occur. Returning the numbers lets :meth:`purge_vault` order those
        two facts independently.

        The count and the id list are deliberately asymmetric, mirroring
        :meth:`_purge_single`'s existing contract exactly:
        ``file_count`` counts **every** ``.md`` file under
        ``01-Fragments/`` — including one with no ``id`` and one whose
        YAML will not parse, because the wipe destroys those too and an
        erasure record that under-counts destruction is the worse error —
        while ``fragment_ids`` gains only the subset carrying a real
        string id, because naming an id nobody recorded would be a
        fabrication in a compliance record.

        :meth:`_list_fragment_files` is reused rather than re-deriving
        the glob: it sorts, and a dry run's id list is compared against
        its apply twin's, which an unsorted walk across two directories
        could make flake. The loader is the read-only
        :meth:`_load_frontmatter_for_match` — nothing here is ever
        written back.

        Returns:
            The file count and the ids read off those files.
        """
        fragment_files = self._list_fragment_files()
        fragment_ids: list[str] = []
        for frag_file in fragment_files:
            post = self._load_frontmatter_for_match(frag_file)
            if post is None:
                continue
            frag_id = post.get("id")
            if isinstance(frag_id, str):
                fragment_ids.append(frag_id)
        return _VaultFragmentCensus(
            file_count=len(fragment_files),
            fragment_ids=fragment_ids,
        )

    def _require_vault_marker(self) -> None:
        """Refuse to operate on a directory that is not a Creek vault (GAP-003).

        Looks for ``<vault>/00-Creek-Meta/creek_config.yaml`` — the
        per-vault config file that ``creek init`` deploys and that
        survives every purge operation. Its absence almost always
        means the operator typoed ``--vault`` and is pointing at an
        unrelated directory with coincidentally numeric-prefix
        folders. Raising here, *before* the intent audit line is
        written, prevents an entry from being committed to a
        non-vault's would-be audit log.

        Raises:
            ValueError: When the marker file is not present. The
                message names the absolute path of the file the engine
                looked for so the operator can diagnose the typo.
        """
        marker = self.vault_path.joinpath(*VAULT_MARKER_RELPATH)
        if not marker.is_file():
            msg = (
                f"{self.vault_path} does not appear to be a Creek vault "
                f"(missing marker file {marker}). Run `creek init "
                f"--vault <path>` first, or fix the --vault argument."
            )
            raise ValueError(msg)

    def _purge_cache_for(self, fragment_ids: list[str]) -> int:
        """Drop matching rows from the embeddings cache (GAP-001).

        Called by every file-deleting purge operation *after* the
        fragments have been removed from disk so the audit entry's
        ``embeddings_removed`` reflects what is now provably gone from
        the parquet cache (or what *would* be gone in a dry run).

        Args:
            fragment_ids: Fragment IDs whose cached embedding rows
                should be scrubbed.

        Returns:
            Number of cache rows removed (or counted-only in dry-run).
        """
        from creek.link.embeddings import (
            embeddings_cache_path,
            purge_fragment_ids_from_cache,
        )

        return purge_fragment_ids_from_cache(
            embeddings_cache_path(self.vault_path),
            fragment_ids,
            dry_run=self.dry_run,
        )

    def _delete_cache_file(self) -> int:
        """Delete the embeddings cache outright (GAP-001 vault path).

        Returns:
            Number of rows that were in the cache when it was removed
            (or that would be removed in a dry run). Zero when the
            cache file did not exist.
        """
        from creek.link.embeddings import (
            delete_embeddings_cache,
            embeddings_cache_path,
        )

        return delete_embeddings_cache(
            embeddings_cache_path(self.vault_path),
            dry_run=self.dry_run,
        )

    def _cache_survived_deletion(self) -> bool:
        """Report whether the embeddings cache is still on disk after deletion.

        Asked of the filesystem rather than inferred from a return value:
        :func:`~creek.link.embeddings.delete_embeddings_cache` reports a *row
        count*, and it deliberately answers ``0`` both when the cache was
        removed empty and when it could not be removed at all (#1480 — it must
        never veto an erasure by raising). Those two are indistinguishable from
        the count, and only one of them is a shortfall.

        Returns:
            ``True`` when the cache path still exists on a real run. Always
            ``False`` under ``dry_run``, where nothing was deleted by design
            and a surviving file is the expected state, not a shortfall.
        """
        if self.dry_run:
            return False
        from creek.link.embeddings import embeddings_cache_path

        return embeddings_cache_path(self.vault_path).exists()

    def _purge_scoped_tail(self, result: PurgeResult) -> None:
        """Run the erasure passes every *scoped* purge owes (#1453).

        Ordering is load-bearing and runs against intuition: the ledger
        erasure goes **first**, the embedding-cache scrub second. A
        corrupt ``embeddings.parquet`` raises an unhandled
        ``ArrowInvalid`` out of the cache layer and aborts the whole
        operation, so a cache scrub sequenced first lets an unreadable
        derived file veto the right-to-be-forgotten work — the erasure
        that actually matters is the one that must not be skippable.

        Deliberately **not** called from :meth:`purge_classifications`.
        A classification reset deletes nothing; wiping its ledger rows
        would make the next ingest re-mint an id for every unchanged
        file in the vault, which is a behaviour change dressed up as a
        privacy fix.

        Args:
            result: Result accumulator. ``ledger_rows_removed`` and
                ``embeddings_removed`` are both set from it.
        """
        result.ledger_rows_removed = forget_fragment_ids(
            self.vault_path,
            result.affected_fragment_ids,
            dry_run=self.dry_run,
        )
        result.embeddings_removed = self._purge_cache_for(
            result.affected_fragment_ids,
        )

    def _wipe_meta_artifacts(self, result: PurgeResult) -> None:
        """Sweep everything unkept out of ``00-Creek-Meta/`` (#1453).

        ``purge_vault`` wipes the ten numbered content folders and used
        to leave this directory whole, so a whole-vault erasure left
        behind the ingest ledger, the provenance log's title-derived
        paths, the consent log, the dedup manifest's content-hash → id
        oracle and the Discord capture-staging plaintext. The policy
        (and the reason for each survivor) lives in
        :mod:`creek.purge.meta`; this method is only the engine's half
        of the wiring.

        Called strictly **before** :meth:`_delete_cache_file`. That
        ordering was originally a defence: the cache delete raised an
        unhandled ``ArrowInvalid`` on a corrupt parquet, so a sweep
        sequenced after it inherited the abort and the erasure did not
        happen at all. #1480 closed the crash — an unreadable cache is
        now destroyed anyway and reported as zero rows — but the order
        stays, because "the erasure that matters runs before anything
        derived" is the rule, not a workaround for one exception.

        Swept paths are counted but deliberately **not** appended to
        ``deleted_files``, following the ``journal_staged_removed``
        precedent — and here the separation is load-bearing rather than
        merely consistent. ``deleted_files`` is copied into the audit
        entry and the MCP payload, both of which outlive the purge, and
        some of these paths are themselves identifying:
        ``State/discord/capture-staging/messages/<channel>/messages.json``
        names a channel. Writing the roster of destroyed artifacts into
        the one file the erasure preserves would re-create a smaller
        version of the leak being closed.

        The file sweep is followed by
        :func:`~creek.purge.meta.prune_empty_meta_dirs`, which removes the
        directories that sweep left empty (#1547). Without it a whole-vault
        erasure kept identifying directory *names* —
        ``State/discord/capture-staging/messages/<channel>/`` survived as an
        empty but named folder — the same residue #1485 closed for a dangling
        symlink's target string. The prune destroys no content (``rmdir``
        refuses a non-empty directory) and is counted nowhere, so it moves no
        number a dry run reports; a dry run therefore skips it outright rather
        than simulating it, exactly as it removes no files.

        Args:
            result: Result accumulator; ``meta_artifacts_removed`` is
                set to the number of files destroyed (counted-only in a
                dry run). Directories pruned afterwards are deliberately
                not counted there: an empty directory is a name, not a
                destroyed artifact.
        """
        meta_root = self.vault_path / META_RELDIR
        result.meta_artifacts_removed = sweep_unkept_meta(
            meta_root,
            skip=self._skip_as_removed,
            remove=self._remove_meta_artifact,
            breaks_link=self._vault_wipe_breaks_link,
        )
        if not self.dry_run:
            prune_empty_meta_dirs(meta_root)

    def _vault_wipe_breaks_link(self, link: Path) -> bool:
        """Report whether this vault purge destroys what *link* points at.

        The meta sweep leaves a symlink to a *directory* alone, and that
        exemption has to mean "a directory that outlives the purge" or
        the preview and the apply run classify the same link
        differently. ``purge_vault`` wipes the ten content folders
        before it sweeps ``00-Creek-Meta/``, so
        ``00-Creek-Meta/latest-thread -> 01-Fragments/Journal/`` is a
        live directory link when a dry run meets it and a dangling one
        when an apply run does: preview ``0``, apply ``1``. Answering
        from the *folder list* rather than from the filesystem makes
        both runs agree.

        Only the ten content folders are consulted, and that is the
        whole set of directory-destroying work that precedes the sweep.
        ``_wipe_adepthood_staging`` removes staged **files**, which
        ``is_file()`` already classifies identically on both sides, and
        leaves its staging roots standing; ``_delete_cache_file`` runs
        *after* the sweep. A link to a content folder itself (rather
        than into one) is not broken either — the wipe empties those
        folders without removing them — so the comparison excludes the
        folder root.

        The question is asked of the **whole resolution chain**, not of
        where it ends (#1562). ``Path.resolve()`` collapses every hop
        and every ``..`` into one final path, and three chains this
        purge really does break survive that collapse looking innocent:
        one whose last hop is an out-of-vault directory but whose
        *intermediate alias* lives in ``01-Fragments/``; one whose
        target path walks through ``01-Fragments/Journal/sub/../../..``
        before landing outside; and one naming ``01-fragments`` in a
        case the filesystem accepts and a string comparison does not.
        All three previewed ``0`` and applied ``1``.
        :func:`_chain_enters_wiped_content` walks the hops instead, and
        compares each one to the content folders by ``Path.samefile``
        rather than by text.

        An ``OSError`` (an unreadable parent) and an exhausted hop cap
        (a symlink loop) both answer ``False``: this predicate can only
        ever *widen* what the sweep destroys, so failing it closed would
        destroy a link on the strength of an error rather than a fact. A
        dangling link needs no answer here at all —
        :func:`~creek.purge.meta._sweep_destroys_link` has already
        claimed it for its target string (#1485).

        Args:
            link: A symlink under ``00-Creek-Meta/``.

        Returns:
            ``True`` when resolving *link* passes through anything
            strictly inside a vault content folder, which this purge is
            about to wipe.
        """
        roots = [self.vault_path / folder for folder in _VAULT_CONTENT_FOLDERS]
        return _chain_enters_wiped_content(link, roots)

    def _remove_meta_artifact(self, path: Path) -> None:
        """Destroy one swept ``00-Creek-Meta/`` file, or pretend to.

        Args:
            path: The file the sweep has decided against.
        """
        if self.dry_run:
            self._ledger.mark_removed(path)
            return
        path.unlink()

    def _purge_single(
        self,
        frag_file: Path,
        post: frontmatter.Post,
        result: PurgeResult,
    ) -> None:
        """Apply a single-fragment purge to a result accumulator.

        Args:
            frag_file: Fragment file to remove.
            post: Parsed frontmatter of ``frag_file``.
            result: Result object being accumulated.
        """
        title = str(post.get("title", frag_file.stem))
        thread_ids = _str_list(post.get("threads"))
        eddy_ids = _str_list(post.get("eddies"))

        result.deleted_files.append(str(frag_file))
        # ``frontmatter.Post.get`` returns ``Any``, so the id may be a
        # non-string; only a real string is a scrubbable fragment ID.
        frag_id = post.get("id")
        if isinstance(frag_id, str):
            result.affected_fragment_ids.append(frag_id)
        result.fragments_affected += 1
        # Before the scrub, for the reason spelled out in purge_fragment.
        self._purge_voice_artifacts(
            frag_id if isinstance(frag_id, str) else "",
            frag_file,
            result,
        )
        # One vault walk applies both the wiki-link and provenance scrubs.
        wiki_count, md_count, prov_count = self._scrub_references(
            title=title,
            fragment_id=frag_id if isinstance(frag_id, str) else "",
            exclude=frag_file,
        )
        result.wikilinks_removed += wiki_count
        result.markdown_links_removed += md_count
        result.provenance_scrubbed += prov_count
        result.threads_updated += self._decrement_counts(
            "02-Threads",
            thread_ids,
        )
        result.eddies_updated += self._decrement_counts(
            "03-Eddies",
            eddy_ids,
        )
        self._purge_intimate_stub(post, result)
        self._purge_staged_source_entry(post, result)
        # The two arms are the same act recorded two ways: a dry run
        # notes what an apply run would have destroyed here, so the next
        # fragment's passes walk the world that deletion would have left
        # behind. Only the dry arm writes to the ledger — an apply run
        # never reads it, so populating it there would resolve() and
        # retain a path per deletion for nobody.
        if self.dry_run:
            self._ledger.mark_removed(frag_file)
        else:
            frag_file.unlink()

    def _resolve_pointer_in_vault(self, pointer: str, *, field: str) -> Path | None:
        """Resolve a frontmatter-supplied path pointer inside the vault.

        Pointers such as ``saved_from.intimate_body_pointer`` and
        ``source.origin_key`` are vault *content*: an operator (or
        anyone who can write one note) controls their text, so they are
        untrusted input to a destructive path. This helper turns such a
        pointer into an absolute path only when it is safe to follow —
        a refusal is a no-op, never an error, per the purge engine's
        "a missing or bad pointer is skipped" contract.

        A pointer is refused when it cannot be resolved at all (an
        embedded NUL byte makes :meth:`~pathlib.Path.resolve` raise
        ``ValueError``; a resolution loop or unreadable component
        raises ``OSError``) or when it resolves outside the vault root.
        Both refusals are logged at WARNING so an ignored pointer is
        distinguishable from a sweep that never ran.

        Args:
            pointer: Vault-relative path string taken from frontmatter.
            field: Frontmatter field name the pointer came from, used
                verbatim in the refusal logs so operators can grep for
                the field that carried the bad value.

        Returns:
            The resolved absolute path, or ``None`` meaning *refuse* —
            the caller must not follow this pointer.
        """
        try:
            resolved = (self.vault_path / pointer).resolve()
        except (OSError, ValueError):
            logger.warning(
                "Ignoring %s %r that cannot be resolved to a path",
                field,
                pointer,
            )
            return None
        vault_root = self.vault_path.resolve()
        if not resolved.is_relative_to(vault_root):
            logger.warning(
                "Ignoring %s %r that resolves outside the vault root %s",
                field,
                pointer,
                vault_root,
            )
            return None
        return resolved

    def _purge_intimate_stub(
        self,
        post: frontmatter.Post,
        result: PurgeResult,
    ) -> None:
        """Sweep the intimate-body stub a purged note points at (GAP-012).

        ``creek save`` of an ``intimate``-tier answer writes a
        title-only note and routes the full body to a stub file under
        ``10-Liminal/Compost/intimate-stubs/``, recording the link in
        the note's ``saved_from.intimate_body_pointer`` frontmatter. A
        scoped purge that deletes the note must follow that pointer and
        delete the stub too, or the full intimate body survives the
        right-to-be-forgotten request.

        The method is defensive: a missing or empty pointer, an
        unresolvable pointer, an already-deleted stub, and a pointer
        that resolves outside the vault root are all treated as no-ops
        rather than errors. A second containment guard additionally
        scopes deletion to the canonical stub directory itself, so a
        hand-edited or malicious pointer (``../../secret``,
        ``00-Creek-Meta/creek_config.yaml``, or a ``..`` walk back out
        of the stub dir) can never steer a delete at an arbitrary file.

        That guard is deliberately fail-closed, with one accepted side
        effect: a hand-authored stub parked *outside* the canonical
        directory now survives the purge. That is the safe direction —
        the note itself is still deleted, so the RTBF request is
        honoured, and the operator can purge the stray file explicitly.

        Args:
            post: Frontmatter of the note being purged.
            result: Result accumulator; ``intimate_stubs_removed`` is
                incremented for each stub actually removed (or that
                would be removed in a dry run).
        """
        # Imported inside the function to keep the RTBF purge path's
        # module-level import surface thin: at module scope this pulls
        # ``creek/save/__init__.py`` → ``writer.py`` →
        # ``creek.classify.privacy_filter`` → the whole
        # ``creek.classify.llm`` provider subtree (httpx and friends).
        # This is NOT a circular-import workaround — nothing under
        # ``creek/save/`` imports ``creek.purge``, so please do not
        # "fix" it by hoisting the import to module scope.
        from creek.save._constants import INTIMATE_STUB_RELPATH

        pointer = self._intimate_pointer(post)
        if not pointer:
            return
        stub_path = self._resolve_pointer_in_vault(
            pointer,
            field="intimate_body_pointer",
        )
        if stub_path is None:
            return
        stub_root = (self.vault_path / INTIMATE_STUB_RELPATH).resolve()
        # Checked before the counter so a dry run never previews a
        # deletion the real run would refuse. The resolved victim path
        # is deliberately left out of the message: the pointer is
        # attacker-controlled, so echoing what it resolved to would
        # turn the log into a filesystem oracle.
        if not stub_path.is_relative_to(stub_root):
            logger.warning(
                "Ignoring intimate_body_pointer %r that resolves outside "
                "the intimate stub dir %s",
                pointer,
                stub_root,
            )
            return
        if not stub_path.is_file():
            return
        # After both containment guards, never before them: this only
        # suppresses a *double* count in a preview, and must not be able
        # to stand in for a refusal. Two notes can point at one stub, so
        # without it a dry run reports two removals for an apply that
        # does one (#1340).
        if self._skip_as_removed(stub_path):
            return
        result.intimate_stubs_removed += 1
        if self.dry_run:
            self._ledger.mark_removed(stub_path)
        else:
            stub_path.unlink(missing_ok=True)

    @staticmethod
    def _intimate_pointer(post: frontmatter.Post) -> str | None:
        """Extract ``saved_from.intimate_body_pointer`` from a note.

        Args:
            post: Parsed frontmatter of a vault note.

        Returns:
            The stub pointer (vault-relative path string), or ``None``
            when the note carries no usable pointer.
        """
        saved_from = post.get("saved_from")
        if not isinstance(saved_from, dict):
            return None
        pointer = saved_from.get("intimate_body_pointer")
        if isinstance(pointer, str) and pointer.strip():
            return pointer
        return None

    def _in_any_staging_root(self, path: Path) -> bool:
        """Report whether *path* lies inside an Adepthood staging root.

        The containment guard behind :meth:`_purge_staged_source_entry`,
        factored out so the guard reads as one condition however many
        roots :data:`~creek.ingest.journal_staging.ADEPTHOOD_STAGING_RELDIRS`
        grows to hold.

        Args:
            path: An already vault-contained absolute path (the output of
                :meth:`_resolve_pointer_in_vault`).

        Returns:
            ``True`` when *path* is inside at least one staging root.
        """
        return any(
            path.is_relative_to((self.vault_path / reldir).resolve())
            for reldir in ADEPTHOOD_STAGING_RELDIRS
        )

    def _purge_staged_source_entry(
        self,
        post: frontmatter.Post,
        result: PurgeResult,
    ) -> None:
        """Sweep the staged source a purged fragment points at (#845/#1023).

        Adepthood stages the content it captures under the vault and
        records the vault-relative path in the fragment's
        ``source.origin_key``: ``creek.journal`` writes each entry's
        full body as markdown under
        ``00-Creek-Meta/adepthood/journal/``, and ``creek.upload``
        writes each uploaded document's bytes verbatim under
        ``00-Creek-Meta/adepthood/uploads/``. A scoped purge that
        deletes the fragment must follow that key and delete the staged
        file too, or the full — often intimate — source content
        survives the right-to-be-forgotten request.

        The method is defensive, and now shares that defence with
        :meth:`_purge_intimate_stub` in both directions: a missing or
        empty key, an unresolvable key, an already-deleted staged file,
        and a key that resolves outside the vault root are all no-ops
        rather than errors (see :meth:`_resolve_pointer_in_vault`). A
        second containment guard additionally scopes deletion to the
        staging directories themselves
        (:meth:`_in_any_staging_root`), so a hand-edited or malicious
        ``origin_key`` (``../x``, ``01-Fragments/other.md``) can never
        steer a delete at an arbitrary vault file. #1023 widened that
        guard to every declared staging root; it never relaxed it.

        Args:
            post: Frontmatter of the fragment being purged.
            result: Result accumulator; ``journal_staged_removed`` is
                incremented for each staged file actually removed (or
                that would be removed in a dry run) — **after** the
                unlink returns, never before (#1481).
        """
        origin_key = _extract_source_origin_key(post)
        if not origin_key:
            return
        staged_path = self._resolve_staged_source(origin_key)
        if staged_path is None:
            return
        # After the containment guards inside _resolve_staged_source, for
        # the reason spelled out in _purge_intimate_stub: two fragments
        # split out of one staged document share an origin_key.
        if self._skip_as_removed(staged_path):
            return
        if self.dry_run:
            self._ledger.mark_removed(staged_path)
        else:
            staged_path.unlink(missing_ok=True)
        # Counted strictly *after* the unlink returns (#1481). Incrementing
        # first meant an ``OSError`` from the unlink — a read-only mount, a
        # permission change, an NFS blip — propagated to ``_run_audited``,
        # which wrote a ``status="partial"`` outcome line whose
        # ``journal_staged_removed`` already included a staged file still
        # sitting on disk with the entry's full plaintext body in it. An
        # erasure record that over-claims a destruction is the one error
        # this subsystem must never make (#1340).
        result.journal_staged_removed += 1

    def _resolve_staged_source(self, origin_key: str) -> Path | None:
        """Resolve *origin_key* to the staged file to delete, or ``None``.

        Tries the key whole, then — only if that named no file — as a
        ``<path>#<unit>`` sub-unit key whose path half is the real staged
        document (#1305). Both guards are re-applied on the fallback: the
        recovered path must resolve inside the vault
        (:meth:`_resolve_pointer_in_vault`) and inside a declared staging
        root (:meth:`_in_any_staging_root`). The fallback therefore only
        ever widens what is deleted *within* the staging roots — a ratchet
        in the restrictive direction, never a relaxation.

        Order is load-bearing in both directions. Whole-key-first means a
        genuinely ``#``-named document (``report#1.xlsx``) keeps resolving
        to itself rather than steering the delete at a different file
        called ``report``; ``#`` is legal in a POSIX filename, so the split
        is a hypothesis and must never pre-empt the literal reading.
        Fallback-at-all is what stops an uploaded workbook surviving an
        RTBF request: since #1305 each sheet's ``origin_key`` names a
        sub-unit, which resolves inside the staging root but is not a file,
        so the ``is_file()`` check alone left the operator's document on
        disk after the erasure meant to remove it.

        Args:
            origin_key: The fragment's ``source.origin_key``, verbatim.

        Returns:
            The staged file to unlink, or ``None`` to skip — a missing,
            unresolvable, out-of-vault or out-of-staging key is a no-op
            rather than an error, per this engine's pointer contract.
        """
        for candidate_key in self._staged_key_candidates(origin_key):
            resolved = self._resolve_pointer_in_vault(
                candidate_key,
                field="source.origin_key",
            )
            if resolved is None:
                continue
            if not self._in_any_staging_root(resolved):
                logger.warning(
                    "Ignoring source.origin_key %r that resolves outside "
                    "the Adepthood staging dirs %s",
                    candidate_key,
                    [str(reldir) for reldir in ADEPTHOOD_STAGING_RELDIRS],
                )
                continue
            if resolved.is_file():
                return resolved
        return None

    @staticmethod
    def _staged_key_candidates(origin_key: str) -> list[str]:
        """Return *origin_key* readings to try, most literal first (#1305).

        Args:
            origin_key: The fragment's ``source.origin_key``, verbatim.

        Returns:
            The whole key, followed by its path half when it carries a
            sub-unit suffix. One entry for every key that does not.
        """
        base, unit = split_source_unit(origin_key)
        return [origin_key] if unit is None else [origin_key, base]

    def _purge_voice_artifacts(
        self,
        fragment_id: str,
        frag_file: Path,
        result: PurgeResult,
    ) -> None:
        """Sweep the ``07-Voice/`` artifacts derived from a purged fragment (#1211).

        The voice subsystem does not merely *reference* a fragment, it
        **copies its body**, so scrubbing references leaves the erased
        content on disk in three shapes:
        ``Register-Samples/<register>/<id>.md`` is a ``shutil.copy2`` of
        the fragment file; ``<register>-profile.md`` renders each
        exemplar body verbatim under ``### Sample Passages``; and
        ``Lexicon/glossary.md`` plus ``Lexicon/Metaphors/<domain>.md``
        quote whole source sentences.

        Each match is **deleted rather than edited**. A derived note is a
        function of the corpus: excising one passage would leave the
        purged fragment's statistical residue (its n-grams, its
        contribution to every count in the note) behind while the note
        went on advertising a total it no longer has. All four artifacts
        are regenerated by ``creek report --type voice`` / ``--type
        lexicon``, so deletion costs a re-run and buys a clean erasure.

        The sweep is scoped to those declared locations, never a blanket
        ``07-Voice`` walk. ``07-Voice/Drafts/`` in particular holds the
        operator's own writing, which the existing provenance scrub
        handles and which a fragment purge has no mandate to delete.

        The body needed by the content-keyed pass is re-read from
        *frag_file* under **strict** UTF-8 rather than taken from the
        caller's already-parsed post. The callers all hold a post from
        :meth:`_load_frontmatter_for_match`, whose contract is that a
        match is a function of frontmatter metadata only, and which
        therefore hands back a body with every undecodable byte replaced
        by U+FFFD. Matching that mangled text against a profile that
        quotes the real bytes would find nothing and report a complete
        erasure — the exact "a derived copy survives a green purge"
        failure this sweep exists to close. See :meth:`_voice_match_body`
        for what happens when the strict read fails.

        Args:
            fragment_id: Id of the fragment being purged. Empty string
                skips the id-keyed passes.
            frag_file: The fragment file being purged, re-read strictly
                to obtain the body the content-keyed pass matches on —
                see :meth:`_voice_profiles_quoting` for why that pass
                has to be content-keyed at all.
            result: Result accumulator; ``voice_artifacts_removed`` is
                incremented per artifact actually removed (or that would
                be removed in a dry run), and ``voice_body_undecodable``
                gains this fragment's id when the strict read fails.
        """
        voice_root = self.vault_path / _VOICE_RELDIR
        if not voice_root.is_dir():
            return
        body = self._voice_match_body(frag_file, fragment_id, result)
        contained_root = voice_root.resolve()
        seen: set[Path] = set()
        for candidate in self._iter_voice_artifacts(fragment_id, body):
            target = self._contained_voice_artifact(candidate, contained_root)
            # Checked before the counter so a dry run never previews a
            # deletion the real run would refuse; de-duplicated because
            # two passes can name the same note; and, in a dry run only,
            # skipped when an earlier fragment in this same operation
            # already claimed it — a profile and a glossary are shared
            # artifacts, so the apply run has no file left to delete by
            # the time the second fragment looks (#1340).
            if target is None or target in seen or self._skip_as_removed(target):
                continue
            seen.add(target)
            result.voice_artifacts_removed += 1
            if self.dry_run:
                self._ledger.mark_removed(target)
            else:
                target.unlink(missing_ok=True)

    @staticmethod
    def _contained_voice_artifact(
        candidate: Path,
        contained_root: Path,
    ) -> Path | None:
        """Resolve *candidate* and confirm it is a file inside the voice root.

        Deletion targets the **resolved** path so a symlinked copy loses
        its content rather than just its link — an RTBF sweep that
        unlinked the alias would leave the body on disk. Resolving first
        is also what makes containment meaningful: a register folder
        symlinked out of the vault would otherwise steer the sweep at an
        arbitrary file.

        Args:
            candidate: A path produced by one of the artifact passes.
            contained_root: The already-resolved ``07-Voice`` directory.

        Returns:
            The resolved path to delete, or ``None`` meaning *refuse* —
            unresolvable, outside the voice root, or not a file.
        """
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            logger.warning(
                "Ignoring voice artifact %s that cannot be resolved to a path",
                candidate,
            )
            return None
        if not resolved.is_relative_to(contained_root):
            # The resolved victim is deliberately left out of the message:
            # naming it would turn the log into a filesystem oracle.
            logger.warning(
                "Ignoring voice artifact %s that resolves outside %s",
                candidate,
                contained_root,
            )
            return None
        if not resolved.is_file():
            return None
        return resolved

    def _decode_body_or_report(
        self,
        frag_file: Path,
        fragment_id: str,
        result: PurgeResult,
    ) -> str | None:
        """Read *frag_file* under strict UTF-8, or record the shortfall.

        Split out of :meth:`_voice_match_body` so each function holds one
        ``try``: an undecodable body and unparseable frontmatter are different
        shortfalls with different messages, and merging their brackets would
        both blur that and widen the guard past the house shape.

        Args:
            frag_file: The fragment file being purged.
            fragment_id: Its id, used only to name it in the warning and on
                ``voice_body_undecodable``.
            result: Result accumulator to record the shortfall on.

        Returns:
            The strictly-decoded text, or ``None`` when the bytes are not
            valid UTF-8 and the content-keyed pass must be skipped.
        """
        try:
            return frag_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            result.voice_body_undecodable.append(fragment_id or _UNNAMED_FRAGMENT)
            logger.warning(
                "Body of fragment %s is not valid UTF-8, so the voice-profile "
                "sweep cannot match it: this erasure is PARTIAL and a "
                "07-Voice/<register>-profile.md may still quote the fragment. "
                "Regenerate 07-Voice with `creek report --type voice`.",
                fragment_id or _UNNAMED_FRAGMENT,
            )
            return None

    def _voice_match_body(
        self,
        frag_file: Path,
        fragment_id: str,
        result: PurgeResult,
    ) -> str | None:
        """Re-read *frag_file*'s body under strict UTF-8, or report the gap.

        The content-keyed profile pass compares this text against bytes
        a generator wrote verbatim, so a lossily-decoded body is not
        merely imprecise — it is guaranteed not to match, which would
        turn an incomplete erasure into a silent success. This read is
        therefore strict, and an undecodable body is reported rather
        than swallowed: the id goes on ``voice_body_undecodable``, which
        downgrades the operation's audit ``outcome`` line to
        ``status="partial"`` (see :meth:`_run_audited`) and surfaces in
        the CLI, and a WARNING names the fragment id.

        Only the id is named. The path would point an unentitled reader
        at the file, and the body is the very content being erased; the
        purge subsystem's logging rule is ids and constants only.

        A decoding failure never stops the id-keyed passes — the
        ``Register-Samples`` stem and the ``[[<id>]]`` lexicon wikilink
        do not depend on the body, so those artifacts are still erased
        for a fragment whose bytes are not valid UTF-8.

        Args:
            frag_file: The fragment file being purged.
            fragment_id: Its id, used only to name the fragment in the
                warning and in ``voice_body_undecodable``.
            result: Result accumulator to record the shortfall on.

        Returns:
            The strictly-decoded body, or ``None`` when the file is not
            valid UTF-8 and the content-keyed pass must be skipped.
        """
        text = self._decode_body_or_report(frag_file, fragment_id, result)
        if text is None:
            return None

        try:
            post = frontmatter.loads(text)
        except (*FRONTMATTER_LOAD_ERRORS, *_FRONTMATTER_RESOURCE_ERRORS):
            # Same shortfall, same fail-safe answer. Raising would let one
            # hostile or hand-broken file VETO an entire right-to-be-forgotten
            # purge (#1455); returning the body anyway would be worse, because
            # the content-keyed comparison could not match and an incomplete
            # erasure would report success. Recording it downgrades the audit
            # outcome to `partial`, which is the honest answer.
            #
            # `_FRONTMATTER_RESOURCE_ERRORS` joins the tuple here for exactly
            # that reason: a nesting bomb or an alias bomb defeats the parser
            # just as a hand-broken document does, the id-keyed passes are
            # equally unaffected, and the shortfall the operator must be told
            # about is identical.
            #
            # Id only - no path, no parser message. This subsystem logs ids and
            # constants, and the tier is unknown precisely because it would not
            # parse.
            result.voice_body_undecodable.append(fragment_id or _UNNAMED_FRAGMENT)
            logger.warning(
                "Frontmatter of fragment %s will not parse, so the "
                "voice-profile sweep cannot match it: this erasure is PARTIAL "
                "and a 07-Voice/<register>-profile.md may still quote the "
                "fragment. Regenerate 07-Voice with `creek report --type voice`.",
                fragment_id or _UNNAMED_FRAGMENT,
            )
            return None

        return post.content

    def _iter_voice_artifacts(
        self,
        fragment_id: str,
        body: str | None,
    ) -> Iterator[Path]:
        """Yield every ``07-Voice`` artifact attributable to one fragment.

        Args:
            fragment_id: Id of the fragment being purged.
            body: Its strictly-decoded body text, or ``None`` when the
                file could not be decoded — which skips the content-keyed
                pass and only that pass, leaving both id-keyed passes to
                erase what they can.

        Yields:
            Candidate paths, before any containment or existence check.
        """
        yield from self._voice_sample_copies(fragment_id)
        yield from self._voice_lexicon_notes(fragment_id)
        if body is not None:
            yield from self._voice_profiles_quoting(body)

    def _voice_sample_copies(self, fragment_id: str) -> Iterator[Path]:
        """Yield the exemplar copies named after *fragment_id*.

        ``VoiceExemplarCollector._persist_fragment`` writes
        ``f"{fragment.id}.md"``, so the filename **stem** is the recorded
        link — and it is the one link the reference scrub cannot destroy,
        which is exactly why #879's own prune matches on it too.

        Only a real fragment id names a file here, so the sibling
        ``Register-Samples/_Summary.md`` is never a candidate: it is
        #879's manifest, which the next prune needs in order to remove
        stale copies. It normally carries counts and no body text — but
        that claim is not unconditional: ``_is_safe_sample_stem`` in
        ``creek/generate/voice.py`` documents the crash window (#879
        territory) in which a fragment body can land in ``_Summary.md``
        instead. Widening the sweep to cover that race is out of scope
        here.

        Args:
            fragment_id: Id of the fragment being purged.

        Yields:
            One candidate per register folder.
        """
        # A separator in the id would make the join a subpath rather than
        # a filename; the containment guard would catch an escape, but
        # refusing here keeps the sweep from naming a file the collector
        # could never have written.
        if not fragment_id or {"/", "\\", "\x00"} & set(fragment_id):
            return
        samples_root = self.vault_path.joinpath(*_VOICE_SAMPLES_RELDIR)
        if not samples_root.is_dir():
            return
        for register_dir in sorted(samples_root.iterdir()):
            if register_dir.is_dir():
                yield register_dir / f"{fragment_id}.md"

    def _voice_lexicon_notes(self, fragment_id: str) -> Iterator[Path]:
        """Yield lexicon notes that quote *fragment_id*'s sentences.

        ``creek.generate.lexicon._render_context_line`` renders every
        usage as ``- [[<fragment_id>]] — <sentence>``, so the wikilink is
        a recorded, exact link from the id the caller holds to the note
        holding its prose.

        Reads the file's real bytes, deliberately **not** the dry-run
        ledger's pending rewrite (#1340). The other counted sweeps
        consult the ledger so a preview does not re-count work an apply
        run had already done; this one must not, because both sides of
        its comparison move together. The needle is the purged
        fragment's body, read from disk, and the haystack is a derived
        note, read from disk; an earlier pass's scrub rewrites *both*,
        so disk-vs-disk already agrees between a dry run and its apply.
        Overlaying only the haystack would compare an unscrubbed needle
        against a scrubbed haystack and invent a divergence that is not
        there — measured at ``voice_artifacts_removed`` dry 0 / apply 1
        on exactly that fixture. Pinned by
        ``test_the_voice_sweep_matches_the_same_way_dry_or_applied``.

        Walked through :func:`_contained_md_files` rather than ``rglob``.
        Nothing outside the vault is written or deleted on this path —
        :meth:`_contained_voice_artifact` runs before any ``unlink`` — but
        the match itself read the bytes of whatever a planted link pointed
        at, and "we only read it" is not a property an RTBF sweep should
        have to argue. The guard makes the read never happen.

        Args:
            fragment_id: Id of the fragment being purged.

        Yields:
            Every glossary or metaphor note carrying that wikilink.
        """
        if not fragment_id:
            return
        lexicon_root = self.vault_path.joinpath(*_VOICE_LEXICON_RELDIR)
        if not lexicon_root.is_dir():
            return
        needle = f"[[{fragment_id}]]".encode()
        for note in _contained_md_files(lexicon_root, vault_path=self.vault_path):
            if needle in _read_bytes_for_match(note):
                yield note

    def _voice_profiles_quoting(self, body: str) -> Iterator[Path]:
        """Yield register profiles whose sample passages contain *body*.

        This pass is content-keyed because nothing else is available: a
        profile note records the exemplar **bodies** and no fragment ids
        at all, so there is no provenance to key on (issue #1211's
        recorded design finding). The match is nonetheless exact rather
        than heuristic — ``VoiceProfileGenerator._render_profile_body``
        emits each passage verbatim, so the body appears as a contiguous
        substring. Reaching it still starts from the id: the caller
        resolved the id to the fragment file that supplied this body.

        The pattern glob stays — it is deliberately non-recursive, and
        :func:`_contained_md_files` would widen the sweep to every
        ``*-profile.md`` anywhere under ``07-Voice`` — so containment is
        applied per leaf instead, through the same
        :func:`_contained_leaf` verdict that walk uses. Judged *before*
        the read, for the reason :meth:`_voice_lexicon_notes` records.

        Args:
            body: The purged fragment's body text.

        Yields:
            Every top-level ``<register>-profile.md`` quoting that body.
        """
        stripped = body.strip()
        if not stripped:
            return
        voice_root = self.vault_path / _VOICE_RELDIR
        needle = stripped.encode()
        resolved_vault = self.vault_path.resolve(strict=False)
        for profile in sorted(voice_root.glob(_VOICE_PROFILE_GLOB)):
            if not _contained_leaf(profile, resolved_vault):
                continue
            if needle in _read_bytes_for_match(profile):
                yield profile

    def _wipe_adepthood_staging(self, result: PurgeResult) -> None:
        """Wipe every Adepthood staging root during a vault purge (#845/#1023).

        ``purge_vault`` deliberately preserves ``00-Creek-Meta/``
        (config marker, audit log), so the content-folder wipe never
        reaches the staged journal bodies under
        ``00-Creek-Meta/adepthood/journal/`` or the staged upload bytes
        under ``00-Creek-Meta/adepthood/uploads/`` — they must be swept
        explicitly or full source plaintext survives a whole-vault RTBF
        request. Staged files are source material, not fragments, so
        they are counted on ``journal_staged_removed`` only — never
        appended to ``deleted_files`` and never inflating
        ``fragments_affected``. Since #1340 that separation is
        structural: ``fragments_affected`` is enumerated from
        ``01-Fragments/`` alone by
        :meth:`_count_fragments_for_vault_purge`, where it previously
        held only because this sweep happened to run *after* the
        ``len(deleted_files)`` assignment.

        The walk is non-recursive by design (the staging layouts are
        flat), so anything that is not a file — a subdirectory an
        operator or a future tool dropped in — is skipped rather than
        unlinked. A bare ``unlink`` on a directory raises an
        ``OSError`` (``IsADirectoryError`` on Linux, ``PermissionError``
        on macOS), which :meth:`_run_audited` turns into a
        ``status="partial"`` outcome and re-raises, aborting the entire
        vault purge — so the guard is what stops one stray directory
        from defeating a whole RTBF request.

        Args:
            result: Result accumulator; ``journal_staged_removed`` is
                incremented per staged file removed (or counted-only in
                a dry run), and only once the unlink has returned
                (#1481).
        """
        for reldir in ADEPTHOOD_STAGING_RELDIRS:
            staging_dir = self.vault_path / reldir
            if not staging_dir.is_dir():
                continue
            for staged_file in sorted(staging_dir.iterdir()):
                if not staged_file.is_file():
                    continue
                if self.dry_run:
                    # The meta sweep walks this same directory a moment
                    # later. Without the mark it meets a file this pass
                    # only *pretended* to unlink and counts it a second
                    # time, so the preview over-reports against its own
                    # apply twin — the one thing a preview of an
                    # irreversible erasure must never do (#1340).
                    self._ledger.mark_removed(staged_file)
                else:
                    staged_file.unlink(missing_ok=True)
                # After the unlink, never before — see the twin site in
                # :meth:`_purge_staged_source_entry` (#1481).
                result.journal_staged_removed += 1

    def _find_fragment_by_id(
        self,
        fragment_id: str,
    ) -> tuple[Path | None, frontmatter.Post | None]:
        """Locate a fragment markdown file by its frontmatter ID.

        Args:
            fragment_id: The fragment ID to locate.

        Returns:
            Tuple of ``(path, post)`` or ``(None, None)`` if not found.
        """
        for frag_file in self._list_fragment_files():
            post = self._load_frontmatter_for_match(frag_file)
            if post is not None and post.get("id") == fragment_id:
                return frag_file, post
        return None, None

    def _fragments_from_source(
        self,
        source_type: str,
    ) -> list[tuple[Path, frontmatter.Post]]:
        """List all fragment files whose source platform matches.

        Args:
            source_type: Source platform identifier.

        Returns:
            List of ``(path, post)`` pairs.
        """
        matches: list[tuple[Path, frontmatter.Post]] = []
        for frag_file in self._list_fragment_files():
            post = self._load_frontmatter_for_match(frag_file)
            if post is None:
                continue
            if _extract_source_platform(post) == source_type:
                matches.append((frag_file, post))
        return matches

    def _fragments_from_source_path(
        self,
        source_path: str,
        *,
        match: str,
    ) -> list[tuple[Path, frontmatter.Post]]:
        """List fragments whose ``source.original_file`` matches *source_path*.

        Args:
            source_path: Path string / substring / regex.
            match: ``"exact"`` / ``"substring"`` / ``"regex"``.

        Returns:
            List of ``(path, post)`` pairs.

        Raises:
            ValueError: When *match* is unrecognised, or when
                ``match="regex"`` and *source_path* is not a valid regex.
        """
        predicate = _build_source_path_matcher(
            source_path, match=match, vault_path=self.vault_path
        )
        matches: list[tuple[Path, frontmatter.Post]] = []
        for frag_file in self._list_fragment_files():
            post = self._load_frontmatter_for_match(frag_file)
            if post is None:
                continue
            original_file = _extract_source_original_file(post)
            if original_file is not None and predicate(original_file):
                matches.append((frag_file, post))
        return matches

    def _list_fragment_files(self) -> list[Path]:
        """Return every contained ``.md`` file under ``01-Fragments``.

        Feeds all five operations — fragment, source, source-path,
        daterange and the vault census — so the containment guard in
        :func:`_contained_md_files` covers them at once.

        Returns:
            Sorted list of fragment markdown paths, with anything linking
            out of the vault refused (#1454).
        """
        fragments_dir = self.vault_path / "01-Fragments"
        if not fragments_dir.is_dir():
            return []
        return _contained_md_files(fragments_dir, vault_path=self.vault_path)

    def _list_vault_md_files(self) -> list[Path]:
        """Return every contained ``.md`` file anywhere in the vault.

        Returns:
            Sorted list of markdown paths, with anything linking out of
            the vault refused (#1454).
        """
        if not self.vault_path.is_dir():
            return []
        return _contained_md_files(self.vault_path, vault_path=self.vault_path)

    def _load_frontmatter(self, path: Path) -> frontmatter.Post | None:
        """Read a frontmatter post byte-faithfully, tolerating bad files.

        The **write-safe** loader, for the callers that rewrite the post
        in place (``purge_classifications`` and :meth:`_decrement_counts`
        both do ``frontmatter.dumps`` + ``write_text``). A file this
        cannot decode is skipped rather than salvaged, because re-encoding
        a lossy read would overwrite the operator's original bytes during
        a non-destructive metadata reset. Callers that only ever
        ``unlink`` the file must use :meth:`_load_frontmatter_for_match`.

        Args:
            path: Path to a markdown file.

        Returns:
            Parsed :class:`frontmatter.Post`, or ``None`` when the file
            cannot be read or parsed.
        """
        try:
            return frontmatter.load(str(path))
        except FRONTMATTER_LOAD_ERRORS as exc:
            # Log the exception *type* only: yaml.MarkedYAMLError
            # stringifies with the offending source snippet, which would
            # copy vault content into the log.
            logger.warning(
                "Unable to parse frontmatter in %s (%s); leaving it untouched",
                path,
                type(exc).__name__,
            )
            return None

    def _load_frontmatter_for_match(self, path: Path) -> frontmatter.Post | None:
        """Read a post for a *delete decision only* — never write it back.

        The returned :class:`frontmatter.Post` may be **lossy**: when the
        file is not valid UTF-8, its body is re-read with
        ``errors="replace"`` so every offending byte becomes U+FFFD.
        Writing such a post back to disk would destroy the operator's
        original bytes, so this loader exists solely to decide whether a
        file matches the purge criteria before it is ``unlink``-ed. The
        rewrite paths use :meth:`_load_frontmatter` instead.

        The lossy read is safe for that one job because a purge match is
        a function of *frontmatter metadata* only — ``id``,
        ``source.platform``, ``source.original_file``, ``created`` — and
        never of the body. Replacing bad body bytes therefore cannot
        change the outcome; it can only stop a matching fragment from
        escaping the purge with its private body intact, which is the
        right-to-be-forgotten failure this exists to close (#910).

        Args:
            path: Path to a markdown file.

        Returns:
            Parsed :class:`frontmatter.Post` — lossy in the body when the
            file was not valid UTF-8 — or ``None`` when the frontmatter
            cannot be parsed at all. ``None`` means "matches nothing", so
            an unreadable file is left on disk rather than deleted.
        """
        try:
            return frontmatter.load(str(path))
        except UnicodeDecodeError:
            # Degrading to a lossy read on an RTBF-critical path is an
            # operator-visible event, so name the file at WARNING.
            logger.warning(
                "Body of %s is not valid UTF-8; re-reading it lossily to test it "
                "against the purge criteria (the file itself is never rewritten)",
                path,
            )
            return self._load_frontmatter_lossily(path)
        except FRONTMATTER_LOAD_ERRORS as exc:
            # Type name only — never str(exc); see _load_frontmatter.
            logger.warning(
                "Unable to parse frontmatter in %s (%s); leaving it untouched",
                path,
                type(exc).__name__,
            )
            return None
        except _FRONTMATTER_RESOURCE_ERRORS as exc:
            _warn_frontmatter_defeated_the_parser(path, exc)
            return None

    def _load_frontmatter_lossily(self, path: Path) -> frontmatter.Post | None:
        """Re-read an undecodable file, replacing its bad bytes with U+FFFD.

        The retry half of :meth:`_load_frontmatter_for_match`, and subject
        to the same prohibition: the returned post's content is lossy and
        must never be written back to disk.

        Args:
            path: Path to a markdown file that failed to decode as UTF-8.

        Returns:
            Parsed :class:`frontmatter.Post` with invalid bytes replaced,
            or ``None`` when the file is *also* unparseable as
            frontmatter and so can match no purge criteria.
        """
        try:
            # Go through the file object rather than decoding bytes and
            # calling ``frontmatter.loads``: ``frontmatter.load`` is itself
            # ``open(...)`` + ``loads(text)``, so this keeps it the single
            # parse entry point and preserves universal-newline handling,
            # leaving a CRLF vault file identical on both paths.
            with path.open(encoding="utf-8", errors="replace") as handle:
                return frontmatter.load(handle)
        except FRONTMATTER_LOAD_ERRORS as exc:
            # Both undecodable *and* unparseable: no criteria can match.
            logger.warning(
                "Unable to parse frontmatter in %s (%s); leaving it untouched",
                path,
                type(exc).__name__,
            )
            return None
        except _FRONTMATTER_RESOURCE_ERRORS as exc:
            _warn_frontmatter_defeated_the_parser(path, exc)
            return None

    def _scrub_references(
        self,
        *,
        title: str,
        fragment_id: str,
        exclude: Path,
    ) -> tuple[int, int, int]:
        """Scrub links and fragment-ID provenance in a single vault walk.

        Walks every ``.md`` file in the vault exactly once (instead of
        once per scrub type) and applies both substitutions in-memory
        before writing:

        * Wiki-links pointing at the deleted fragment are removed. They
          match by *name*, not by ID, so this pass is still needed
          alongside the provenance one — and by every name the page
          answers to, in every case Obsidian resolves, without eating a
          spelling a surviving page claims. See :class:`_WikilinkScrub`
          for the whole of that decision (#903).
        * Markdown links — ``[text](Secret%20Title.md)`` — pointing at
          the deleted fragment are removed, matched on their **target**
          and never on their display text (#1622). This is the shape
          ``BrokenLinkScanner`` has always counted as a vault link while
          this scrub did not.
        * Bare fragment-ID occurrences are replaced with the
          :data:`PURGED_MARKER` placeholder. This catches YAML
          provenance list entries (``source_fragments: [frag-…]`` in
          drafts and mining ideas) and prose mentions of the ID in the
          body in the same pass.

        The walk covers all ``.md`` files in the vault, including
        derived content under ``04-Praxis``, ``05-Wavelength``,
        ``07-Voice/Drafts``, ``08-Decisions``, and the deployed skill
        tree under ``00-Creek-Meta/Skills``. The compliance audit log
        at ``00-Creek-Meta/audit/purge.jsonl`` is a JSONL file (not
        Markdown) so the recursive walk naturally excludes it — the
        audit's ``affected_fragments`` list keeps the real ID for
        compliance reconstruction.

        Args:
            title: The fragment title whose wiki-links should be removed
                (empty string skips the wiki-link pass).
            fragment_id: The exact fragment ID to scrub (empty string
                skips the provenance pass).
            exclude: Path to skip (the file currently being deleted).

        Returns:
            A ``(wikilinks_removed, markdown_links_removed,
            provenance_scrubbed)`` count triple.
        """
        prov_pattern = _build_provenance_pattern(fragment_id) if fragment_id else None
        if not (title or exclude.stem or prov_pattern):
            return 0, 0, 0
        # One walk, reused twice: the claimant scan below reads only the
        # header of each file, and repeating the walk to get at them
        # would break this pass's pinned once-per-fragment property.
        md_files = self._list_vault_md_files()
        wiki_scrub = self._build_wikilink_scrub(title, exclude, md_files)
        if wiki_scrub is None is prov_pattern:
            return 0, 0, 0
        wiki_total = 0
        md_total = 0
        prov_total = 0
        for md_file in md_files:
            # Two separate concerns: *exclude* is the file being deleted
            # right now, while the ledger names the files an apply run
            # would already have deleted earlier in this operation — a
            # sibling fragment, a swept voice artifact — and which are
            # therefore still on disk only because this is a preview.
            if md_file == exclude or self._skip_as_removed(md_file):
                continue
            wiki_count, md_count, prov_count = self._scrub_one_file(
                md_file,
                wiki_scrub,
                prov_pattern,
            )
            wiki_total += wiki_count
            md_total += md_count
            prov_total += prov_count
        return wiki_total, md_total, prov_total

    def _build_wikilink_scrub(
        self,
        title: str,
        frag_file: Path,
        md_files: list[Path],
    ) -> _WikilinkScrub | None:
        """Build the wikilink matcher for the fragment being purged.

        Every name, not one (#903, widened by #1622). A page is linkable
        by its declared title, by its filename stem, *and* by each of its
        declared ``aliases`` — :func:`creek.vault.links.build_link_index`
        registers all three — and this vault's paths are title-derived,
        so ``01-Fragments/Journal/2026-03-11 therapy session.md`` is
        itself the private string a right-to-be-forgotten request is
        about. The scrub knew only ``post["title"]``, so on every
        fragment whose title and filename differ, a link written by the
        filename survived the purge verbatim.

        The name set is taken from :func:`creek.vault.links.page_names`
        rather than assembled here, because that is the exact function
        :meth:`_surviving_claimants` scans survivors with. Asking a
        narrower question of the purged page than of every other page in
        the vault is what left ``[[Codename Raven]]`` standing after a
        purge of a fragment declaring that alias (#1622). One reader,
        one answer, and a name creek adds to a page later is scrubbed
        without touching this method again.

        Reading the header is safe here: the whole scrub runs *before*
        ``frag_file.unlink()``, and before the dry-run ledger marks the
        file removed. A header that cannot be read yields
        ``[stem]`` — a superset of the old behaviour, never a narrower
        one, so a defeated parser cannot quietly shrink what a
        compliance purge removes.

        The purged page's **own** exact-case spellings are subtracted
        from the shelter, and that subtraction is load-bearing. The
        shelter exists for a *case variant* a surviving page claims;
        letting it cover the purged page's own spelling means any other
        ``.md`` in the vault naming the same string — a sibling fragment
        titled the same thing, an intimate stub, an Adepthood staging
        file this very purge is about to delete a moment later — turns
        the primary scrub off entirely, leaving ``[[Secret Title]]``
        standing after a right-to-be-forgotten request. That is a
        *regression* on the exact-case link the scrub removed before
        #903 widened it, so the widening must never cost it.

        Args:
            title: The purged fragment's declared title, or ``""``.
            frag_file: The fragment file being deleted. Its stem and its
                declared ``aliases`` are the page's other linkable
                names, both read off it by
                :func:`creek.vault.links.page_names`.
            md_files: The vault walk the caller has already performed,
                reused rather than repeated — one walk per fragment is a
                pinned property of this pass.

        Returns:
            A matcher, or ``None`` when the fragment has no linkable
            name at all and the wiki-link pass should be skipped.
        """
        own = {name for name in (title, *page_names(frag_file)) if name.strip()}
        folded = frozenset(name.casefold() for name in own)
        if not folded:
            return None
        return _WikilinkScrub(
            folded_names=folded,
            protected=self._surviving_claimants(folded, frag_file, md_files) - own,
        )

    def _surviving_claimants(
        self,
        folded: frozenset[str],
        exclude: Path,
        md_files: list[Path],
    ) -> frozenset[str]:
        """Return the exact-case spellings some surviving page claims.

        ``LinkIndex.resolve`` consults its exact-case index *before* the
        folded one, so a link spelled the way a surviving page spells
        itself names that page and no other. Scrubbing it would destroy
        an operator's live link on the strength of a case-insensitive
        near-miss — the over-matching direction, and the one a bare
        ``re.IGNORECASE`` fix takes (#903).

        Only names that could collide are collected, and only the
        **header** of each file is read, so this costs one short read
        per vault file rather than a second full pass.

        Args:
            folded: The case-folded names the purge is about to scrub.
            exclude: The fragment file being deleted, which claims
                nothing once the purge completes.
            md_files: Every markdown file in the vault.

        Returns:
            Exact-case names claimed by pages that outlive this purge.
            The caller subtracts the purged page's own spellings from
            this set before sheltering anything by it.
        """
        claimed: set[str] = set()
        for md_file in md_files:
            if md_file == exclude or self._skip_as_removed(md_file):
                continue
            claimed.update(
                name for name in page_names(md_file) if name.casefold() in folded
            )
        return frozenset(claimed)

    def _scrub_one_file(
        self,
        md_file: Path,
        wiki_scrub: _WikilinkScrub | None,
        prov_pattern: re.Pattern[bytes] | None,
    ) -> tuple[int, int, int]:
        """Apply the link and provenance scrubs to a single file.

        Split three ways — read, substitute, persist — because the two
        ends of it are where a dry run has to diverge from an apply run
        (#1340): the read may have to come from a rewrite this operation
        only *pretended* to make, and the write may have to become that
        pretence. The substitution in the middle is identical in both
        modes and is pure, so it lives outside the class entirely.

        Args:
            md_file: Markdown file to scrub.
            wiki_scrub: Link matcher, or ``None`` to skip both link passes.
            prov_pattern: Fragment-ID regex, or ``None`` to skip that pass.

        Returns:
            A ``(wikilinks_removed, markdown_links_removed,
            provenance_scrubbed)`` count triple for this file. All zeros
            when the file cannot be read at all.
        """
        data = self._scrub_input_bytes(md_file)
        if data is None:
            return 0, 0, 0
        scrubbed, wiki_count, md_count, prov_count = _apply_scrubs(
            data,
            wiki_scrub,
            prov_pattern,
        )
        if wiki_count or md_count or prov_count:
            self._persist_scrub(md_file, scrubbed)
        return wiki_count, md_count, prov_count

    def _scrub_input_bytes(self, md_file: Path) -> bytes | None:
        """Return the bytes the scrub should operate on, or ``None`` to skip.

        In a dry run the ledger wins when it holds a pending rewrite for
        *md_file*: an apply run would have written those bytes on an
        earlier fragment's pass, so reading the stale file instead makes
        the preview re-count references the real run had already
        scrubbed. In an apply run the ledger is never consulted at all,
        which is what keeps its contents unable to affect a real purge.

        ``read_bytes`` rather than ``read_text`` (#948). The text read
        raised ``UnicodeDecodeError`` on a file that is not valid UTF-8,
        and the only safe response *then* was to skip it — the caller
        writes the result back, so an ``errors="replace"`` read would
        have persisted U+FFFD over the operator's bytes. That left the
        purged title and fragment ID standing inside such a file after a
        right-to-be-forgotten request. Reading bytes removes both the
        exception and the trade-off, and takes the newline translation
        ``read_text`` performed with it.

        Args:
            md_file: Markdown file about to be scrubbed.

        Returns:
            The bytes to scrub, or ``None`` when the file cannot be read.
        """
        if self.dry_run:
            pending = self._ledger.bytes_for(md_file)
            if pending is not None:
                return pending
        try:
            return md_file.read_bytes()
        except OSError as exc:
            # Never raise: aborting here would leave the purge target
            # unlinked-but-still-referenced. Type name only, never
            # str(exc): the message can quote file content (possibly the
            # very secret being purged).
            logger.warning(
                "Skipping unreadable file during reference scrub: %s (%s)",
                md_file,
                type(exc).__name__,
            )
            return None

    def _persist_scrub(self, md_file: Path, data: bytes) -> None:
        """Write the scrubbed bytes, or record them as a dry run's pretence.

        The two branches are mutually exclusive by construction, and
        that matters beyond tidiness: buffering contents in an apply run
        would hold every rewritten file for the lifetime of the
        operation, which at 35k fragments is a memory blow-up in
        exchange for something no apply run ever reads.

        ``write_bytes`` rather than ``write_text`` (#948): the scrub is
        a substitution, so every byte it was not asked about — an
        invalid sequence, a CRLF line ending, a missing final newline —
        has to come back exactly as it went in.

        Args:
            md_file: Markdown file that was scrubbed.
            data: Its scrubbed contents.
        """
        if self.dry_run:
            self._ledger.set_bytes(md_file, data)
            return
        md_file.write_bytes(data)

    def _decrement_counts(
        self,
        subfolder: str,
        ids: list[str],
    ) -> int:
        """Decrement ``fragment_count`` on thread/eddy files.

        The engine's **third** vault walk, and the one with the sharpest
        edge (#1454 follow-up): a match here ends in
        :meth:`_rewrite_fragment_count`'s ``write_bytes``, which follows
        a symlink and writes the *target*. A planted
        ``02-Threads/alias.md -> /elsewhere/file.md`` whose ``id`` a
        purged fragment declares in ``threads``/``eddies`` therefore got
        an arbitrary out-of-vault file overwritten. The issue reasoned
        that purge cannot write outside the vault because ``unlink``
        drops the alias rather than its target — true of the delete
        paths, and false of this one.

        So the walk goes through :func:`_contained_md_files`, the same
        fail-closed primitive the fragment census and the reference
        scrub use, rather than a second hand-rolled containment check.

        Args:
            subfolder: ``02-Threads`` or ``03-Eddies``.
            ids: IDs of files whose counts should decrement.

        Returns:
            Number of files updated.
        """
        if not ids:
            return 0
        folder = self.vault_path / subfolder
        if not folder.is_dir():
            return 0
        id_set = set(ids)
        updated = 0
        for md_file in _contained_md_files(folder, vault_path=self.vault_path):
            meta = read_header_meta(md_file)
            if meta.get("id") not in id_set:
                continue
            raw_count = meta.get("fragment_count", 0)
            current = int(raw_count) if isinstance(raw_count, (int, float, str)) else 0
            if self._rewrite_fragment_count(md_file, max(0, current - 1)):
                updated += 1
        return updated

    def _rewrite_fragment_count(self, md_file: Path, new_count: int) -> bool:
        """Splice *new_count* into *md_file*'s header, or report that it cannot.

        The read is header-only and the write is a byte splice, so a
        thread whose *body* is not valid UTF-8 gets its count corrected
        like any other (#949) — the header is clean ASCII, which is
        exactly why the old whole-file round trip failing on it was a
        loss rather than a necessity (#910).

        The dry-run branch is deliberately not the scrub path's: no
        ledger is involved here, because nothing later in a purge reads
        a thread's fragment count back. It still performs the splice, so
        a preview counts a rewrite only when the apply run could really
        make it.

        Args:
            md_file: Thread or eddy file to update.
            new_count: The decremented count.

        Returns:
            ``True`` when the count was written (or, in a dry run, could
            have been), ``False`` when the file was left alone.
        """
        try:
            data = md_file.read_bytes()
        except OSError as exc:
            # Type name only, never str(exc): the message can quote file
            # content (possibly the very secret being purged).
            logger.warning(
                "Skipping unreadable file during count update: %s (%s)",
                md_file,
                type(exc).__name__,
            )
            return False
        spliced = _splice_fragment_count(data, new_count)
        if spliced is None:
            return self._reserialise_fragment_count(md_file, new_count)
        if not self.dry_run:
            md_file.write_bytes(spliced)
        return True

    def _reserialise_fragment_count(self, md_file: Path, new_count: int) -> bool:
        """Write the count by rewriting the whole header, the last resort.

        Reached only when :func:`_splice_fragment_count` declined — the
        key is missing, or holds something that is not a plain integer
        (a list, a null, a multi-line scalar). Such a header is already
        not in the shape a splice can preserve, and setting the count is
        the repair; this path is what wrote it before #949, and the
        contract that a non-numeric ``fragment_count`` becomes ``0``
        rather than a crash predates that issue.

        The cost is real and is why it is a fallback and not the
        default: PyYAML drops comments, alphabetises keys, normalises
        quoting and strips trailing whitespace, so a file that lands
        here comes back reformatted. It is logged for that reason.

        Args:
            md_file: Thread or eddy file to update.
            new_count: The decremented count.

        Returns:
            ``True`` when the header was rewritten (or, in a dry run,
            could have been); ``False`` when it could not be read
            losslessly, which is #910's rule and still holds here.
        """
        post = self._load_frontmatter(md_file)
        if post is None:
            return False
        logger.warning(
            "Reserialising %s to set fragment_count: its existing value is not a "
            "plain integer, so the header cannot be updated in place",
            md_file,
        )
        post["fragment_count"] = new_count
        if not self.dry_run:
            md_file.write_text(frontmatter.dumps(post), encoding="utf-8")
        return True

    def _reset_classifications(self, post: frontmatter.Post) -> bool:
        """Reset classification fields on a frontmatter post.

        Args:
            post: Frontmatter to mutate in-place.

        Returns:
            True if any classification field was actually changed.
        """
        changed = False
        for field_name in _CLASSIFICATION_RESET_FIELDS:
            default = _CLASSIFICATION_DEFAULTS[field_name]
            if post.get(field_name) != default:
                post[field_name] = default
                changed = True
        return changed

    def _wipe_folder_contents(
        self,
        folder_path: Path,
        result: PurgeResult,
    ) -> None:
        """Remove every file/subdirectory inside a vault folder.

        What is *deleted* is unchanged: each top-level entry goes in one
        ``rmtree`` or ``unlink``, which is why the walk stays top-level.
        What is *recorded* is not: ``deleted_files`` now names the
        regular files that were destroyed, recursively, instead of the
        directories that held them (#1340). A directory path in a
        right-to-be-forgotten deletion record names no destroyed
        content, and an empty directory destroyed none — so it
        contributes no entry while still being removed from disk.

        Args:
            folder_path: Folder whose contents should be removed.
            result: Result accumulator to update with deleted paths.
        """
        for entry in sorted(folder_path.iterdir()):
            result.deleted_files.extend(_regular_files_under(entry))
            if self.dry_run:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def _run_audited(
        self,
        result: PurgeResult,
        body: Callable[[], None],
    ) -> PurgeResult:
        """Wrap a purge body in the GAP-002 intent → outcome audit pair.

        Emits an ``intent`` entry before invoking *body*, then an
        ``outcome`` entry after — ``status="complete"`` on success,
        ``status="partial"`` (with the exception **type name only** in
        ``failure_reason``) when *body* raises. The exception then
        propagates so callers see the failure.

        A body that returns normally can still have left the erasure
        incomplete: an undecodable fragment body skips the content-keyed
        voice sweep (see :meth:`_voice_match_body`). That is recorded on
        ``voice_body_undecodable``, and :attr:`PurgeResult.outcome_status`
        — the single definition of the distinction, shared with the MCP
        tool payload — downgrades the outcome to
        ``status="partial"`` too, because "the operation finished" and
        "everything it promised to erase is gone" are different claims
        and the audit log must not conflate them.

        The message is deliberately dropped: the audit log is preserved
        by every purge, including ``purge_vault``, so anything written
        there outlives the right-to-be-forgotten request that produced
        it — and exception text routinely quotes vault-derived content
        (a title-derived filename in an ``OSError``, a source snippet
        in a ``yaml.MarkedYAMLError``). The type is the forensic value;
        the message is the leak. Callers that need the detail still get
        it from the propagating exception.

        The intent entry is the engine's recovery contract: even a
        SIGKILL between the intent write and the first destructive op
        leaves a forensic trail naming what was being attempted.

        This is also the operation boundary for the dry-run ledger
        (:class:`~creek.purge.dryrun.DryRunLedger`), which is rebuilt
        here so no two operations on one engine share bookkeeping.

        Args:
            result: Result accumulator the body will populate.
            body: Callable that does the actual destructive work and
                mutates *result* in place.

        Returns:
            The same ``result`` with counts populated.

        Raises:
            Exception: Whatever *body* raises, after the partial
                outcome line has been written.
        """
        operation_id = uuid.uuid4().hex
        # One ledger per operation. Carrying the previous call's over
        # would hide a file from the next preview that is still sitting
        # on disk, so a sequence of dry runs on one engine would drift
        # further from the truth with every call (#1340).
        self._ledger = DryRunLedger()
        self._write_intent_audit(result, operation_id)
        try:
            body()
        except BaseException as exc:
            failure_reason = type(exc).__name__
            self._write_outcome_audit(
                result,
                operation_id,
                status="partial",
                failure_reason=failure_reason,
            )
            raise
        status = result.outcome_status
        if status == "partial":
            self._write_outcome_audit(
                result,
                operation_id,
                status=status,
                failure_reason=_VOICE_BODY_UNDECODABLE,
            )
            return result
        self._write_outcome_audit(result, operation_id, status=status)
        return result

    def _write_intent_audit(
        self,
        result: PurgeResult,
        operation_id: str,
    ) -> None:
        """Append the GAP-002 ``intent`` entry for *result*.

        The intent line captures the *planned* scope (operation name,
        criteria, dry-run flag). Counts are deliberately zero — they
        are not known until the body runs.

        Args:
            result: Result accumulator carrying the planned criteria.
            operation_id: UUID4 hex linking this intent to its outcome.
        """
        self._validate_operation(result.operation)
        entry = PurgeAuditEntry(
            operation=result.operation,
            criteria=result.criteria.copy(),
            dry_run=result.dry_run,
            phase="intent",
            operation_id=operation_id,
        )
        self.audit_log.append(entry)

    def _write_outcome_audit(
        self,
        result: PurgeResult,
        operation_id: str,
        *,
        status: PurgeOutcomeStatus,
        failure_reason: str | None = None,
    ) -> None:
        """Append the GAP-002 ``outcome`` entry for *result*.

        ``fragments_deleted`` counts only operations that remove files
        from disk (see :data:`_FILE_DELETING_OPERATIONS`);
        ``classifications`` resets metadata in place and therefore
        reports zero deletions. ``embeddings_removed`` is the *real*
        number of rows the engine just dropped from
        ``<vault>/00-Creek-Meta/embeddings.parquet`` (GAP-001) — zero
        when the cache had not been built yet, zero for metadata-only
        operations, and the actual row delta otherwise.

        Args:
            result: The result whose metadata should be logged.
            operation_id: UUID4 hex matching the paired intent entry.
            status: ``"complete"`` if the body ran to the end, or
                ``"partial"`` if it raised.
            failure_reason: The exception **type name only** (e.g.
                ``"OSError"``) when ``status="partial"``; ``None``
                otherwise. The message is omitted because the audit log
                survives every purge, so vault-derived text quoted in an
                exception would outlive the right-to-be-forgotten
                request that produced it.

        Raises:
            ValueError: If ``result.operation`` is not a known purge
                operation. Unknown operations are rejected explicitly
                so a new operation type cannot silently default to
                ``deletes_files=True``.
        """
        deletes_files = self._validate_operation(result.operation)
        fragments_deleted = result.fragments_affected if deletes_files else 0
        entry = PurgeAuditEntry(
            operation=result.operation,
            criteria=result.criteria.copy(),
            affected_fragments=result.affected_fragment_ids.copy(),
            fragments_deleted=fragments_deleted,
            references_scrubbed=result.wikilinks_removed,
            markdown_links_removed=result.markdown_links_removed,
            embeddings_removed=result.embeddings_removed,
            provenance_scrubbed=result.provenance_scrubbed,
            intimate_stubs_removed=result.intimate_stubs_removed,
            journal_staged_removed=result.journal_staged_removed,
            voice_artifacts_removed=result.voice_artifacts_removed,
            ledger_rows_removed=result.ledger_rows_removed,
            meta_artifacts_removed=result.meta_artifacts_removed,
            dry_run=result.dry_run,
            phase="outcome",
            operation_id=operation_id,
            status=status,
            failure_reason=failure_reason,
        )
        self.audit_log.append(entry)

    def _validate_operation(self, operation: str) -> bool:
        """Confirm *operation* is known and return whether it deletes files.

        Args:
            operation: The operation name to validate.

        Returns:
            ``True`` when the operation removes files from disk,
            ``False`` for metadata-only operations.

        Raises:
            ValueError: When *operation* is neither in
                :data:`_FILE_DELETING_OPERATIONS` nor the metadata-only
                allowlist.
        """
        if operation in _FILE_DELETING_OPERATIONS:
            return True
        if operation == "classifications":
            return False
        msg = (
            f"Unknown purge operation {operation!r}; classify it "
            f"explicitly in _FILE_DELETING_OPERATIONS or add a metadata-only "
            f"branch before logging."
        )
        raise ValueError(msg)

    def _write_audit(self, result: PurgeResult) -> None:
        """Backward-compat shim that writes a single outcome entry.

        Pre-existing callers (and a small handful of tests) treat
        ``_write_audit`` as "emit one final audit line". The GAP-002
        contract has the engine emit *two* lines per operation via
        :meth:`_run_audited`. This shim is preserved so direct callers
        keep working: they get an outcome line with a fresh
        ``operation_id`` and ``status="complete"``, no paired intent.

        Args:
            result: Result whose metadata should be logged.
        """
        self._write_outcome_audit(result, uuid.uuid4().hex, status="complete")


_CLASSIFICATION_DEFAULTS: dict[str, dict[str, object]] = {
    "frequency": {"primary": "unclassified", "secondary": []},
    "wavelength": {
        "phase": "unclassified",
        "mode": "unclassified",
        "orientation": "unclassified",
        "dosage": "unclassified",
        "color": "unclassified",
        "descriptor": "",
    },
    "voice": {"voice_register": None, "confidence": None},
}
"""Default values applied to classification fields during reset."""


def _extract_source_platform(post: frontmatter.Post) -> str | None:
    """Extract the ``source.platform`` string from frontmatter.

    Args:
        post: Parsed frontmatter post.

    Returns:
        The source platform identifier, or ``None`` when missing.
    """
    source = post.get("source")
    if isinstance(source, dict):
        platform = source.get("platform")
        return str(platform) if platform is not None else None
    return None


def _extract_source_original_file(post: frontmatter.Post) -> str | None:
    """Extract the ``source.original_file`` string from frontmatter (INC-008).

    Args:
        post: Parsed frontmatter post.

    Returns:
        The original file path, or ``None`` when missing.
    """
    source = post.get("source")
    if isinstance(source, dict):
        original = source.get("original_file")
        return str(original) if original is not None else None
    return None


def _extract_source_origin_key(post: frontmatter.Post) -> str | None:
    """Extract the ``source.origin_key`` string from frontmatter (#845).

    ``origin_key`` is the vault-relative source-ledger key the ingest
    pipeline records on each fragment; for journal fragments it names
    the staged full-body file under ``00-Creek-Meta/adepthood/journal/``.

    Args:
        post: Parsed frontmatter post.

    Returns:
        The origin key, or ``None`` when missing.
    """
    source = post.get("source")
    if isinstance(source, dict):
        origin_key = source.get("origin_key")
        return str(origin_key) if origin_key is not None else None
    return None


SOURCE_PATH_MATCH_MODES: frozenset[str] = frozenset(
    {"exact", "substring", "regex"},
)
"""Canonical set of valid ``--match`` modes for ``purge source --source-path``.

Exported (no leading underscore) so the CLI layer can re-use the same
constant rather than duplicating it. The CLI iterates this set when
rendering its own usage error and the engine validates against it
inside :meth:`PurgeEngine.purge_source_path`. INC-008.
"""


def _build_source_path_matcher(
    source_path: str,
    *,
    match: str,
    vault_path: Path | None = None,
) -> Callable[[str], bool]:
    """Build a per-fragment predicate from *source_path* and *match* mode.

    **``exact`` accepts either spelling of the same source (#1575).** Since
    ingest stores :func:`creek.ingest.pipeline.derive_source_key`'s output
    rather than the host path, an operator's right-to-be-forgotten command —
    which names the path they know, the absolute one on their disk — would
    otherwise match zero fragments and report success. No frontmatter
    migration ships with that change, so a real vault holds both spellings at
    once; reconciling only one direction would break whichever half the
    operator happened to have. The derivation runs once, here, rather than
    per fragment.

    ``substring`` and ``regex`` are deliberately left alone: deriving a key
    from a fragment of a path, or from a regex, is not a meaningful operation,
    and an operator reaching for those modes is already asking for a literal
    text match against what the vault stores.

    Args:
        source_path: Path string the operator passed.
        match: One of ``"exact"`` / ``"substring"`` / ``"regex"``.
        vault_path: Vault root, enabling the derived-spelling arm of
            ``exact``. ``None`` keeps the literal-only comparison, for a
            caller with no vault in hand.

    Returns:
        A callable that takes a fragment's ``source.original_file`` and
        returns ``True`` when the fragment should be purged.

    Raises:
        ValueError: When *match* is unrecognised, or when
            ``match="regex"`` and *source_path* is not a valid regex.
    """
    if match not in SOURCE_PATH_MATCH_MODES:
        msg = (
            f"Unknown match mode {match!r}; expected one of "
            f"{sorted(SOURCE_PATH_MATCH_MODES)}."
        )
        raise ValueError(msg)
    if match == "exact":
        spellings = {source_path}
        if vault_path is not None:
            from creek.ingest.pipeline import derive_source_key

            spellings.add(derive_source_key(source_path, vault_path))
        return lambda candidate: candidate in spellings
    if match == "substring":
        return lambda candidate: source_path in candidate
    # match == "regex"
    try:
        pattern = re.compile(source_path)
    except re.error as exc:
        msg = f"Invalid regex {source_path!r}: {exc}"
        raise ValueError(msg) from exc
    return lambda candidate: pattern.search(candidate) is not None


def _build_provenance_pattern(fragment_id: str) -> re.Pattern[bytes]:
    """Build a regex matching a bare ``fragment_id`` but not hyphen-suffixed IDs.

    Compiled against **bytes**, like the rest of the scrub (#948), which
    switches ``\\w`` from Unicode to ASCII semantics. A fragment ID is an
    ASCII slug and so is every character that can continue one, so the
    "is this the prefix of a longer ID" question the lookarounds exist to
    ask is unchanged.

    A plain ``\\b`` word boundary treats the hyphen as a word/non-word
    boundary, so ``\\bfrag-01\\b`` would match the ``frag-01`` prefix
    inside ``frag-01-extended``. The lookarounds below treat both
    word characters *and* ``-`` as ID continuation, so a trailing or
    leading hyphen marks a longer ID and is left untouched.

    Args:
        fragment_id: The exact fragment ID to match.

    Returns:
        A compiled regex pattern.
    """
    escaped = re.escape(fragment_id.encode())
    return re.compile(rb"(?<![\w-])" + escaped + rb"(?![\w-])")


def _coerce_date(value: object) -> date | None:
    """Coerce a frontmatter value into a date, if possible.

    Accepts :class:`datetime`, :class:`date`, or ISO-format strings.

    Args:
        value: Frontmatter value to coerce.

    Returns:
        A ``date``, or ``None`` if ``value`` cannot be coerced.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC).date()
        except ValueError:
            return None
    return None
