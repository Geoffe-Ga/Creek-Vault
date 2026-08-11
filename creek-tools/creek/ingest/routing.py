"""Decide which ingestor owns a source file when several claim it (#1304).

``Pipeline._run_ingestion`` hands the whole source tree to every entry in
:data:`creek.ingest.INGESTOR_REGISTRY`. That is deliberate — several
ingestors identify their input by *sniffing* it, not by its extension —
but it means a file claimed by two ingestors used to be written to the
vault twice. Nothing de-duplicated it downstream: the vault writer
de-collides a name clash with a ``-N`` suffix, so both copies landed.

This module supplies the arbiter. Every ingestor still runs, and then
:func:`arbitrate` groups the parsed fragments by
:attr:`~creek.ingest.base.ParsedFragment.source_path` and keeps only the
fragments of the highest-priority ingestor that actually produced output
for that path.

Why arbitrate after parsing rather than after ``discover()``
------------------------------------------------------------
A claim staked at discovery has to be trusted before anyone has read the
file. When the winner's ``parse()`` then fails — a malformed ``.csv``, a
password-protected ``.xlsx`` — the loser has already been discarded and
the file's content is lost, where today it still lands as a generic
fragment. Arbitrating on produced fragments makes that impossible by
construction: an ingestor that produced nothing for a path is not a
claimant. It also keeps today's memory profile, since one ingestor's
documents are freed before the next runs.

The enforced invariant is therefore "exactly one ingestor's output is
**written** per source file", not "each file is parsed once". A contested
*text* file is still parsed twice; that is CPU only. The one case where
the wasted read mattered — ``GenericIngestor`` slurping whole binaries it
was always going to discard — is fixed at its source in
:mod:`creek.ingest.generic`.

The priority order
------------------
:data:`CLAIM_PRIORITY` is a single total order over registry names rather
than a per-extension table, so it is reviewable in one glance and cannot
disagree with itself. It runs specific to general: structure- and
content-sniffed exporters first (they recognise a file by its internals,
so their claim is the strongest evidence available), then the
extension-keyed specialists, then ``generic`` last — ``generic`` is by
definition "nothing better claimed this" and must never beat a
specialist.

Each contested extension and the reason its winner wins:

* ``.md`` -> **markdown** over **code**. ``CodeIngestor`` claims READMEs,
  ``CLAUDE.md`` and ADRs (``creek/ingest/code.py`` ``_is_relevant_file``)
  and tags them with an ``artifact_type`` plus a git first-commit
  ``authored_at``. It also emits the file *verbatim*, so a README's YAML
  frontmatter stays inline in the fragment body — directly beneath the
  frontmatter block the vault writer adds, which is at best a stray
  horizontal rule and at worst two frontmatter blocks in one note.
  ``MarkdownIngestor`` splits that frontmatter out, promotes ``title``
  and ``tags`` from it, and treats every ``.md`` the same way. Losing an
  ``artifact_type`` label costs less than corrupting the body, and "every
  ``.md`` is ingested by the markdown ingestor" is a rule an operator can
  hold in their head.
* ``.py`` -> **code** over **generic**. Code emits one fragment per
  module and one per function; generic emits one fenced dump of the file.
* ``.html`` -> **substack** over **document** over **generic**. Substack
  recognises its own ``<postid>.<slug>.html`` naming and recovers post
  metadata; ``DocumentIngestor`` extracts readable text; ``generic``
  wraps the raw markup in a ```` ```html ```` fence, which is unreadable
  as a note. (``DocumentIngestor`` already stands aside for a real
  Substack export directory, so the full three-way only arises for a
  stray post file outside one.)
* ``.txt`` -> **document** over **generic**, and ADR ``.txt``/``.rst``
  -> **code** over **document**. Note the trade this makes on a plain
  ``.txt``: ``DocumentIngestor`` applies a heuristic that promotes the
  first line to an ``#`` heading, where ``generic`` reproduces the text
  verbatim. Verbatim is arguably the more faithful rendering of a log
  file, but a total order cannot prefer ``generic`` on ``.txt`` and
  ``document`` on ``.html``, and letting the declared fallback outrank a
  specialist anywhere makes the order incoherent.
* ``.csv``/``.xlsx`` -> **spreadsheet**, ``.pptx`` -> **presentation**,
  images -> **image**, each over **generic**, for the same reason.

The winner is load-bearing beyond the body text: a fragment's id hashes
``source_path + timestamp + content`` (``creek.ingest.base``), and the
ingestors disagree on both the body *and* the timestamp — ``CodeIngestor``
stamps local time while the others stamp UTC. So arbitration also picks
which timestamp survives, and with it the ``YYYY-MM-DD-`` prefix on the
vault filename. Between 16:00 and midnight Pacific the two candidates
fall on different calendar days.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from creek.ingest.base import ParsedFragment

logger = logging.getLogger(__name__)

CLAIM_PRIORITY: tuple[str, ...] = (
    # Tier 1 — structure- or content-sniffed exporters. These recognise a
    # file by its internals (a Discord ``messages/<channel>/messages.json``
    # tree, a ChatGPT top-level JSON list, a Claude export envelope, a
    # Substack post filename), so no extension table can express them.
    "discord",
    "chatgpt",
    "claude",
    "substack",
    # Tier 2 — extension-keyed specialists.
    "markdown",
    "code",
    "spreadsheet",
    "presentation",
    "image",
    "document",
    # Tier 3 — the fallback. Always last.
    "generic",
)
"""Total order over :data:`creek.ingest.INGESTOR_REGISTRY` keys, best first.

Every registry key must appear exactly once; ``tests/test_ingest_routing.py``
asserts that against the live registry, so adding a twelfth ingestor
without ranking it is a CI failure rather than a silent demotion.
"""

SKIPPED_DIRECTORY_NAMES: frozenset[str] = frozenset(
    {
        "node_modules",
        "vendor",
        ".git",
        "__pycache__",
        ".tox",
        ".venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".eggs",
    }
)
"""Directory names that are machinery rather than authored content.

Single definition, shared by ``CodeIngestor``'s discovery walk and by the
pipeline's unclaimed-source report, so the two cannot drift apart and
start disagreeing about whether a file was meant to be ingested.
"""

_BASE_RANKS: dict[str, int] = {name: rank for rank, name in enumerate(CLAIM_PRIORITY)}


class Arbitration(NamedTuple):
    """Outcome of resolving competing claims over one ingestion run.

    Attributes:
        winners: Ingestor name mapped to the fragments it keeps, in the
            order that ingestor produced them. Every key of the input
            mapping is present, possibly with an empty list.
        contested: Sorted source paths that more than one ingestor
            produced fragments for, and where output was therefore
            dropped. Reported so an operator can find the losing
            ingestor's stale fragments in a vault ingested before #1304.
    """

    winners: dict[str, list[ParsedFragment]]
    contested: list[str]


def _rank(claims: Mapping[str, Sequence[ParsedFragment]]) -> dict[str, int]:
    """Rank every claiming ingestor, placing unranked names after ``generic``.

    Names outside :data:`CLAIM_PRIORITY` — synthetic registries injected
    by tests, or a third-party ingestor — rank behind the whole known
    order, tie-broken by name so the result is deterministic. They still
    win any path nobody else claims.

    Args:
        claims: Ingestor name mapped to the fragments it produced.

    Returns:
        Ingestor name mapped to its rank; lower wins.
    """
    ranks = _BASE_RANKS.copy()
    unranked = sorted(name for name in claims if name not in _BASE_RANKS)
    for offset, name in enumerate(unranked, start=1):
        ranks[name] = len(CLAIM_PRIORITY) + offset
    return ranks


def _warn_unranked(contenders: set[str]) -> None:
    """Warn when an ingestor outside :data:`CLAIM_PRIORITY` lost a contest.

    Silent unless an unranked name actually competed: a registry holding
    only unranked names (every mock-registry test does) never contests
    anything and must not produce noise.

    Args:
        contenders: Names that took part in at least one contested path.
    """
    unranked = sorted(name for name in contenders if name not in _BASE_RANKS)
    if unranked:
        logger.warning(
            "Ingestor(s) %s are not listed in CLAIM_PRIORITY and were ranked "
            "after every known ingestor; add them to "
            "creek.ingest.routing.CLAIM_PRIORITY to control the outcome.",
            ", ".join(unranked),
        )


def arbitrate(claims: Mapping[str, Sequence[ParsedFragment]]) -> Arbitration:
    """Keep one ingestor's fragments per source file.

    For every distinct ``source_path`` across *claims*, the producing
    ingestor with the lowest :data:`CLAIM_PRIORITY` rank wins and keeps
    **all** of its fragments for that path — ``SpreadsheetIngestor``'s
    one-per-sheet output and ``CodeIngestor``'s per-function output are
    preserved intact. The invariant is one ingestor per source file, never
    one fragment per source file.

    An ingestor that produced no fragments for a path cannot win it, so a
    claimant whose parse failed silently forfeits to whoever succeeded.

    Args:
        claims: Ingestor name mapped to the fragments it produced, in
            registry order.

    Returns:
        The surviving fragments per ingestor, plus the contested paths.
    """
    ranks = _rank(claims)
    owner: dict[str, str] = {}
    contested: set[str] = set()
    contenders: set[str] = set()

    for name, fragments in claims.items():
        for fragment in fragments:
            incumbent = owner.get(fragment.source_path)
            if incumbent is None:
                owner[fragment.source_path] = name
            elif incumbent != name:
                contested.add(fragment.source_path)
                contenders.update((incumbent, name))
                if ranks[name] < ranks[incumbent]:
                    owner[fragment.source_path] = name

    _warn_unranked(contenders)
    winners = {
        name: [f for f in fragments if owner[f.source_path] == name]
        for name, fragments in claims.items()
    }
    return Arbitration(winners=winners, contested=sorted(contested))
