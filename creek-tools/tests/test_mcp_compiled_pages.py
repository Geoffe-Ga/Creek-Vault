"""``creek_mcp.compiled_pages`` — the compiled layer through a tier ceiling (#873).

``creek.reflect`` may now return the eddy and praxis pages nearest a reflected
entry. Neither page carries a ``privacy_tier`` of its own, and both are
*synthesised from fragments* — which makes them the exact laundering shape
#1013 / #1538 closed for drafts. The rule this suite exists to hold is:

    **Provenance authorizes; seeds only select.**

So the security assertions below are made at the **narrowest** ceiling that can
still fail: a page compiled from a ``personal`` fragment is asserted withheld at
``ceiling=open``, not merely at ``ceiling=intimate`` where nothing would have
been filtered either way and the assertion could pass against unfixed code. The
same battery re-runs one rank up (``personal`` vs an ``intimate`` contributor)
so neither rank boundary can regress alone.

The second half of the rule is the one that is easy to get backwards: a page
whose provenance cannot be *enumerated* is opaque, and an opaque page is
withheld. "No sources listed" is never read as "no sources".
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp.compiled_pages import (
    EXCERPT_CHARS,
    MAX_RELATED_EDDIES,
    MAX_RELATED_PRAXIS,
    RelatedCompiled,
    _body_of,
    _excerpt,
    _praxis_page,
    related_compiled,
)
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

_OPEN_FRAGMENT = "frag-open00000001"
_PERSONAL_FRAGMENT = "frag-personal001"
_INTIMATE_FRAGMENT = "frag-intimate001"
_UNTIERED_FRAGMENT = "frag-untiered001"

_EDDY_TITLE = "Rest and Ruin"
_PRAXIS_TITLE = "Rest before the collapse"


def _vault(tmp_path: Path) -> Path:
    """Create an empty vault root."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _fragment(
    vault: Path,
    frag_id: str,
    *,
    tier: str | None = "open",
    eddies: tuple[str, ...] = (),
    title: str = "A fragment",
) -> None:
    """Write a loadable fragment under ``01-Fragments``.

    Args:
        vault: Vault root.
        frag_id: The fragment ``id``.
        tier: The ``privacy_tier`` value; ``None`` omits the key entirely,
            which must fail closed to INTIMATE rather than inherit the
            ``Fragment`` model's ``unclassified`` default.
        eddies: ``eddies:`` wiki-link entries, verbatim.
        title: The fragment title.
    """
    directory = vault / "01-Fragments" / "Notes"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: fragment",
        f"id: {frag_id}",
        f"title: {title}",
        "source:",
        "  platform: journal",
    ]
    if tier is not None:
        lines.append(f"privacy_tier: {tier}")
    if eddies:
        lines.append("eddies:")
        lines.extend(f'  - "{link}"' for link in eddies)
    lines.extend(["---", "", "Body text."])
    (directory / f"{frag_id}.md").write_text("\n".join(lines) + "\n")


def _eddy(
    vault: Path,
    *,
    title: str = _EDDY_TITLE,
    fragment_count: int = 1,
    formed: str = "2026-03-04",
    description: str = "Where rest and ruin keep meeting.",
    filename: str | None = None,
    page_type: str = "eddy",
) -> None:
    """Write an eddy page under ``03-Eddies``."""
    directory = vault / "03-Eddies"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"type: {page_type}",
        "id: eddy-0000000001",
        f"title: {title}",
        f"formed: {formed}" if formed else "",
        f"fragment_count: {fragment_count}",
        f"description: {description}",
        "---",
        "",
        description,
    ]
    name = filename or f"{title}.md"
    (directory / name).write_text(
        "\n".join(line for line in lines if line != "") + "\n"
    )


def _praxis(
    vault: Path,
    *,
    title: str = _PRAXIS_TITLE,
    derived_from: tuple[str, ...] | None = (_OPEN_FRAGMENT,),
    praxis_type: str = "practice",
    status: str = "active",
    body: str = "Rest is the practice, not the reward.",
    filename: str | None = None,
) -> None:
    """Write a praxis page under ``04-Praxis``."""
    directory = vault / "04-Praxis" / "Daily"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: praxis",
        "id: prax-0000000001",
        f"title: {title}",
        f"praxis_type: {praxis_type}",
        f"status: {status}",
    ]
    if derived_from is not None:
        lines.append("derived_from:")
        lines.extend(f"  - {entry}" for entry in derived_from)
    lines.extend(["---", "", body])
    (directory / (filename or f"{title}.md")).write_text("\n".join(lines) + "\n")


def _titles(rows: list[dict[str, Any]]) -> list[str]:
    """Return the ``title`` of each returned row."""
    return [str(row["title"]) for row in rows]


# ---------------------------------------------------------------------------
# The happy path -- so every withholding assertion below is a real observation
# ---------------------------------------------------------------------------


def test_open_provenance_is_published_at_the_narrowest_ceiling(tmp_path: Path) -> None:
    """An all-``open`` eddy and praxis reach even an ``open``-ceiling caller.

    This is the control. Every withholding test that follows differs from this
    one by exactly one fragment's tier or one missing source, so a withheld
    result there cannot be explained by the fixture simply never qualifying.
    """
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}]]",))
    _eddy(vault, fragment_count=1)
    _praxis(vault, derived_from=(_OPEN_FRAGMENT,))

    related = related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.OPEN)

    assert _titles(related.eddies) == [_EDDY_TITLE]
    assert _titles(related.praxis) == [_PRAXIS_TITLE]
    assert related.eddies[0] == {
        "title": _EDDY_TITLE,
        "description": "Where rest and ruin keep meeting.",
        "fragment_count": 1,
        "formed": "2026-03-04",
    }
    assert related.praxis[0] == {
        "title": _PRAXIS_TITLE,
        "praxis_type": "practice",
        "status": "active",
        "excerpt": "Rest is the practice, not the reward.",
    }


# ---------------------------------------------------------------------------
# The ceiling, asserted at the narrowest rank that can fail
# ---------------------------------------------------------------------------

_ABOVE_CEILING_CASES: tuple[tuple[str, str, str], ...] = (
    ("open", "personal", "personal"),
    ("open", "intimate", "intimate"),
    ("personal", "intimate", "intimate"),
    ("open", None, "missing-key"),  # type: ignore[arg-type]
    ("personal", None, "missing-key"),  # type: ignore[arg-type]
)
"""``(ceiling, contributor tier, id)`` triples that must all withhold.

The first row is the load-bearing one: ``personal`` content withheld from an
``open``-ceiling caller is a rank boundary an unfixed implementation crosses,
whereas asserting only ``intimate`` at ``ceiling=intimate`` would pass without
anything ever being filtered. The two ``None`` rows are the missing-key
fail-closed case, which must rank INTIMATE rather than inherit ``Fragment``'s
``unclassified`` default.
"""


def test_above_ceiling_cases_are_not_empty() -> None:
    """The parametrised battery below is non-vacuous.

    Emptying :data:`_ABOVE_CEILING_CASES` would delete the entire ceiling
    battery behind a green gate; this makes that deletion red.
    """
    assert len(_ABOVE_CEILING_CASES) >= 5


@pytest.mark.parametrize(
    ("ceiling_value", "contributor_tier", "case_id"),
    _ABOVE_CEILING_CASES,
    ids=[case[2] + "@" + case[0] for case in _ABOVE_CEILING_CASES],
)
def test_eddy_with_an_above_ceiling_member_is_withheld(
    tmp_path: Path, ceiling_value: str, contributor_tier: str | None, case_id: str
) -> None:
    """One above-ceiling member is enough to withhold the whole eddy page.

    The eddy's ``description`` and ``fragment_count`` are compiled *from* its
    members, so returning the page would hand the caller a summary of content
    they are not admitted to — without ever returning a byte of that content
    directly, which is precisely what makes it easy to miss.
    """
    del case_id
    vault = _vault(tmp_path)
    link = f"[[{_EDDY_TITLE}]]"
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(link,))
    _fragment(vault, "frag-above000001", tier=contributor_tier, eddies=(link,))
    _eddy(vault, fragment_count=2)

    related = related_compiled([_OPEN_FRAGMENT], vault, TierCeiling(ceiling_value))

    assert related.eddies == []


@pytest.mark.parametrize(
    ("ceiling_value", "contributor_tier", "case_id"),
    _ABOVE_CEILING_CASES,
    ids=[case[2] + "@" + case[0] for case in _ABOVE_CEILING_CASES],
)
def test_praxis_with_an_above_ceiling_source_is_withheld(
    tmp_path: Path, ceiling_value: str, contributor_tier: str | None, case_id: str
) -> None:
    """One above-ceiling ``derived_from`` source withholds the praxis page."""
    del case_id
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open")
    _fragment(vault, "frag-above000001", tier=contributor_tier)
    _praxis(vault, derived_from=(_OPEN_FRAGMENT, "frag-above000001"))

    related = related_compiled([_OPEN_FRAGMENT], vault, TierCeiling(ceiling_value))

    assert related.praxis == []


def test_an_above_ceiling_seed_widens_nothing(tmp_path: Path) -> None:
    """Seeds select; they do not authorize.

    Handing the lookup an ``intimate`` fragment's id as a seed must not admit
    the pages that fragment contributed to. If seeds carried authority, the
    ``entry_ref`` path would become a way to launder the whole compiled layer
    by naming one id.
    """
    vault = _vault(tmp_path)
    link = f"[[{_EDDY_TITLE}]]"
    _fragment(vault, _INTIMATE_FRAGMENT, tier="intimate", eddies=(link,))
    _eddy(vault, fragment_count=1)
    _praxis(vault, derived_from=(_INTIMATE_FRAGMENT,))

    related = related_compiled([_INTIMATE_FRAGMENT], vault, TierCeiling.PERSONAL)

    assert related == RelatedCompiled([], [])


def test_intimate_provenance_is_published_only_at_an_intimate_ceiling(
    tmp_path: Path,
) -> None:
    """The gate is a rank comparison, not a blanket refusal of compiled pages.

    Without this, `_provenance_admitted` could be replaced by ``return False``
    and every withholding test above would still pass.
    """
    vault = _vault(tmp_path)
    link = f"[[{_EDDY_TITLE}]]"
    _fragment(vault, _INTIMATE_FRAGMENT, tier="intimate", eddies=(link,))
    _eddy(vault, fragment_count=1)
    _praxis(vault, derived_from=(_INTIMATE_FRAGMENT,))

    related = related_compiled([_INTIMATE_FRAGMENT], vault, TierCeiling.INTIMATE)

    assert _titles(related.eddies) == [_EDDY_TITLE]
    assert _titles(related.praxis) == [_PRAXIS_TITLE]


def test_a_duplicate_fragment_id_resolves_most_restrictive(tmp_path: Path) -> None:
    """A second file claiming the same id cannot launder the stricter copy."""
    vault = _vault(tmp_path)
    link = f"[[{_EDDY_TITLE}]]"
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(link,))
    directory = vault / "01-Fragments" / "Duplicates"
    directory.mkdir(parents=True)
    (directory / "dup.md").write_text(
        "---\ntype: fragment\n"
        f"id: {_OPEN_FRAGMENT}\ntitle: Same id\nsource:\n  platform: journal\n"
        "privacy_tier: intimate\n"
        f'eddies:\n  - "{link}"\n---\n\nBody.\n'
    )
    _eddy(vault, fragment_count=1)

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.OPEN).eddies == []


# ---------------------------------------------------------------------------
# Opacity -- a page whose provenance cannot be enumerated is withheld
# ---------------------------------------------------------------------------


def test_eddy_whose_members_cannot_all_be_enumerated_is_withheld(
    tmp_path: Path,
) -> None:
    """A ``fragment_count`` larger than the enumerable members means opaque.

    ``EddyDetector`` sets ``fragment_count`` to exactly the member list it then
    writes wiki-links for, so a shortfall means a contributor is missing from
    the walk — deleted, purged, unreadable, or never written. Its tier is
    therefore unknown, and unknown is not clean.
    """
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}]]",))
    _eddy(vault, fragment_count=7)

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).eddies == []


def test_praxis_naming_an_unresolvable_source_is_withheld(tmp_path: Path) -> None:
    """A ``derived_from`` id that resolves to no fragment is unenumerable."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open")
    _praxis(vault, derived_from=(_OPEN_FRAGMENT, "frag-vanished0001"))

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).praxis == []


@pytest.mark.parametrize("derived_from", [(), None], ids=["empty-list", "key-absent"])
def test_praxis_with_no_declared_sources_never_parses(
    tmp_path: Path, derived_from: tuple[str, ...] | None
) -> None:
    """An empty ``derived_from`` is *opaque*, never "compiled from nothing".

    This is the inversion the module docstring warns about: a page that lists
    no sources is the one page whose sources nobody can check.

    Asserted against :func:`_praxis_page` rather than through
    :func:`related_compiled`, and deliberately so. Selection today requires a
    seed to appear *in* ``derived_from``, so a sourceless page is never
    selected in the first place — a whole-lookup assertion here would pass for
    that reason alone and would keep passing with the guard deleted, which is
    theatre. Pointing it at the parser makes it a real observation about the
    rule, and keeps the rule standing if selection ever widens (by eddy, by
    tag, by frequency) to a path that does not read ``derived_from``.
    """
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open")
    _praxis(vault, derived_from=derived_from)
    page = next(iter((vault / "04-Praxis").rglob("*.md")))

    assert _praxis_page(page) is None


def test_praxis_with_a_nonstring_source_entry_is_withheld(tmp_path: Path) -> None:
    """A ``derived_from`` entry that is not an id leaves provenance partial."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open")
    directory = vault / "04-Praxis" / "Daily"
    directory.mkdir(parents=True)
    (directory / "odd.md").write_text(
        "---\ntype: praxis\nid: prax-0000000002\ntitle: Odd sources\n"
        "praxis_type: practice\nstatus: active\n"
        f"derived_from:\n  - {_OPEN_FRAGMENT}\n  - 12345\n---\n\nBody.\n"
    )

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).praxis == []


def test_an_ambiguous_eddy_title_is_withheld(tmp_path: Path) -> None:
    """Two pages answering to one title cannot be matched to a member set."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}]]",))
    _eddy(vault, fragment_count=1, filename="first.md")
    _eddy(vault, fragment_count=1, filename="second.md")

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).eddies == []


# ---------------------------------------------------------------------------
# Bounds, absence, and malformed input
# ---------------------------------------------------------------------------


def test_results_are_bounded(tmp_path: Path) -> None:
    """At most 3 praxis and 2 eddies, however many qualify."""
    vault = _vault(tmp_path)
    links = tuple(f"[[Eddy {index}]]" for index in range(5))
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=links)
    for index in range(5):
        _eddy(vault, title=f"Eddy {index}", fragment_count=1, filename=f"e{index}.md")
    for index in range(6):
        _praxis(
            vault,
            title=f"Praxis {index}",
            derived_from=(_OPEN_FRAGMENT,),
            filename=f"p{index}.md",
        )

    related = related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL)

    assert len(related.eddies) == MAX_RELATED_EDDIES
    assert len(related.praxis) == MAX_RELATED_PRAXIS
    # Deterministic, and independent of filesystem order: ranked by seed
    # overlap, ties broken by title.
    assert _titles(related.eddies) == ["Eddy 0", "Eddy 1"]
    assert _titles(related.praxis) == ["Praxis 0", "Praxis 1", "Praxis 2"]


def test_no_seeds_selects_nothing(tmp_path: Path) -> None:
    """No seeds means nothing selected -- never "select everything"."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}]]",))
    _eddy(vault, fragment_count=1)
    _praxis(vault, derived_from=(_OPEN_FRAGMENT,))

    assert related_compiled([], vault, TierCeiling.ALL) == RelatedCompiled([], [])
    assert related_compiled([""], vault, TierCeiling.ALL) == RelatedCompiled([], [])


def test_an_unrelated_seed_selects_nothing(tmp_path: Path) -> None:
    """A seed that contributes to neither page yields both fields empty."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}]]",))
    _fragment(vault, _PERSONAL_FRAGMENT, tier="personal")
    _eddy(vault, fragment_count=1)
    _praxis(vault, derived_from=(_OPEN_FRAGMENT,))

    related = related_compiled([_PERSONAL_FRAGMENT], vault, TierCeiling.ALL)

    assert related == RelatedCompiled([], [])


def test_an_empty_vault_yields_nothing_and_does_not_raise(tmp_path: Path) -> None:
    """A vault with no compiled folders at all is empty, not an error."""
    vault = _vault(tmp_path)

    assert related_compiled(
        [_OPEN_FRAGMENT], vault, TierCeiling.ALL
    ) == RelatedCompiled([], [])


@pytest.mark.parametrize(
    "page_body",
    [
        "not markdown at all",
        "---\ntype: eddy\ntitle: [unclosed\n",
        "---\ntype: eddy\ntitle: Rest and Ruin\nfragment_count: many\nformed: x\n---\n",
        "---\ntype: eddy\ntitle: Rest and Ruin\nfragment_count: 1\n---\n",
        "---\ntype: thread\ntitle: Rest and Ruin\n"
        "fragment_count: 1\nformed: 2026-01-01\n---\n",
    ],
    ids=["no-header", "unparseable", "bad-count", "no-formed", "wrong-type"],
)
def test_a_malformed_eddy_page_is_skipped_not_crashed(
    tmp_path: Path, page_body: str
) -> None:
    """A hand-edited page costs itself, never the call."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}]]",))
    (vault / "03-Eddies").mkdir()
    (vault / "03-Eddies" / "broken.md").write_text(page_body)

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).eddies == []


@pytest.mark.parametrize(
    ("praxis_type", "status"),
    [("aspiration", "active"), ("practice", "retired")],
    ids=["unknown-type", "unknown-status"],
)
def test_a_praxis_outside_the_vocabulary_is_withheld(
    tmp_path: Path, praxis_type: str, status: str
) -> None:
    """A hand-edited vocabulary never reaches the published contract."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open")
    _praxis(
        vault, derived_from=(_OPEN_FRAGMENT,), praxis_type=praxis_type, status=status
    )

    assert related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).praxis == []


def test_the_excerpt_is_bounded_and_carries_no_front_matter(tmp_path: Path) -> None:
    """The excerpt is the page's own prose, capped, header stripped."""
    vault = _vault(tmp_path)
    _fragment(vault, _OPEN_FRAGMENT, tier="open")
    _praxis(vault, derived_from=(_OPEN_FRAGMENT,), body="word " * 400)

    excerpt = str(
        related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL).praxis[0]["excerpt"]
    )

    assert len(excerpt) == EXCERPT_CHARS
    assert "derived_from" not in excerpt
    assert "type: praxis" not in excerpt


def test_a_wikilink_alias_resolves_to_the_page_name(tmp_path: Path) -> None:
    """``[[Title|shown]]`` links to ``Title``, not to the display text."""
    vault = _vault(tmp_path)
    _fragment(
        vault, _OPEN_FRAGMENT, tier="open", eddies=(f"[[{_EDDY_TITLE}|that eddy]]",)
    )
    _eddy(vault, fragment_count=1)

    related = related_compiled([_OPEN_FRAGMENT], vault, TierCeiling.ALL)

    assert _titles(related.eddies) == [_EDDY_TITLE]


# ---------------------------------------------------------------------------
# No new egress (ADR-0004)
# ---------------------------------------------------------------------------

_NETWORK_MODULES = frozenset(
    {
        "http",
        "httpx",
        "requests",
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "anthropic",
        "openai",
        "google",
        "ollama",
        "sentence_transformers",
    }
)


def test_the_compiled_layer_lookup_opens_no_network_client() -> None:
    """AST-pin the import list: this lookup is vault pages and nothing else.

    #873 requires the compiled-layer lookup to add **no new egress path**. A
    runtime assertion would only cover the paths a test happens to drive; the
    import list covers every path there is. Anything reaching a provider, a
    socket, or an embedding model would have to appear here first.
    """
    from creek_mcp import compiled_pages

    source = ast.parse(pathlib_read(compiled_pages.__file__))
    imported: set[str] = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & _NETWORK_MODULES, sorted(imported & _NETWORK_MODULES)


def pathlib_read(path: str) -> str:
    """Return the text of *path* (a tiny helper so the AST test reads cleanly)."""
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def test_untiered_fragments_are_not_silently_open(tmp_path: Path) -> None:
    """A fragment with no ``privacy_tier`` key is INTIMATE here, not ``open``.

    Named separately from the parametrised battery because it is the case a
    "just read ``fragment.privacy_tier``" refactor would silently break: the
    ``Fragment`` model defaults that field to ``unclassified``, which ranks
    with ``personal`` and is admitted from ``ceiling=personal``.
    """
    vault = _vault(tmp_path)
    _fragment(vault, _UNTIERED_FRAGMENT, tier=None)
    _praxis(vault, derived_from=(_UNTIERED_FRAGMENT,))

    assert (
        related_compiled([_UNTIERED_FRAGMENT], vault, TierCeiling.PERSONAL).praxis == []
    )
    assert (
        related_compiled([_UNTIERED_FRAGMENT], vault, TierCeiling.INTIMATE).praxis != []
    )


# ---------------------------------------------------------------------------
# The body reader, driven directly
# ---------------------------------------------------------------------------
#
# ``_excerpt`` is only ever *reached* through ``_praxis_page``, which has
# already parsed a well-formed header -- so these two branches are unreachable
# from ``related_compiled`` and are exercised here instead of being left as
# untested defensive code. Both are real: a Creek vault is a live Obsidian
# folder, so a page can vanish or be rewritten between the header read and the
# body read, which is the same verify-then-load race #1083 built its verifier
# for.


def test_an_excerpt_of_a_vanished_page_is_empty_not_an_exception(
    tmp_path: Path,
) -> None:
    """A page deleted between the header read and the body read costs itself."""
    assert _excerpt(tmp_path / "gone.md") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no header here", "no header here"),
        ("---\ntype: praxis\nbody never reached", ""),
        ("---\na: 1\n---\nThe body.", "The body."),
    ],
    ids=["no-fence", "unterminated", "well-formed"],
)
def test_the_body_reader_never_returns_front_matter(text: str, expected: str) -> None:
    """An unterminated header yields nothing rather than its own front matter.

    The ``unterminated`` row is the one that matters: returning the remainder
    would put raw front matter -- including a ``privacy_tier`` line -- into a
    published excerpt.
    """
    assert _body_of(text) == expected
