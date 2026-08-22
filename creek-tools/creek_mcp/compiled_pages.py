"""The compiled layer — eddies and praxis — as seen through a tier ceiling (#873).

``creek.reflect`` grounds its margin notes in the corpus fragments nearest an
entry. Those fragments *belong to* compiled structures — ``03-Eddies/`` topic
clusters they carry ``eddies:`` wiki-links to, and ``04-Praxis/`` pages whose
``derived_from`` names them — and this module is the one place that turns a set
of seed fragment ids into the bounded, ceiling-admitted view of those
structures that :func:`creek_mcp.tools.reflect.reflect_tool` publishes as
``related_praxis`` / ``related_eddies``.

**A compiled page is a laundering risk, and is treated as one.** An eddy page's
``description`` and ``fragment_count``, and a praxis page's body, are
*synthesised from fragments* — fragments the requesting caller may have no
right to. Neither page carries a ``privacy_tier`` of its own (see
:class:`creek.models.Eddy` and :class:`creek.models.Praxis`, which have no such
field), so there is nothing on the page itself to rank. That is the same shape
#1013 / #1538 closed for drafts, and the rule here is the same one:

    **Provenance authorizes; seeds only select.**

A seed id decides which pages are *candidates*. Whether a candidate may be
returned is decided by enumerating **every** fragment that page was compiled
from and admitting the page only when all of them are admitted under the
caller's ceiling. A page whose provenance cannot be enumerated in full is
**opaque**, and an opaque page is withheld — "no provenance" is never read as
"no sources". Concretely, a page is withheld when:

- any contributing fragment's tier exceeds the ceiling
  (:func:`_provenance_admitted`);
- any contributing fragment id does not resolve to a fragment on disk at all,
  so its tier is unknown and unknowable;
- an eddy page's own ``fragment_count`` disagrees with the number of member
  fragments that can be enumerated, which is what an above-ceiling,
  hand-deleted, purged or unreadable member looks like from here;
- a praxis page declares no ``derived_from`` at all, or declares one carrying a
  non-string entry;
- the page's own front matter is missing, malformed, or names a ``type`` other
  than the one being looked for.

Because the tier of every contributor is re-checked against the ceiling here,
the seed ids need not themselves be trusted: handing this function an
above-ceiling id widens the *candidate* set and admits nothing.

**No egress, no embeddings, no second sweep.** Every lookup here is a read of
vault markdown through the tolerant shared readers
(:func:`creek.vault.reader.iter_vault_fragments`,
:func:`creek.vault.links.read_header_meta`), which skip an unreadable or
malformed file rather than raising. Nothing in this module opens a socket,
loads a model, or embeds anything — ADR-0004's local-only guarantee is
inherited by construction, not by a runtime check.

**Fail-closed tier reading.** Contributor tiers come from
:func:`creek_mcp.tier_ceiling.frontmatter_tier` over the *raw* front matter, so
a fragment with the ``privacy_tier`` key missing entirely ranks INTIMATE rather
than inheriting ``Fragment``'s ``unclassified`` default. A file the vault
cannot vouch for cannot contribute to a page a remote consumer is handed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from creek.models import PraxisStatus, PraxisType
from creek.vault.links import read_header_meta
from creek.vault.reader import iter_vault_fragments
from creek_mcp.tier_ceiling import frontmatter_tier, tier_allowed, tier_sensitivity

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from creek.models import PrivacyTier
    from creek_mcp.tier_ceiling import TierCeiling

logger = logging.getLogger(__name__)

MAX_RELATED_PRAXIS: Final[int] = 3
"""Most praxis pages one reflection may carry (issue #873's suggested bound)."""

MAX_RELATED_EDDIES: Final[int] = 2
"""Most eddy pages one reflection may carry (issue #873's suggested bound)."""

EXCERPT_CHARS: Final[int] = 240
"""Hard cap on a praxis excerpt, in characters.

A bound rather than a summary: the excerpt is the page's own opening prose,
whitespace-collapsed and truncated. It is only ever taken from a page whose
whole provenance is already admitted, so the cap is about response size, not
about admission.
"""

_FRAGMENTS_SUBDIR: Final[str] = "01-Fragments"
_EDDIES_SUBDIR: Final[str] = "03-Eddies"
_PRAXIS_SUBDIR: Final[str] = "04-Praxis"

_FENCE: Final[str] = "---"
_PRAXIS_TYPES: Final[frozenset[str]] = frozenset(member.value for member in PraxisType)
_PRAXIS_STATUSES: Final[frozenset[str]] = frozenset(
    member.value for member in PraxisStatus
)


class RelatedCompiled(NamedTuple):
    """The compiled structures one reflection may carry.

    Both lists are empty when nothing qualifies, which is what
    :func:`creek_mcp.tools.reflect.reflect_tool` renders as the fields being
    *absent* rather than present-and-empty.

    Attributes:
        praxis: At most :data:`MAX_RELATED_PRAXIS` ``{title, praxis_type,
            status, excerpt}`` mappings.
        eddies: At most :data:`MAX_RELATED_EDDIES` ``{title, description,
            fragment_count, formed}`` mappings.
    """

    praxis: list[dict[str, Any]]
    eddies: list[dict[str, Any]]


class _Corpus(NamedTuple):
    """The one corpus walk both selectors read.

    Attributes:
        tier_by_id: Every fragment id on disk, mapped to its fail-closed tier.
            An id absent from this mapping is one whose tier is unknowable, and
            no page depending on it may be published.
        eddy_members: Eddy title -> the ordered, deduplicated ids of the
            fragments carrying an ``eddies:`` wiki-link to it.
    """

    tier_by_id: dict[str, PrivacyTier]
    eddy_members: dict[str, list[str]]


class _EddyPage(NamedTuple):
    """A well-formed ``03-Eddies/`` page's published fields."""

    title: str
    description: str
    fragment_count: int
    formed: str


class _PraxisPage(NamedTuple):
    """A well-formed ``04-Praxis/`` page's published fields plus its sources."""

    title: str
    praxis_type: str
    status: str
    derived_from: tuple[str, ...]
    excerpt: str


def _wikilink_target(raw: object) -> str | None:
    """Return the page name a ``[[Wiki|link]]`` entry points at.

    Args:
        raw: One entry of a fragment's ``eddies:`` list. Non-strings are
            rejected rather than stringified, so ``eddies: [null]`` cannot
            invent a page named ``None``.

    Returns:
        The bare page name, or ``None`` when the entry names nothing.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = text.split("|", 1)[0].strip()
    return text or None


def _unique(ids: Iterable[str]) -> list[str]:
    """Return *ids* deduplicated, first occurrence first."""
    return list(dict.fromkeys(ids))


def _read_corpus(vault_path: Path) -> _Corpus:
    """Walk ``01-Fragments`` once and index tiers plus eddy membership.

    The walk is deliberately **unfiltered**: it must see the above-ceiling
    fragments too, because an eddy compiled from one of them is exactly the
    page that has to be withheld. Filtering here would leave the selectors
    blind to the content they exist to catch — the same reasoning
    :func:`creek.author.checks._resolve_cited_tiers` records for the leak gate.

    A fragment id seen on two files resolves **most-restrictive-wins**, so a
    duplicate cannot be used to launder the stricter copy's tier.

    Args:
        vault_path: Vault root.

    Returns:
        The tier index and the eddy membership map.
    """
    tier_by_id: dict[str, PrivacyTier] = {}
    members: dict[str, list[str]] = {}
    for _path, fragment, _body, raw in iter_vault_fragments(
        vault_path / _FRAGMENTS_SUBDIR
    ):
        tier = frontmatter_tier(raw)
        prior = tier_by_id.get(fragment.id)
        tier_by_id[fragment.id] = (
            tier if prior is None else max(prior, tier, key=tier_sensitivity)
        )
        for link in fragment.eddies:
            title = _wikilink_target(link)
            if title is not None:
                members.setdefault(title, []).append(fragment.id)
    return _Corpus(tier_by_id, {title: _unique(ids) for title, ids in members.items()})


def _provenance_admitted(
    ids: Iterable[str], corpus: _Corpus, ceiling: TierCeiling
) -> bool:
    """Return whether every contributing fragment is admitted under *ceiling*.

    This is the admission decision for a compiled page, and it is the assertion
    the whole module exists to make. An id that resolves to no fragment at all
    fails it: an unresolvable contributor has no tier, and a page with an
    untierable contributor is opaque, not clean.

    Args:
        ids: The fragment ids the page was compiled from.
        corpus: The indexed corpus walk.
        ceiling: The caller's declared ceiling.

    Returns:
        ``True`` only when every id resolves *and* ranks within *ceiling*.
    """
    for fragment_id in ids:
        tier = corpus.tier_by_id.get(fragment_id)
        if tier is None or not tier_allowed(tier, ceiling):
            return False
    return True


def _date_str(value: object) -> str:
    """Render a front-matter date as ``YYYY-MM-DD``, or ``""`` when unusable.

    PyYAML resolves an unquoted ``formed: 2026-03-04`` to a
    :class:`datetime.date`, while a quoted one stays a string; both are
    accepted, anything else is not.

    Args:
        value: The raw front-matter value.

    Returns:
        The ISO date, or ``""``.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return ""


def _body_of(text: str) -> str:
    """Return *text* with a leading YAML front-matter block removed.

    Args:
        text: The whole markdown file.

    Returns:
        The body. A file whose header never closes yields ``""`` rather than
        its own front matter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            return "\n".join(lines[index + 1 :])
    return ""


def _excerpt(path: Path) -> str:
    """Return the page's opening prose, whitespace-collapsed and bounded.

    Args:
        path: The markdown page.

    Returns:
        At most :data:`EXCERPT_CHARS` characters, or ``""`` for an unreadable
        file — never a raised exception, because this runs inside a tool whose
        contract is that no read error crosses the MCP boundary.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Type name only, never ``str(exc)``: these are user-managed vault
        # files and the message could carry their content into a log that
        # carries no tier.
        logger.debug("Skipping unreadable page %s (%s)", path, type(exc).__name__)
        return ""
    return " ".join(_body_of(text).split())[:EXCERPT_CHARS]


def _eddy_page(path: Path) -> _EddyPage | None:
    """Parse one ``03-Eddies/`` page, or ``None`` when it is not well-formed.

    Args:
        path: The markdown page.

    Returns:
        The page's published fields, or ``None`` when its ``type`` is not
        ``eddy``, its ``title`` or ``formed`` is missing, or its
        ``fragment_count`` is not a non-negative integer. ``fragment_count`` is
        load-bearing rather than cosmetic — :func:`_select_eddies` compares it
        against the members it can enumerate — so a page without a usable one
        cannot be published at all.
    """
    meta = read_header_meta(path)
    if meta.get("type") != "eddy":
        return None
    title = meta.get("title")
    count = meta.get("fragment_count")
    formed = _date_str(meta.get("formed"))
    description = meta.get("description")
    if not isinstance(title, str) or not title.strip() or not formed:
        return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return _EddyPage(
        title=title.strip(),
        description=description.strip() if isinstance(description, str) else "",
        fragment_count=count,
        formed=formed,
    )


def _praxis_page(path: Path) -> _PraxisPage | None:
    """Parse one ``04-Praxis/`` page, or ``None`` when it is not well-formed.

    Args:
        path: The markdown page.

    Returns:
        The page's published fields plus the ids it was derived from, or
        ``None``. ``derived_from`` must be a non-empty list of non-blank
        strings: an empty one is a page whose provenance cannot be enumerated,
        and a list carrying a non-string entry is one whose provenance cannot
        be enumerated *in full* — both are opaque, and neither may be
        published. ``praxis_type`` and ``status`` must name members of
        :class:`~creek.models.PraxisType` / :class:`~creek.models.PraxisStatus`,
        so a hand-edited vocabulary never reaches the published contract.
    """
    meta = read_header_meta(path)
    if meta.get("type") != "praxis":
        return None
    title = meta.get("title")
    praxis_type = meta.get("praxis_type")
    status = meta.get("status")
    derived = meta.get("derived_from")
    if not isinstance(title, str) or not title.strip():
        return None
    if praxis_type not in _PRAXIS_TYPES or status not in _PRAXIS_STATUSES:
        return None
    if not isinstance(derived, list) or not derived:
        return None
    ids = tuple(entry.strip() for entry in derived if isinstance(entry, str))
    if len(ids) != len(derived) or not all(ids):
        return None
    return _PraxisPage(
        title=title.strip(),
        praxis_type=str(praxis_type),
        status=str(status),
        derived_from=ids,
        excerpt=_excerpt(path),
    )


def _pages_under(root: Path) -> list[Path]:
    """Return the markdown files under *root*, sorted, or ``[]`` when absent."""
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


def _eddy_pages(vault_path: Path) -> dict[str, _EddyPage]:
    """Index ``03-Eddies/`` by title, dropping any title claimed twice.

    A title two pages both answer to is ambiguous, and an ambiguous title
    cannot be matched to the member set that authorizes it — so it is dropped
    rather than resolved by first- or last-wins.

    Args:
        vault_path: Vault root.

    Returns:
        Unambiguous eddy pages keyed by title.
    """
    pages: dict[str, _EddyPage] = {}
    ambiguous: set[str] = set()
    for path in _pages_under(vault_path / _EDDIES_SUBDIR):
        page = _eddy_page(path)
        if page is None:
            continue
        if page.title in pages:
            ambiguous.add(page.title)
        pages[page.title] = page
    for title in ambiguous:
        del pages[title]
    return pages


def _select_eddies(
    seeds: set[str], corpus: _Corpus, vault_path: Path, ceiling: TierCeiling
) -> list[dict[str, Any]]:
    """Return the admitted eddy pages the seed fragments belong to.

    Candidates are ranked by how many seeds they contain, ties broken by title,
    so the result is deterministic and independent of filesystem order.

    Args:
        seeds: Seed fragment ids.
        corpus: The indexed corpus walk.
        vault_path: Vault root.
        ceiling: The caller's declared ceiling.

    Returns:
        At most :data:`MAX_RELATED_EDDIES` published mappings.
    """
    pages = _eddy_pages(vault_path)
    overlaps = {
        title: len(seeds.intersection(members))
        for title, members in corpus.eddy_members.items()
        if title in pages and seeds.intersection(members)
    }
    selected: list[dict[str, Any]] = []
    for title in sorted(overlaps, key=lambda name: (-overlaps[name], name)):
        page = pages[title]
        members = corpus.eddy_members[title]
        # The completeness check. ``EddyDetector`` sets ``fragment_count`` to
        # exactly the member list it then writes the wiki-links for, so a
        # disagreement means a member is missing from the walk — deleted,
        # purged, unreadable, or simply never written — and a page compiled
        # from a fragment nobody can produce is opaque.
        if len(members) != page.fragment_count:
            continue
        if not _provenance_admitted(members, corpus, ceiling):
            continue
        selected.append(
            {
                "title": page.title,
                "description": page.description,
                "fragment_count": page.fragment_count,
                "formed": page.formed,
            }
        )
        if len(selected) >= MAX_RELATED_EDDIES:
            break
    return selected


def _select_praxis(
    seeds: set[str], corpus: _Corpus, vault_path: Path, ceiling: TierCeiling
) -> list[dict[str, Any]]:
    """Return the admitted praxis pages derived from the seed fragments.

    Args:
        seeds: Seed fragment ids.
        corpus: The indexed corpus walk.
        vault_path: Vault root.
        ceiling: The caller's declared ceiling.

    Returns:
        At most :data:`MAX_RELATED_PRAXIS` published mappings, ranked by seed
        overlap then title.
    """
    ranked: list[tuple[int, str, _PraxisPage]] = []
    for path in _pages_under(vault_path / _PRAXIS_SUBDIR):
        page = _praxis_page(path)
        if page is None:
            continue
        overlap = len(seeds.intersection(page.derived_from))
        if not overlap:
            continue
        if not _provenance_admitted(page.derived_from, corpus, ceiling):
            continue
        ranked.append((-overlap, page.title, page))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [
        {
            "title": page.title,
            "praxis_type": page.praxis_type,
            "status": page.status,
            "excerpt": page.excerpt,
        }
        for _overlap, _title, page in ranked[:MAX_RELATED_PRAXIS]
    ]


def related_compiled(
    seed_ids: Sequence[str], vault_path: Path, ceiling: TierCeiling
) -> RelatedCompiled:
    """Return the compiled structures nearest *seed_ids*, admitted by *ceiling*.

    Args:
        seed_ids: Fragment ids that *select* candidate pages — the reflected
            entry's own id and the ids the grounding pass retrieved. They carry
            no authority: every candidate's provenance is re-checked against
            *ceiling* regardless of which seed found it.
        vault_path: Vault root.
        ceiling: The caller's declared ceiling. A remote caller never reaches
            this with more than ``personal``, because
            :data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` caps the request
            before dispatch.

    Returns:
        The bounded, admitted view. Empty on both axes when there are no seeds
        — no seeds means nothing selected, never "select everything".
    """
    seeds = {seed for seed in seed_ids if seed}
    if not seeds:
        return RelatedCompiled([], [])
    corpus = _read_corpus(vault_path)
    return RelatedCompiled(
        praxis=_select_praxis(seeds, corpus, vault_path, ceiling),
        eddies=_select_eddies(seeds, corpus, vault_path, ceiling),
    )
