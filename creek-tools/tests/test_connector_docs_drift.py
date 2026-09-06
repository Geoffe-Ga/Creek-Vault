"""The connector contract's prose may not drift from ``route_to_ingestor`` (#1544).

``creek.ingest.gdrive.route_to_ingestor`` stopped being a total function in
#1526: for a conversation export, an archive or a legacy binary Office
document it raises :class:`creek.ingest.UnsupportedSourceError` instead of
returning the ``generic`` fallback. Three places that *teach* the connector
contract were never updated and still described the old, total contract --
``docs/connectors/adding-a-remote-connector.md`` (twice, once in prose and
once inside the ``python`` fence a connector author copies) and the
:mod:`creek.ingest.connectors` Protocol itself (its module docstring and
:meth:`RemoteSourceConnector.fetch_to`). A connector author who believed them
would write a caller with no ``except``, and every refused file would abort
the fetch instead of being skipped or explained.

A fourth site was drifted in the opposite direction: ``docs/mcp.md`` told
readers the ``creek.upload`` tool turns a ``.zip`` away, when the archive fork
of #1525 unpacks it *above* the #1526 gate.

This module is the drift gate. It joins four siblings --
``test_taxonomy_docs_drift.py``, ``test_redaction_docs_drift.py``,
``test_ontology_vocabulary_docs_drift.py`` and, closest of all,
``test_seeding_docs_capability_set.py``, whose ``capability-set`` HTML-comment
fence idiom and *bidirectional* set equality are reused here. A missing row
and an invented row must both fail; a containment check would let the guide
invent a refused family and stay green.

Two properties keep the assertions from going vacuous:

* **Every expectation is a runtime value, never a copied literal.** The
  exception is named by ``UnsupportedSourceError.__name__`` and the refused
  set is read from the live :data:`~creek.ingest.gdrive._EXTENSION_ROUTES`, so
  renaming the exception or editing the routing table fails this gate rather
  than silently staling four documents. Do **not** "simplify" these into
  hard-coded strings -- that is precisely the drift being guarded.
* **The docstring sites are read through ``__doc__``, not by grepping the
  source.** A token wrapped across an implicit string-concatenation break is
  invisible to a source grep but intact in the runtime docstring, so a
  text-scanning guard here would report zero hits against the very defect it
  exists to catch. :func:`inspect.cleandoc` normalises the indentation CPython
  3.13 strips at compile time but 3.12 does not, and
  :func:`_normalised` collapses whitespace so a pure reflow of a paragraph
  cannot redden a gate that is about meaning.

``_EXTENSION_ROUTES`` and ``_Refuse`` are private; importing them from a test
is the established in-tree precedent (``test_gdrive_downloader.py``,
``test_v1_api_upload.py``, ``test_seeding_docs_capability_set.py``) and is
what makes this gate track the code instead of a copy of it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from creek.ingest import UnsupportedSourceError
from creek.ingest import connectors as connectors_module
from creek.ingest.archive import ARCHIVE_SUFFIXES
from creek.ingest.connectors import RemoteSourceConnector
from creek.ingest.gdrive import _EXTENSION_ROUTES, _Refuse

DOCS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "docs"
GUIDE: Final[Path] = DOCS_DIR / "connectors" / "adding-a-remote-connector.md"
MCP_DOC: Final[Path] = DOCS_DIR / "mcp.md"

GENERIC_FALLBACK: Final[str] = "generic"
REFUSALS_FENCE: Final[str] = "connector-refusals"

_FENCE_TEMPLATE: Final[str] = (
    r"<!-- capability-set: {name} -->(?P<body>.*?)<!-- /capability-set -->"
)
_CODE_SPAN: Final[re.Pattern[str]] = re.compile(r"`([^`]+)`")

_GATE_ANCHOR: Final[str] = "The unsupported-format gate"
_GATE_END: Final[str] = "instead of routing to"

_PYTHON_FENCE: Final[re.Pattern[str]] = re.compile(
    r"^```python$(?P<body>.*?)^```$", re.DOTALL | re.MULTILINE
)

# The guide is checked twice on purpose. The whole-file site catches the
# narrative prose; ``guide-python-fence`` isolates the skeleton a connector
# author literally copies, because a token present anywhere else in the file
# would otherwise satisfy the whole-file assertion and leave the skeleton --
# the half of the guide that becomes real code -- unguarded.
CONTRACT_SITES: Final[tuple[str, ...]] = (
    "guide",
    "guide-python-fence",
    "connectors-module",
    "fetch_to",
)


def _normalised(text: str) -> str:
    """Collapse every run of whitespace in *text* to a single space.

    Keeps the prose assertions about meaning rather than layout: rewrapping a
    paragraph must not redden a gate that only cares whether a concept is
    still named.

    Args:
        text: The raw prose to normalise.

    Returns:
        *text* with leading/trailing whitespace stripped and internal runs of
        whitespace collapsed to one space each.
    """
    return " ".join(text.split())


def _guide_text() -> str:
    """Return the whole connector guide, whitespace-normalised.

    Returns:
        The guide's full text.

    Raises:
        AssertionError: If the guide is missing. Asserted rather than left to
            raise at collection time, where it would print as an error and
            read as a fast pass.
    """
    assert GUIDE.is_file(), f"{GUIDE} is missing"
    return _normalised(GUIDE.read_text(encoding="utf-8"))


def _guide_python_fence() -> str:
    """Return the connector skeleton inside the guide's ``python`` fence.

    Returns:
        The fence body, whitespace-normalised.

    Raises:
        AssertionError: If the guide is missing or holds no ``python`` fence.
    """
    assert GUIDE.is_file(), f"{GUIDE} is missing"
    match = _PYTHON_FENCE.search(GUIDE.read_text(encoding="utf-8"))
    assert match is not None, (
        f"{GUIDE.name} has no ```python fence; the connector skeleton this "
        "assertion reads was renamed or deleted"
    )
    return _normalised(match.group("body"))


def _connectors_module_docstring() -> str:
    """Return :mod:`creek.ingest.connectors`'s module docstring.

    Returns:
        The runtime ``__doc__``, de-indented and whitespace-normalised.

    Raises:
        AssertionError: If the module lost its docstring.
    """
    raw = connectors_module.__doc__
    assert raw, "creek.ingest.connectors lost its module docstring"
    return _normalised(inspect.cleandoc(raw))


def _fetch_to_docstring() -> str:
    """Return :meth:`RemoteSourceConnector.fetch_to`'s docstring.

    Returns:
        The runtime ``__doc__``, de-indented and whitespace-normalised.

    Raises:
        AssertionError: If the method lost its docstring.
    """
    raw = RemoteSourceConnector.fetch_to.__doc__
    assert raw, "RemoteSourceConnector.fetch_to lost its docstring"
    return _normalised(inspect.cleandoc(raw))


# Read through ``__doc__`` -- the runtime value -- not by grepping the source,
# so a token wrapped across an implicit string-concatenation break is still
# seen whole.
_SITE_READERS: Final[dict[str, Callable[[], str]]] = {
    "guide": _guide_text,
    "guide-python-fence": _guide_python_fence,
    "connectors-module": _connectors_module_docstring,
    "fetch_to": _fetch_to_docstring,
}


def _contract_prose(site: str) -> str:
    """Return the normalised prose of one statement of the connector contract.

    Args:
        site: One of :data:`CONTRACT_SITES`.

    Returns:
        The site's prose, whitespace-normalised.

    Raises:
        AssertionError: If *site* is not a known contract site.
    """
    reader = _SITE_READERS.get(site)
    assert reader is not None, f"unknown contract site: {site}"
    return reader()


def _live_refused_suffixes() -> set[str]:
    """Return every extension ``route_to_ingestor`` refuses, read live.

    Returns:
        The suffixes whose ``_EXTENSION_ROUTES`` entry is a ``_Refuse``.
    """
    return {
        suffix
        for suffix, route in _EXTENSION_ROUTES.items()
        if isinstance(route, _Refuse)
    }


def _fenced_table_rows(doc: Path, name: str) -> list[list[str]]:
    """Return the body rows of the ``capability-set`` table fenced as *name*.

    Args:
        doc: The Markdown file holding the fence.
        name: The fence label.

    Returns:
        One list of raw cell strings per body row, with the header and the
        separator row dropped.

    Raises:
        AssertionError: If *doc* has no fence with that label, or the table
            has no body rows.
    """
    text = doc.read_text(encoding="utf-8")
    match = re.search(_FENCE_TEMPLATE.format(name=re.escape(name)), text, re.DOTALL)
    assert match is not None, (
        f"{doc.name} has no <!-- capability-set: {name} --> fence; the "
        "machine-checked refusal table was renamed or deleted"
    )
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        rows.append(cells)
    assert len(rows) > 1, f"the {name} table has no body rows"
    return rows[1:]


def _documented_refused_suffixes() -> set[str]:
    """Return the extensions the guide's fenced table lists as refused.

    Returns:
        Every code span found in the ``Extensions`` column of the
        ``connector-refusals`` table.
    """
    documented: set[str] = set()
    for row in _fenced_table_rows(GUIDE, REFUSALS_FENCE):
        documented.update(_CODE_SPAN.findall(row[1]))
    return documented


@pytest.mark.parametrize("site", CONTRACT_SITES)
def test_contract_prose_names_the_refusal(site: str) -> None:
    """Every statement of the connector contract names the exception.

    The name is read from the class, so renaming it fails here instead of
    quietly staling three documents (#1544).

    Args:
        site: The contract site under test.
    """
    prose = _contract_prose(site)
    assert UnsupportedSourceError.__name__ in prose, (
        f"the {site} statement of the connector contract does not name "
        f"{UnsupportedSourceError.__name__}, so it still describes "
        "route_to_ingestor as a total function (#1526/#1544)"
    )


@pytest.mark.parametrize("site", CONTRACT_SITES)
def test_contract_prose_keeps_the_generic_carve_out(site: str) -> None:
    """Every site still records that an unnamed extension routes to ``generic``.

    #1526 narrowed the fallback; it did not retire it. A reader who loses this
    sentence concludes every unrecognised extension is refused.

    Args:
        site: The contract site under test.
    """
    prose = _contract_prose(site)
    assert GENERIC_FALLBACK in prose, (
        f"the {site} statement of the connector contract lost the "
        f"{GENERIC_FALLBACK!r} fallback carve-out; an extension the routing "
        "table does not name is still routed, not refused"
    )


def test_the_guide_lists_exactly_the_refused_extensions() -> None:
    """The guide's fenced table equals the live refusal set, both directions.

    Equality, not containment: containment would let the guide invent a
    refused family (a ``.parquet`` row, or a *routed* extension listed as
    refused) and stay green.
    """
    actual = _live_refused_suffixes()
    assert actual, (
        "_EXTENSION_ROUTES refuses nothing; this gate would be vacuous, so "
        "the routing table or _Refuse changed shape"
    )
    assert _documented_refused_suffixes() == actual, (
        "docs/connectors/adding-a-remote-connector.md's connector-refusals "
        "table drifted from creek.ingest.gdrive._EXTENSION_ROUTES; document "
        "the new refusal or drop the invented row"
    )


def test_the_mcp_upload_gate_does_not_claim_archives_are_refused() -> None:
    """``docs/mcp.md`` must not list an unpacked archive as turned away.

    The archive fork of #1525 runs *above* the #1526 gate on the
    ``creek.upload`` surface, so every member of ``ARCHIVE_SUFFIXES`` is
    expanded there rather than refused.
    """
    text = _normalised(MCP_DOC.read_text(encoding="utf-8"))
    assert _GATE_ANCHOR in text, (
        f"docs/mcp.md no longer contains {_GATE_ANCHOR!r}; the anchor this "
        "assertion reads was reworded, so it can no longer find the clause"
    )
    start = text.index(_GATE_ANCHOR)
    end = text.find(_GATE_END, start)
    assert end != -1, (
        f"docs/mcp.md no longer contains {_GATE_END!r} after the gate "
        "clause; the extraction window is gone"
    )
    clause = text[start:end]
    named = sorted(suffix for suffix in ARCHIVE_SUFFIXES if f"`{suffix}`" in clause)
    assert not named, (
        f"docs/mcp.md's creek.upload unsupported-format-gate clause lists "
        f"{named} as turned away, but the #1525 archive fork unpacks them "
        "above that gate"
    )
