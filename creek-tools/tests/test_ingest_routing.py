"""Unit tests for the ingestor claim arbiter (issue #1304).

The e2e counterpart (``tests/e2e/test_full_pipeline_mixed_sources.py``)
asserts on what lands in the vault. These assert the arbiter's contract
directly, including the cases that are awkward to stage on disk: an
ingestor that produced nothing, an ingestor nobody has ranked, and
fragment ordering.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.base import ParsedFragment
from creek.ingest.routing import CLAIM_PRIORITY, arbitrate


def _fragment(source: str, marker: str) -> ParsedFragment:
    """Build a minimal fragment identifiable by *marker*.

    Args:
        source: Value for ``source_path``, the arbitration key.
        marker: Goes into ``content`` so assertions can name the exact
            fragment that survived.

    Returns:
        A fragment carrying just enough to be arbitrated.
    """
    return ParsedFragment(
        content=marker,
        metadata={},
        source_path=source,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _markers(fragments: list[ParsedFragment]) -> list[str]:
    """Return the ``content`` marker of each fragment, in order."""
    return [fragment.content for fragment in fragments]


def test_claim_priority_covers_the_whole_registry_exactly() -> None:
    """Every registered ingestor is ranked, and nothing else is.

    This is the forcing function: adding a twelfth ingestor without
    deciding where it sits fails here rather than silently landing
    behind ``generic`` at runtime.
    """
    assert set(CLAIM_PRIORITY) == set(INGESTOR_REGISTRY)
    assert len(CLAIM_PRIORITY) == len(set(CLAIM_PRIORITY))


def test_generic_is_ranked_last() -> None:
    """The declared fallback must never outrank a specialist."""
    assert CLAIM_PRIORITY[-1] == "generic"


def test_sniffing_ingestors_outrank_extension_specialists() -> None:
    """Structure- and content-sniffed claims beat extension-keyed ones.

    A Discord export's ``messages.json`` and a Substack post's ``.html``
    are recognised by their internals; whoever merely matched the
    extension has weaker evidence.
    """
    ranks = {name: index for index, name in enumerate(CLAIM_PRIORITY)}
    for sniffer in ("discord", "chatgpt", "claude", "substack"):
        for by_extension in ("markdown", "code", "document", "generic"):
            assert ranks[sniffer] < ranks[by_extension]


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        ("markdown", "code"),
        ("code", "generic"),
        ("substack", "document"),
        ("document", "generic"),
        ("spreadsheet", "generic"),
        ("presentation", "generic"),
        ("image", "generic"),
    ],
)
def test_contested_path_goes_to_the_higher_priority_producer(
    winner: str, loser: str
) -> None:
    """Each documented contest resolves the documented way.

    Order of the input mapping must not matter, so both orderings are
    exercised.
    """
    for claims in (
        {winner: [_fragment("/s/f", "win")], loser: [_fragment("/s/f", "lose")]},
        {loser: [_fragment("/s/f", "lose")], winner: [_fragment("/s/f", "win")]},
    ):
        outcome = arbitrate(claims)
        assert _markers(outcome.winners[winner]) == ["win"]
        assert outcome.winners[loser] == []
        assert outcome.contested == ["/s/f"]


def test_a_producer_of_nothing_never_wins_a_path() -> None:
    """An ingestor that emitted no fragments for a path cannot claim it.

    This is the whole reason arbitration runs after parsing: a
    higher-priority ingestor whose ``parse`` failed forfeits, and the
    content still reaches the vault via whoever succeeded.
    """
    outcome = arbitrate(
        {"spreadsheet": [], "generic": [_fragment("/s/broken.csv", "fallback")]}
    )

    assert _markers(outcome.winners["generic"]) == ["fallback"]
    assert outcome.contested == []


def test_a_multi_fragment_winner_keeps_every_fragment_for_that_path() -> None:
    """One *ingestor* per file, never one *fragment* per file.

    ``SpreadsheetIngestor`` emits one fragment per non-empty sheet (see
    issue #1305) and ``CodeIngestor`` one per module and function.
    Collapsing those to a single fragment would be a different bug.
    """
    outcome = arbitrate(
        {
            "spreadsheet": [
                _fragment("/s/book.xlsx", "sheet-1"),
                _fragment("/s/book.xlsx", "sheet-2"),
                _fragment("/s/book.xlsx", "sheet-3"),
            ],
            "generic": [_fragment("/s/book.xlsx", "whole-file")],
        }
    )

    assert _markers(outcome.winners["spreadsheet"]) == [
        "sheet-1",
        "sheet-2",
        "sheet-3",
    ]
    assert outcome.winners["generic"] == []


def test_uncontested_paths_are_untouched_and_not_reported() -> None:
    """Files only one ingestor claimed pass straight through."""
    outcome = arbitrate(
        {
            "markdown": [_fragment("/s/a.md", "md")],
            "generic": [_fragment("/s/b.txt", "txt")],
        }
    )

    assert _markers(outcome.winners["markdown"]) == ["md"]
    assert _markers(outcome.winners["generic"]) == ["txt"]
    assert outcome.contested == []


def test_fragment_order_within_an_ingestor_is_preserved() -> None:
    """Surviving fragments keep the order their ingestor produced them in.

    Fragment order drives vault write order and, on a name collision,
    which file gets the ``-N`` suffix.
    """
    outcome = arbitrate(
        {
            "code": [
                _fragment("/s/a.py", "module-a"),
                _fragment("/s/b.py", "module-b"),
                _fragment("/s/a.py", "function-a"),
            ],
            "generic": [_fragment("/s/a.py", "dump-a")],
        }
    )

    assert _markers(outcome.winners["code"]) == [
        "module-a",
        "module-b",
        "function-a",
    ]


def test_an_unranked_ingestor_wins_paths_nobody_else_claims_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registry of names the arbiter has never heard of still works.

    ``tests/test_pipeline.py`` patches ``INGESTOR_REGISTRY`` with
    synthetic keys such as ``{"mock": ...}``. Those must ingest normally
    and, since nothing contests them, must not log a warning.
    """
    with caplog.at_level(logging.WARNING, logger="creek.ingest.routing"):
        outcome = arbitrate({"mock": [_fragment("/s/broken.md", "mocked")]})

    assert _markers(outcome.winners["mock"]) == ["mocked"]
    assert outcome.contested == []
    assert caplog.records == []


def test_an_unranked_ingestor_loses_a_contest_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unranked name ranks behind every known one, and warns when it matters.

    Silently demoting an ingestor nobody ranked is how this defect class
    comes back, so the contest is announced.
    """
    with caplog.at_level(logging.WARNING, logger="creek.ingest.routing"):
        outcome = arbitrate(
            {
                "generic": [_fragment("/s/f.txt", "known")],
                "bespoke": [_fragment("/s/f.txt", "unranked")],
            }
        )

    assert _markers(outcome.winners["generic"]) == ["known"]
    assert outcome.winners["bespoke"] == []
    assert "bespoke" in caplog.text
    assert "CLAIM_PRIORITY" in caplog.text


def test_two_unranked_ingestors_resolve_deterministically_by_name() -> None:
    """Ties among unranked names break on the name, not on dict order."""
    fragments = {
        "zeta": [_fragment("/s/f", "zeta")],
        "alpha": [_fragment("/s/f", "alpha")],
    }

    assert _markers(arbitrate(fragments).winners["alpha"]) == ["alpha"]
    assert _markers(arbitrate(dict(reversed(fragments.items()))).winners["alpha"]) == [
        "alpha"
    ]


def test_empty_claims_arbitrate_to_nothing() -> None:
    """An empty registry produces no winners and no contests."""
    outcome = arbitrate({})

    assert outcome.winners == {}
    assert outcome.contested == []


def test_contested_paths_are_reported_sorted_and_deduplicated() -> None:
    """Each contested path appears once, in sorted order.

    Enough paths, in a deliberately reversed input order, that the
    assertion cannot pass by accident: the arbiter accumulates contests
    in a set, whose iteration order is neither sorted nor stable across
    interpreter runs.
    """
    names = [f"/s/{letter}.txt" for letter in "jihgfedcba"]
    outcome = arbitrate(
        {
            "document": [_fragment(path, f"doc-{path}") for path in names],
            "generic": [
                *[_fragment(path, f"gen-{path}") for path in names],
                _fragment("/s/uncontested.txt", "gen-solo"),
            ],
        }
    )

    assert outcome.contested == sorted(names)
    assert _markers(outcome.winners["generic"]) == ["gen-solo"]
    assert len(outcome.winners["document"]) == len(names)
