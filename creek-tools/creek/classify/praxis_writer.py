"""Materialise ``04-Praxis/`` pages from explicit-praxis fragments (#2122).

:mod:`creek.classify.praxis_pass` stamps ``praxis_potential: explicit`` on a
fragment that writes a practice down, but until #2122 nothing turned that
verdict into a :class:`~creek.models.Praxis` entity:
:meth:`creek.vault.writer.VaultWriter.write_praxis` had no caller, so
``04-Praxis/`` stayed empty in every vault and
``ReflectionResponse.related_praxis`` (:mod:`creek_mcp.compiled_pages`) could
never return anything. This module is that missing writer pass. Both
orchestrators — the ``creek classify`` engine (and through it ``POST
/v1/classifications``) and the ``creek process`` pipeline — call
:func:`write_praxis_pages` while they still hold the vault content-mutation
lock, so a page is written in the same load-to-write window as the verdict
that justifies it.

Rules this pass keeps:

* **Only ``explicit``.** ``latent`` is a practice the author has not named
  (see :mod:`creek.classify.praxis_pass`); proposing one would be guessing.
* **Proposed, never promoted.** Every page is written ``status: proposed``.
  Only the owner moves it on.
* **Never a second page, never a rewrite.** Dedupe is by ``derived_from``
  across the whole of ``04-Praxis/``, whatever a page's status or subfolder.
  A fragment that already has a page — written by an earlier run, moved by
  the owner, promoted to ``active`` — is skipped, so an owner's edit is never
  overwritten and a re-run converges.
* **The source's tier, never a lower one.** The page copies its source's
  first sentence, so it is stamped with the source's ``privacy_tier`` read
  fail-closed off raw frontmatter (missing → ``intimate``). The read-side
  gate in :mod:`creek_mcp.compiled_pages` still re-checks every contributor;
  the stamp is what any other whole-vault walker sees.
* **Erasure follows provenance.** A page whose *only* source is a purged
  fragment is deleted by :class:`creek.purge.engine.PurgeEngine`, because its
  title and body are that fragment's words.

``praxis_type`` comes from a small keyword map (no classifier supplies one):
habit language → ``habit`` (``Daily/``), commitment language → ``commitment``
(``Seasonal/``), otherwise ``insight`` (``Situational/``). Habit is checked
first so "I'm committing to a morning sit" is a daily practice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from creek._containment import escaping_child
from creek.classify.privacy_filter import raw_privacy_tier
from creek.models import Praxis, PraxisPotential, PraxisType
from creek.vault.links import read_header_meta
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS, try_load_fragment
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment

logger = logging.getLogger(__name__)

PRAXIS_TITLE_MAX_CHARS: Final[int] = 80
"""Longest title a generated page may carry (the issue's bound)."""

PRAXIS_ID_PREFIX: Final[str] = "praxis-"
"""Prefix of a generated page's id; the rest is the source fragment's id."""

_FRAGMENTS_RELDIR: Final[str] = "01-Fragments"
"""Where the source fragments live."""

_PRAXIS_RELDIR: Final[str] = "04-Praxis"
"""Where praxis pages live, in any subfolder."""

_HABIT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:daily|habit|habits|routine|morning|mornings|nightly|"
    r"(?:every|each)\s+(?:day|morning|evening|night))\b",
    re.IGNORECASE,
)
"""Language naming a recurring, day-scale practice → ``habit``."""

_COMMITMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:commit|commits|committed|committing|commitment|vow|vows|vowed)\b",
    re.IGNORECASE,
)
"""Language naming a pledge → ``commitment``."""

_LINE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:[#>]+[ \t]*|[-*+][ \t]*(?:\[[ xX]\][ \t]*)?)",
    re.MULTILINE,
)
"""Markdown heading, quote, list and task-checkbox prefixes on a line."""

_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(r"[.!?](?=\s|$)")
"""A sentence terminator followed by whitespace or the end of the text."""


@dataclass(frozen=True)
class PraxisWriteSummary:
    """What one :func:`write_praxis_pages` pass did.

    Attributes:
        written: Pages this pass created.
        errors: One message per page that could not be written. Each names
            the source fragment id and the exception type only — never a
            path or message, since both can carry vault content.
    """

    written: int
    errors: tuple[str, ...]


def praxis_type_for(body: str) -> PraxisType:
    """Map a fragment body onto a :class:`~creek.models.PraxisType`.

    Args:
        body: The fragment's markdown body.

    Returns:
        ``HABIT`` for habit language, else ``COMMITMENT`` for pledge
        language, else ``INSIGHT``.
    """
    if _HABIT_RE.search(body):
        return PraxisType.HABIT
    if _COMMITMENT_RE.search(body):
        return PraxisType.COMMITMENT
    return PraxisType.INSIGHT


def _bounded(text: str) -> str:
    """Return *text* cut to :data:`PRAXIS_TITLE_MAX_CHARS`, on a word if possible.

    Args:
        text: Whitespace-collapsed prose.

    Returns:
        *text* unchanged when it fits; otherwise its longest prefix within the
        cap that ends before a space, or a hard cut when there is no space.
    """
    if len(text) <= PRAXIS_TITLE_MAX_CHARS:
        return text
    cut = text[: PRAXIS_TITLE_MAX_CHARS + 1].rfind(" ")
    if cut <= 0:
        return text[:PRAXIS_TITLE_MAX_CHARS]
    return text[:cut].rstrip()


def praxis_title(fragment: Fragment, body: str) -> str:
    """Name a praxis after its source's first sentence.

    Args:
        fragment: The source fragment; its title is the fallback.
        body: The fragment's markdown body.

    Returns:
        The first sentence of *body*'s first line of prose (markdown line
        markers stripped, whitespace collapsed; a line break ends a sentence
        too), bounded to :data:`PRAXIS_TITLE_MAX_CHARS`.
        Falls back to the fragment's own title when the body has no prose.
    """
    lines = (
        " ".join(line.split()) for line in _LINE_MARKER_RE.sub("", body).splitlines()
    )
    prose = next((line for line in lines if line), "")
    end = _SENTENCE_END_RE.search(prose)
    sentence = prose[: end.end()] if end else prose
    return _bounded(sentence or " ".join(fragment.title.split()))


def praxis_from_fragment(fragment: Fragment, body: str) -> Praxis:
    """Build the proposed :class:`~creek.models.Praxis` for *fragment*.

    Args:
        fragment: An explicit-praxis source fragment.
        body: Its markdown body.

    Returns:
        A ``proposed`` praxis derived from exactly that fragment, with a
        deterministic id so the writer's own id index also refuses a twin.
    """
    return Praxis(
        id=f"{PRAXIS_ID_PREFIX}{fragment.id}",
        title=praxis_title(fragment, body),
        praxis_type=praxis_type_for(body),
        derived_from=[fragment.id],
    )


def praxis_page_sources(path: Path) -> list[str]:
    """Return the string ``derived_from`` ids of one praxis page.

    Args:
        path: A markdown file under ``04-Praxis``.

    Returns:
        The ids, or ``[]`` when the page is not ``type: praxis`` or its
        ``derived_from`` is not a list.
    """
    meta = read_header_meta(path)
    derived = meta.get("derived_from")
    if meta.get("type") != "praxis" or not isinstance(derived, list):
        return []
    return [entry for entry in derived if isinstance(entry, str)]


def derived_fragment_ids(vault_path: Path) -> set[str]:
    """Return every fragment id some ``04-Praxis`` page is derived from.

    Args:
        vault_path: Vault root.

    Returns:
        The union of every praxis page's ``derived_from`` ids, whatever the
        page's status or subfolder.
    """
    root = vault_path / _PRAXIS_RELDIR
    if not root.is_dir():
        return set()
    found: set[str] = set()
    for path in root.rglob("*.md"):
        found.update(praxis_page_sources(path))
    return found


def _explicit_candidates(vault_path: Path, skip: set[str]) -> list[Path]:
    """Return the fragment files whose header says ``explicit``, header-only.

    Args:
        vault_path: Vault root.
        skip: Fragment ids that already have a page.

    Returns:
        Sorted fragment paths worth loading in full.
    """
    root = vault_path / _FRAGMENTS_RELDIR
    if not root.is_dir():
        return []
    resolved_root = root.resolve(strict=False)
    candidates: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        if escaping_child(path, resolved_root):
            continue
        meta = read_header_meta(path)
        if meta.get("praxis_potential") != PraxisPotential.EXPLICIT.value:
            continue
        if meta.get("id") in skip:
            continue
        candidates.append(path)
    return candidates


def _write_one(writer: VaultWriter, path: Path, done: set[str]) -> str | None:
    """Write the page for one candidate fragment file.

    Args:
        writer: The vault writer.
        path: The candidate fragment file.
        done: Ids that already have a page; extended on success.

    Returns:
        The written source id, or ``None`` when nothing was written.

    Raises:
        OSError: When the page could not be written.
    """
    try:
        record = try_load_fragment(path)
    except FRONTMATTER_LOAD_ERRORS:
        logger.debug("Skipping unreadable praxis candidate: %s", path)
        return None
    if record is None:
        return None
    fragment, body, raw = record
    if fragment.id in done:
        return None
    writer.write_praxis(
        praxis_from_fragment(fragment, body),
        privacy_tier=raw_privacy_tier(raw),
    )
    done.add(fragment.id)
    return fragment.id


def write_praxis_pages(vault_path: Path) -> PraxisWriteSummary:
    """Write a proposed ``04-Praxis`` page for every explicit fragment lacking one.

    The caller must hold the vault content-mutation lock
    (:func:`creek.vault.mutations.hold_content_mutation`): this reads
    fragments and writes pages derived from them, the same load-to-write
    window classification itself holds the lock for.

    Args:
        vault_path: Vault root.

    Returns:
        How many pages were written and which sources failed.
    """
    done = derived_fragment_ids(vault_path)
    candidates = _explicit_candidates(vault_path, done)
    if not candidates:
        return PraxisWriteSummary(written=0, errors=())
    try:
        writer = VaultWriter(vault_path=vault_path)
    except FileNotFoundError:
        # Not a scaffolded vault (no ``00-Creek-Meta``): there is nowhere a
        # provenance-logged page may go. Classification itself does not need
        # the scaffold, so this is a skipped stage, not a failed pass.
        logger.warning(
            "Skipping %d praxis page(s): vault layout incomplete; run `creek init`.",
            len(candidates),
        )
        return PraxisWriteSummary(written=0, errors=())
    written = 0
    errors: list[str] = []
    for path in candidates:
        try:
            if _write_one(writer, path, done) is not None:
                written += 1
        except OSError as exc:
            # The id comes from the header; the message and path stay out,
            # since both can carry vault content.
            source = read_header_meta(path).get("id", "?")
            errors.append(f"praxis page not written for {source}: {type(exc).__name__}")
    return PraxisWriteSummary(written=written, errors=tuple(errors))
