"""``docs/seeding.md`` may not drift from the code's real capability set (#1528).

The seeding guide tells a non-engineer which sources they can seed from,
which file extensions the network upload surface accepts, and which
``/v1`` routes exist. Every one of those lists is derived from a live
object in the tree, so a capability added or removed in code silently
falsifies the doc unless something compares them.

This module is that comparison. It enumerates from the code --
:data:`creek.ingest.INGESTOR_REGISTRY`,
:class:`creek.models.SourcePlatform`,
:data:`creek.ingest.gdrive._EXTENSION_ROUTES`,
:data:`creek.ingest.archive.ARCHIVE_SUFFIXES` and
:data:`creek_mcp.api.routes.ROUTES` -- and asserts the doc's tables match
**exactly**, in both directions. A missing row and an invented row both
fail.

``SourcePlatform`` is enumerated separately from ``INGESTOR_REGISTRY``
because the two lists are not the same and the difference is where the
doc rotted first. An earlier draft of the page claimed no
``creek ingest --type`` produced ``journal`` or ``essay``; in fact
``--type markdown`` stamps both, chosen from the *folder name* --
``daily/``, ``journal/``, ``diary/`` for one and ``essay*``/``writing/``
for the other. A registry-only check cannot see that, because neither
platform is a registry key. So
:func:`test_markdown_stamps_exactly_the_platforms_the_doc_credits_it_with`
runs the real ingestor over a real tree and compares the platforms it
stamps against the doc, and
:func:`test_documented_platforms_cover_every_source_platform` closes the
enum so a new member cannot be added without documenting what emits it.

Reading a platform out of a source literal is not the same as watching an
ingestor stamp it, and the difference is invisible in a passing test that
only greps. So
:func:`test_ingestor_stamps_exactly_the_platforms_the_doc_credits_it_with`
is parametrized over **every** key of ``INGESTOR_REGISTRY`` and drives
each one over a purpose-built fixture tree, comparing the platforms it
really writes against the producer column of the doc's table. Nine
ingestors run on their default backend. Two cannot, and are driven
through their own documented injection points instead: ``image`` takes a
stub :class:`~creek.ingest.images.OcrEngine` because the real one shells
out to a ``tesseract`` system binary, and ``presentation`` takes a stub
:class:`~creek.ingest.presentations.PresentationBackend` because there is
no library-free way to author a ``.pptx`` fixture. In both cases the code
under test -- ``generate_frontmatter``, which holds the platform literal
-- is the ingestor's own.

:data:`_FIXTURE_BUILDERS` is keyed by registry name and
:func:`test_every_registered_ingestor_has_an_execution_fixture` asserts
the two sets are equal, so a new ingestor cannot be added and left
un-executed: the parametrization would fail on the missing builder rather
than quietly skipping it.

The doc marks each machine-checked table with an HTML comment fence::

    <!-- capability-set: ingest-types -->
    | `--type` | Seeds |
    ...
    <!-- /capability-set -->

HTML comments render as nothing, so the fences are invisible to a reader
and unambiguous to the parser. Prose outside a fence is not checked --
this gate pins the enumerable claims, not the sentences.

One subtlety the equality has to encode: ``.zip`` is present in
``_EXTENSION_ROUTES`` as a *refusal*, yet a ``.zip`` upload succeeds,
because the archive fork in ``creek_mcp/tools/upload.py`` runs before
extension routing and never reaches the refusal (#1525). The expectation
built here therefore reads ``ARCHIVE_SUFFIXES`` first, so the doc's
``archive`` row is correct and the shadowing stays visible rather than
being quietly asserted away.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek.ingest import INGESTOR_REGISTRY, MarkdownIngestor
from creek.ingest.archive import ARCHIVE_SUFFIXES
from creek.ingest.gdrive import _EXTENSION_ROUTES
from creek.ingest.images import ImageIngestor, OcrResult
from creek.ingest.presentations import (
    PresentationData,
    PresentationIngestor,
    SlideData,
)
from creek.models import SourcePlatform
from creek_mcp.api.routes import ROUTES

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.ingest.base import Ingestor

SEEDING_DOC: Final[Path] = Path(__file__).resolve().parents[1] / "docs" / "seeding.md"

_FENCE_TEMPLATE: Final[str] = (
    r"<!-- capability-set: {name} -->(?P<body>.*?)<!-- /capability-set -->"
)
_CODE_SPAN: Final[re.Pattern[str]] = re.compile(r"`([^`]+)`")

# The line ``creek ingest`` prints for an unknown ``--type``, quoted in the
# doc's console block. Wrapped across two lines there, so the parser joins
# from the marker to the end of the fenced block.
_KNOWN_TYPES_MARKER: Final[str] = "Known types: "

_INGEST_PACKAGE: Final[Path] = Path(__file__).resolve().parents[1] / "creek" / "ingest"


def _fenced_table_rows(name: str) -> list[list[str]]:
    """Return the body rows of the capability-set table fenced as ``name``.

    Args:
        name: The fence label, e.g. ``"ingest-types"``.

    Returns:
        One list of raw cell strings per body row, header and separator
        rows dropped.

    Raises:
        AssertionError: If the doc has no fence with that label.
    """
    text = SEEDING_DOC.read_text(encoding="utf-8")
    match = re.search(_FENCE_TEMPLATE.format(name=re.escape(name)), text, re.DOTALL)
    assert match is not None, (
        f"docs/seeding.md has no <!-- capability-set: {name} --> fence; "
        "the machine-checked table was renamed or deleted"
    )
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue  # separator row
        rows.append(cells)
    assert len(rows) > 1, f"the {name} table has no body rows"
    return rows[1:]  # drop the header


def _documented_ingest_types() -> set[str]:
    """Return the ``--type`` values ``docs/seeding.md`` documents.

    Returns:
        The set of type names in the ``ingest-types`` table.
    """
    return {_CODE_SPAN.findall(row[0])[0] for row in _fenced_table_rows("ingest-types")}


def _documented_upload_routes() -> dict[str, str]:
    """Return the doc's extension-to-outcome map for uploads.

    Returns:
        A mapping of file extension (``".md"``) to the documented
        outcome -- an ingestor name, ``"archive"``, or ``"refused"``.
    """
    documented: dict[str, str] = {}
    for extensions, outcome in (
        (row[0], row[1]) for row in _fenced_table_rows("upload-extensions")
    ):
        target = _CODE_SPAN.findall(outcome)[0]
        for extension in _CODE_SPAN.findall(extensions):
            documented[extension] = target
    return documented


def _actual_upload_routes() -> dict[str, str]:
    """Return the real extension-to-outcome map, archive fork included.

    Returns:
        A mapping of file extension to ``"archive"`` when the archive
        fork claims it, the ingestor name when routing succeeds, and
        ``"refused"`` otherwise.
    """
    actual: dict[str, str] = {}
    for extension, route in _EXTENSION_ROUTES.items():
        if extension in ARCHIVE_SUFFIXES:
            actual[extension] = "archive"
        elif isinstance(route, str):
            actual[extension] = route
        else:
            actual[extension] = "refused"
    return actual


def test_seeding_doc_exists() -> None:
    """The guide the other assertions read is actually present."""
    assert SEEDING_DOC.is_file(), f"{SEEDING_DOC} is missing"


def test_documented_ingest_types_match_the_registry() -> None:
    """The doc's ``--type`` table equals ``INGESTOR_REGISTRY``."""
    assert _documented_ingest_types() == set(INGESTOR_REGISTRY), (
        "docs/seeding.md's --type table drifted from creek.ingest."
        "INGESTOR_REGISTRY; document the new ingestor or drop the retired row"
    )


def test_documented_upload_extensions_match_the_routing_table() -> None:
    """The doc's upload table equals the real routing outcomes."""
    assert _documented_upload_routes() == _actual_upload_routes(), (
        "docs/seeding.md's upload-extension table drifted from "
        "creek.ingest.gdrive._EXTENSION_ROUTES (archive fork applied)"
    )


def test_zip_is_documented_as_accepted_despite_its_refusal_entry() -> None:
    """``.zip`` routes to a refusal yet uploads, so the doc says ``archive``.

    Pins the #1525 shadowing itself: if the archive fork were removed,
    ``.zip`` would become a genuine refusal and this expectation --
    along with the doc's "send the platform's .zip" instruction -- would
    have to change.
    """
    assert ".zip" in ARCHIVE_SUFFIXES
    assert not isinstance(_EXTENSION_ROUTES[".zip"], str), (
        "_EXTENSION_ROUTES['.zip'] is no longer a refusal; the doc's note "
        "that the archive fork shadows it is now wrong"
    )
    assert _documented_upload_routes()[".zip"] == "archive"


def test_documented_v1_routes_match_the_route_table() -> None:
    """The doc's ``/v1`` table equals ``creek_mcp.api.routes.ROUTES``."""
    documented = {
        (_CODE_SPAN.findall(row[0])[0], _CODE_SPAN.findall(row[1])[0])
        for row in _fenced_table_rows("v1-routes")
    }
    actual = {(spec.method, spec.path) for spec in ROUTES}
    assert documented == actual, (
        "docs/seeding.md's /v1 route table drifted from ROUTES; a seeding "
        "route added or removed over the network must be documented"
    )


def _documented_platform_producers() -> dict[str, set[str]]:
    """Return the doc's ``source.platform`` to ``--type`` producer map.

    Returns:
        A mapping of platform value to the set of ingestor names the doc
        credits with stamping it. A platform the doc says nothing
        produces maps to the empty set.
    """
    return {
        _CODE_SPAN.findall(row[0])[0]: set(_CODE_SPAN.findall(row[1]))
        for row in _fenced_table_rows("source-platforms")
    }


def test_documented_platforms_cover_every_source_platform() -> None:
    """The platform table equals ``SourcePlatform``, both directions.

    This is the assertion that makes a *new* enum member fail the
    build. Checking only ``INGESTOR_REGISTRY`` cannot: a platform can
    exist, be stamped on real fragments, and never appear as a
    ``--type`` -- which is exactly how the earlier draft of this page
    came to claim that nothing produced ``journal``.
    """
    assert set(_documented_platform_producers()) == {
        platform.value for platform in SourcePlatform
    }, (
        "docs/seeding.md's source-platform table drifted from "
        "creek.models.SourcePlatform; document the new platform (and what "
        "stamps it) or drop the retired row"
    )


def test_documented_platform_producers_are_real_ingestors() -> None:
    """Every ``--type`` named as a producer is in the registry."""
    named = {
        ingestor
        for producers in _documented_platform_producers().values()
        for ingestor in producers
    }
    unknown = named - set(INGESTOR_REGISTRY)
    assert not unknown, (
        f"docs/seeding.md credits {sorted(unknown)} with stamping a platform, "
        "but no such ingestor is registered"
    )


def test_markdown_stamps_exactly_the_platforms_the_doc_credits_it_with(
    tmp_path: Path,
) -> None:
    """Run the markdown ingestor and compare its platforms to the doc.

    The folder-derived ``journal`` / ``essay`` / ``code`` stamping is
    invisible from ``INGESTOR_REGISTRY`` -- it lives in path patterns
    and a body heuristic -- so this executes the ingestor rather than
    reading a table. It is the check that catches a rename of
    ``_JOURNAL_PATH_PATTERNS`` or a change of default platform, neither
    of which touches any name the other tests inspect.

    Args:
        tmp_path: Pytest's per-test temporary directory.
    """
    prose = "# Heading\n\nOrdinary prose about water and sediment settling.\n"
    technical = (
        "# API\n\n```python\ndef f() -> int:\n    return 1\n```\n\n"
        "Run `pip install creek` and read the function signature in the "
        "API reference for the parameter list.\n"
    )
    for folder, body in (
        ("journal", prose),
        ("essays", prose),
        ("notes", prose),
        ("reference", technical),
    ):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "note.md").write_text(body, encoding="utf-8")

    ingestor = MarkdownIngestor()
    observed = {
        str(ingestor.generate_frontmatter(fragment)["source"]["platform"])
        for raw in ingestor.discover(tmp_path)
        for fragment in ingestor.parse(raw)
    }

    documented = {
        platform
        for platform, producers in _documented_platform_producers().items()
        if "markdown" in producers
    }
    assert observed == documented, (
        "the markdown ingestor stamps "
        f"{sorted(observed)}, but docs/seeding.md credits `markdown` with "
        f"{sorted(documented)}"
    )


# --------------------------------------------------------------------------- #
# Executing every ingestor, rather than reading its platform literal
# --------------------------------------------------------------------------- #

_PROSE: Final[str] = "The creek runs low in August. I keep coming back to silt.\n"
"""Body text for every fixture that just needs some prose."""

_TECHNICAL: Final[str] = (
    "# API\n\n```python\ndef f() -> int:\n    return 1\n```\n\n"
    "Run `pip install creek` and read the function signature in the "
    "API reference for the parameter list.\n"
)
"""A markdown body the ingestor's heuristic reads as technical prose."""

_CLAUDE_STAMP: Final[str] = "2026-08-01T10:30:00Z"
"""ISO timestamp used throughout the Claude export fixture."""

_CHATGPT_EPOCH: Final[float] = 1754042400.0
"""Unix epoch used throughout the ChatGPT export fixture."""


def _build_markdown_tree(root: Path) -> None:
    """Write notes whose *folders* select journal / essay / code / markdown.

    Args:
        root: Directory to populate.
    """
    for folder, body in (
        ("journal", _PROSE),
        ("essays", _PROSE),
        ("notes", _PROSE),
        ("reference", _TECHNICAL),
    ):
        (root / folder).mkdir()
        (root / folder / "note.md").write_text(body, encoding="utf-8")


def _build_document_tree(root: Path) -> None:
    """Write both halves of the document split: ``.rtf`` and ``.txt``/``.html``.

    ``.rtf`` is chosen for the ``document`` half because RTF is plain
    text, so the fixture needs no optional document library to author --
    and it takes the identical ``_DOCUMENT_PLATFORM_EXTENSIONS`` branch
    that ``.docx`` and ``.pdf`` take.

    Args:
        root: Directory to populate.
    """
    (root / "memo.rtf").write_text(
        r"{\rtf1\ansi\deff0 " + _PROSE + "}",
        encoding="utf-8",
    )
    (root / "page.html").write_text(
        f"<html><body><p>{_PROSE}</p></body></html>",
        encoding="utf-8",
    )
    (root / "plain.txt").write_text(_PROSE, encoding="utf-8")


def _build_generic_tree(root: Path) -> None:
    """Write a file with an extension no other ingestor claims.

    Args:
        root: Directory to populate.
    """
    (root / "notes.log").write_text(_PROSE, encoding="utf-8")


def _build_code_tree(root: Path) -> None:
    """Write a documented Python module for the code ingestor to decompose.

    Args:
        root: Directory to populate.
    """
    (root / "braid.py").write_text(
        '"""A module."""\n\n\ndef braid(x: int) -> int:\n'
        '    """Braid a number."""\n    return x + 1\n',
        encoding="utf-8",
    )


def _build_image_tree(root: Path) -> None:
    """Write a file with a PNG magic number for the image ingestor.

    Only the header is needed: the stub :class:`_StubOcrEngine` never
    decodes the pixels, and EXIF extraction falls through to ``None``.

    Args:
        root: Directory to populate.
    """
    (root / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def _build_presentation_tree(root: Path) -> None:
    """Write a ``.pptx``-named file for :class:`_StubPresentationBackend`.

    Args:
        root: Directory to populate.
    """
    (root / "talk.pptx").write_bytes(b"PK\x03\x04")


def _build_spreadsheet_tree(root: Path) -> None:
    """Write a ``.csv``, which the real backend reads with no optional library.

    Args:
        root: Directory to populate.
    """
    (root / "data.csv").write_text("column_a,column_b\n1,2\n", encoding="utf-8")


def _build_claude_tree(root: Path) -> None:
    """Write a Claude export: an object with a ``conversations`` key.

    Args:
        root: Directory to populate.
    """
    export = {
        "conversations": [
            {
                "uuid": "conv-1",
                "name": "Silt",
                "created_at": _CLAUDE_STAMP,
                "messages": [
                    {
                        "role": "human",
                        "content": _PROSE,
                        "created_at": _CLAUDE_STAMP,
                    },
                    {
                        "role": "assistant",
                        "content": "Silt settles.",
                        "created_at": _CLAUDE_STAMP,
                    },
                ],
            },
        ],
    }
    (root / "conversations.json").write_text(json.dumps(export), encoding="utf-8")


def _build_chatgpt_tree(root: Path) -> None:
    """Write a ChatGPT export: a ``mapping`` wired by ``parent``/``children``.

    Args:
        root: Directory to populate.
    """

    def node(
        node_id: str,
        role: str,
        text: str,
        parent: str,
        children: list[str],
    ) -> dict[str, Any]:
        """Return one message node of the conversation mapping.

        Args:
            node_id: The node's id, repeated on its message.
            role: ``user`` or ``assistant``.
            text: The message body.
            parent: The parent node's id.
            children: Ids of this node's children.

        Returns:
            A mapping entry in ChatGPT export shape.
        """
        return {
            "id": node_id,
            "message": {
                "id": node_id,
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]},
                "create_time": _CHATGPT_EPOCH,
            },
            "parent": parent,
            "children": children,
        }

    export = [
        {
            "title": "Silt",
            "create_time": _CHATGPT_EPOCH,
            "update_time": _CHATGPT_EPOCH + 100.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": node("u1", "user", _PROSE, "root", ["a1"]),
                "a1": node("a1", "assistant", "Silt settles.", "u1", []),
            },
        },
    ]
    (root / "conversations.json").write_text(json.dumps(export), encoding="utf-8")


def _build_discord_tree(root: Path) -> None:
    """Write the ``messages/<channel-id>/`` layout the ingestor insists on.

    Args:
        root: Directory to populate.
    """
    channel = root / "messages" / "1234"
    channel.mkdir(parents=True)
    (channel / "channel.json").write_text(
        json.dumps({"id": "1234", "name": "general", "type": "GUILD_TEXT"}),
        encoding="utf-8",
    )
    (channel / "messages.json").write_text(
        json.dumps(
            [
                {
                    "ID": "1",
                    "Timestamp": "2026-08-01T10:00:00+00:00",
                    "Contents": _PROSE,
                },
            ],
        ),
        encoding="utf-8",
    )


def _build_substack_tree(root: Path) -> None:
    """Write a post whose filename carries the post-id prefix.

    A name without the prefix is silently skipped -- the trap the page
    documents -- so the fixture must carry it or this test would report
    the doc wrong for a reason that is really the fixture's fault.

    Args:
        root: Directory to populate.
    """
    (root / "164523.on-silt.html").write_text(
        f"<html><body><h1>On Silt</h1><p>{_PROSE}</p></body></html>",
        encoding="utf-8",
    )


_FIXTURE_BUILDERS: Final[dict[str, Callable[[Path], None]]] = {
    "chatgpt": _build_chatgpt_tree,
    "claude": _build_claude_tree,
    "code": _build_code_tree,
    "discord": _build_discord_tree,
    "document": _build_document_tree,
    "generic": _build_generic_tree,
    "image": _build_image_tree,
    "markdown": _build_markdown_tree,
    "presentation": _build_presentation_tree,
    "spreadsheet": _build_spreadsheet_tree,
    "substack": _build_substack_tree,
}
"""One fixture builder per ``INGESTOR_REGISTRY`` key. Equality is asserted."""


class _StubOcrEngine:
    """A deterministic :class:`~creek.ingest.images.OcrEngine`.

    The real engine shells out to a ``tesseract`` system binary, which
    is not a Python dependency and is absent from most environments.
    The platform literal under test lives in ``ImageIngestor``'s own
    ``generate_frontmatter``, not in the engine, so injecting here
    swaps out only the part that cannot run.
    """

    def is_available(self) -> bool:
        """Report the stub as ready.

        Returns:
            Always ``True``.
        """
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        """Return canned OCR output for *image_path*.

        Args:
            image_path: Ignored; the result is constant.

        Returns:
            A high-confidence single-page result.
        """
        del image_path
        return OcrResult(text="Silt settles.", confidence=0.9)

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Return no pages; the fixture is a standalone image.

        Args:
            pdf_path: Ignored.

        Returns:
            An empty list.
        """
        del pdf_path
        return []


class _StubPresentationBackend:
    """A deterministic :class:`~creek.ingest.presentations.PresentationBackend`.

    Authoring a real ``.pptx`` needs an optional library; the platform
    literal under test does not. Same trade as :class:`_StubOcrEngine`.
    """

    def is_available(self) -> bool:
        """Report the stub as ready.

        Returns:
            Always ``True``.
        """
        return True

    def read_presentation(self, path: Path) -> PresentationData:
        """Return a canned one-slide deck for *path*.

        Args:
            path: Ignored; the result is constant.

        Returns:
            A single-slide :class:`PresentationData`.
        """
        del path
        return PresentationData(
            title="Silt",
            slides=(SlideData(index=1, title="Silt", body="Silt settles."),),
        )


_INJECTED_INGESTORS: Final[dict[str, Callable[[], Ingestor]]] = {
    "image": lambda: ImageIngestor(engine=_StubOcrEngine()),
    "presentation": lambda: PresentationIngestor(
        backend=_StubPresentationBackend(),
    ),
}
"""The two ingestors driven through an injection point, and how."""


def _ingestor_for(name: str) -> Ingestor:
    """Return the ingestor registered as *name*, stubbed where it must be.

    Args:
        name: An ``INGESTOR_REGISTRY`` key.

    Returns:
        A ready-to-run ingestor instance.
    """
    injected = _INJECTED_INGESTORS.get(name)
    if injected is not None:
        return injected()
    return INGESTOR_REGISTRY[name]()


def _platforms_stamped_by(name: str, source: Path) -> set[str]:
    """Run the *name* ingestor over *source* and return the platforms it wrote.

    Args:
        name: An ``INGESTOR_REGISTRY`` key.
        source: A directory populated by that ingestor's fixture builder.

    Returns:
        Every distinct ``source.platform`` value on the fragments produced.

    Raises:
        AssertionError: If the fixture discovers nothing or parses to no
            fragment -- which would make the comparison vacuous, and is a
            defect in this module rather than in the doc.
    """
    ingestor = _ingestor_for(name)
    raws = ingestor.discover(source)
    assert raws, (
        f"the {name} fixture discovered no input, so nothing was executed; "
        "fix the fixture in this module -- do not read this as doc drift"
    )
    observed = {
        str(ingestor.generate_frontmatter(fragment)["source"]["platform"])
        for raw in raws
        for fragment in ingestor.parse(raw)
    }
    assert observed, (
        f"the {name} fixture produced no fragment, so no platform was "
        "stamped; fix the fixture in this module"
    )
    return observed


def test_every_registered_ingestor_has_an_execution_fixture() -> None:
    """``_FIXTURE_BUILDERS`` covers ``INGESTOR_REGISTRY`` exactly.

    Without this, adding an ingestor would leave its producer row
    documented but never executed -- the exact gap that let the
    ``image_ocr`` / ``document`` / ``presentation`` rows sit unchecked.
    """
    assert set(_FIXTURE_BUILDERS) == set(INGESTOR_REGISTRY), (
        "tests/test_seeding_docs_capability_set.py must execute every "
        "registered ingestor; add a fixture builder for the new one"
    )


@pytest.mark.parametrize("name", sorted(INGESTOR_REGISTRY))
def test_ingestor_stamps_exactly_the_platforms_the_doc_credits_it_with(
    name: str,
    tmp_path: Path,
) -> None:
    """Every producer row in the doc's platform table is *run*, not read.

    The other assertions in this module compare names against names. This
    one writes a fixture, drives the real ingestor over it, and reads the
    ``source.platform`` it actually stamped -- so a platform literal
    changed in ``generate_frontmatter`` fails here even though every
    table name still matches.

    Args:
        name: The ``INGESTOR_REGISTRY`` key under test.
        tmp_path: Pytest's per-test temporary directory.
    """
    source = tmp_path / name
    source.mkdir()
    _FIXTURE_BUILDERS[name](source)

    observed = _platforms_stamped_by(name, source)
    documented = {
        platform
        for platform, producers in _documented_platform_producers().items()
        if name in producers
    }
    assert observed == documented, (
        f"the {name} ingestor stamps {sorted(observed)}, but "
        f"docs/seeding.md credits `{name}` with {sorted(documented)}"
    )


def test_platforms_the_doc_says_nothing_produces_are_absent_from_ingest() -> None:
    """A platform documented as having no producer is unmentioned in code.

    Scans the ingest package's source text rather than the registry,
    because an ingestor stamps its platform as a literal or an enum
    member, not as a registry key. A docstring mention also trips this
    -- deliberately: the point is to fail loudly the moment anyone
    starts wiring the platform up, not to pass until the wiring is
    finished.
    """
    orphans = {
        platform
        for platform, producers in _documented_platform_producers().items()
        if not producers
    }
    assert orphans, (
        "docs/seeding.md no longer marks any platform as having no producer; "
        "if every platform is now reachable, delete this test with the claim"
    )
    sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(_INGEST_PACKAGE.rglob("*.py"))
    ]
    for platform in sorted(orphans):
        needles = (f"SourcePlatform.{platform.upper()}", f'"platform": "{platform}"')
        hits = [needle for needle in needles for text in sources if needle in text]
        assert not hits, (
            f"creek/ingest mentions {hits[0]!r}, but docs/seeding.md still "
            f"tells users nothing produces `{platform}`"
        )


def test_documented_unknown_type_error_lists_the_registry() -> None:
    """The quoted ``Known types:`` line matches ``INGESTOR_REGISTRY``.

    The doc reproduces the exit-2 error verbatim so a reader can match
    what they see in their terminal. That literal is a second copy of
    the registry and rots the same way the tables do.
    """
    text = SEEDING_DOC.read_text(encoding="utf-8")
    start = text.index(_KNOWN_TYPES_MARKER) + len(_KNOWN_TYPES_MARKER)
    listed = text[start : text.index("```", start)]
    documented = [name.strip() for name in listed.replace("\n", " ").split(",")]
    assert documented == sorted(INGESTOR_REGISTRY), (
        "the `Known types:` line quoted in docs/seeding.md drifted from "
        "INGESTOR_REGISTRY; re-run `creek ingest --type bogus` and paste it"
    )


def test_no_assertion_in_this_module_is_skipped() -> None:
    """No test here is disabled by a skip, skipif or xfail marker.

    A skipped test reports as a pass, so silencing this gate with a
    marker would let exactly the doc rot through that the module exists
    to stop. Deleting a test is still possible; making one vacuous
    without deleting it is not.
    """
    module = sys.modules[__name__]
    assert not getattr(module, "pytestmark", []), (
        "a module-level pytestmark would disable every assertion here"
    )
    silenced = sorted(
        name
        for name in dir(module)
        if name.startswith("test_")
        for mark in getattr(getattr(module, name), "pytestmark", [])
        if mark.name in {"skip", "skipif", "xfail"}
    )
    assert not silenced, f"these tests are silenced by a marker: {silenced}"
